"""Task image Bundle data types and pure manifest/runtime assembly."""

from __future__ import annotations

import hashlib
import json
import shlex
from dataclasses import dataclass
from pathlib import Path

if __package__ and __package__.startswith("Agents."):
    from ...compose_bundle import (
        BUNDLE_FORMAT_VERSION,
        BUNDLE_SCHEMA_VERSION,
        BundleSpec,
        ServiceSpec,
    )
else:
    from compose_bundle import (
        BUNDLE_FORMAT_VERSION,
        BUNDLE_SCHEMA_VERSION,
        BundleSpec,
        ServiceSpec,
    )

# This is deliberately a small, version-controlled adapter contract rather
# than an inference based on service names or installed software.  It covers
# the real Compose task whose SSH sidecar has OCI evidence for port 22 but no
# image/Compose healthcheck from which readiness can otherwise be derived.
OPENSANDBOX_ADAPTER_METADATA: dict[str, dict[str, dict[str, dict[str, object]]]] = {
    "seta": {
        "973": {
            "worker": {
                "readiness": {"type": "tcp", "port": 22},
            }
        }
    }
}

LEGACY_DOCKERFILE_KEEPALIVE = ["sh", "-c", "while :; do sleep 60; done"]


@dataclass(frozen=True)
class PreparedBundle:
    main_image_ref: str
    manifest: dict[str, object]
    manifest_path: Path | None


def _path_relative_to_environment(path: Path, environment_dir: Path) -> str:
    try:
        return path.relative_to(environment_dir).as_posix()
    except ValueError as exc:
        raise ValueError(f"build path escapes task environment: {path}") from exc


def _compose_argv(value: object, *, label: str) -> list[str]:
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return list(value)
    if isinstance(value, str):
        try:
            return shlex.split(value)
        except ValueError as exc:
            raise RuntimeError(f"invalid Compose {label}: {exc}") from exc
    raise RuntimeError(f"Compose {label} must be a string or string list")


def _compose_runtime(
    service: ServiceSpec,
    image_config: dict[str, object],
    *,
    benchmark: str,
    task_identity: str,
    image_config_resolved: bool = True,
    legacy_dockerfile_keepalive: bool = False,
) -> dict[str, object]:
    """Materialize OCI defaults and Compose overrides for one provider run."""
    image_entrypoint = image_config.get("entrypoint")
    image_command = image_config.get("cmd")
    image_working_dir = image_config.get("working_dir")
    if image_entrypoint is not None and not isinstance(image_entrypoint, list):
        raise RuntimeError("normalized OCI image entrypoint is invalid")
    if image_command is not None and not isinstance(image_command, list):
        raise RuntimeError("normalized OCI image command is invalid")
    if image_working_dir is not None and not isinstance(image_working_dir, str):
        raise RuntimeError("normalized OCI image working directory is invalid")

    entrypoint_overridden = (
        service.entrypoint_present and service.entrypoint is not None
    )
    if entrypoint_overridden:
        effective_entrypoint = _compose_argv(service.entrypoint, label="entrypoint")
        entrypoint_source = "compose.entrypoint"
    else:
        effective_entrypoint = list(image_entrypoint or [])
        entrypoint_source = "image-config.entrypoint" if image_entrypoint else None

    command_overridden = service.command_present and service.command is not None
    if legacy_dockerfile_keepalive:
        # Harbor's Docker backend overlays every implicit single-Dockerfile
        # task with ``command: [sh, -c, sleep infinity]``. Mirror that
        # contract instead of releasing the image's default Cmd as a service
        # process: language base images commonly default to an interactive
        # interpreter (for example ``python3``), which exits immediately when
        # detached from stdin.
        effective_command = list(LEGACY_DOCKERFILE_KEEPALIVE)
        command_source = "adapter.legacy-keepalive"
    elif command_overridden:
        effective_command = _compose_argv(service.command, label="command")
        command_source = "compose.command"
    elif entrypoint_overridden:
        # Compose entrypoint override suppresses the image Cmd unless Compose
        # also explicitly supplies command.
        effective_command = []
        command_source = None
    else:
        effective_command = list(image_command or [])
        command_source = "image-config.cmd" if image_command else None

    start_argv = [*effective_entrypoint, *effective_command]
    sources = [source for source in (entrypoint_source, command_source) if source]
    start_source = "+".join(sources) if sources else None

    if service.working_dir is not None:
        workdir = service.working_dir
        workdir_source = "compose.working_dir"
    else:
        workdir = image_working_dir
        workdir_source = "image-config.working-dir" if workdir else None

    ports: list[dict[str, object]] = []
    seen_ports: set[tuple[int, str]] = set()

    def add_port(port: int, protocol: str, source: str) -> None:
        key = (port, protocol)
        if key not in seen_ports:
            seen_ports.add(key)
            ports.append({"port": port, "protocol": protocol, "source": source})

    for item in service.ports:
        raw_port = item.get("target") if isinstance(item, dict) else item
        raw_text = str(raw_port).split("/", 1)[0].rsplit(":", 1)[-1]
        protocol = (
            str(item.get("protocol", "tcp")).lower()
            if isinstance(item, dict)
            else (
                str(raw_port).split("/", 1)[1].lower()
                if "/" in str(raw_port)
                else "tcp"
            )
        )
        if (
            raw_text.isdigit()
            and protocol in {"tcp", "udp"}
            and 1 <= int(raw_text) <= 65535
        ):
            add_port(int(raw_text), protocol, "compose.ports.target")
    for item in service.expose:
        raw_text, separator, raw_protocol = str(item).partition("/")
        protocol = raw_protocol.lower() if separator else "tcp"
        if (
            raw_text.isdigit()
            and protocol in {"tcp", "udp"}
            and 1 <= int(raw_text) <= 65535
        ):
            add_port(int(raw_text), protocol, "compose.expose")
        else:
            raise RuntimeError(
                f"invalid Compose expose port for service {service.name!r}: {item!r}"
            )
    for item in image_config.get("exposed_ports") or []:
        if not isinstance(item, dict):
            raise TypeError("normalized OCI image exposed ports are invalid")
        add_port(int(item["port"]), str(item["protocol"]), "image-config.exposed-ports")

    healthcheck = service.healthcheck or image_config.get("healthcheck")
    healthcheck_source = (
        ("compose.healthcheck" if service.healthcheck else "image-config.healthcheck")
        if healthcheck
        else None
    )
    readiness: dict[str, object] | None = None
    if healthcheck:
        readiness = {
            "type": "healthcheck",
            "healthcheck": healthcheck,
            "source": healthcheck_source,
        }
    else:
        metadata = (
            OPENSANDBOX_ADAPTER_METADATA.get(benchmark, {})
            .get(task_identity, {})
            .get(service.name, {})
        )
        candidate = metadata.get("readiness")
        if isinstance(candidate, dict):
            port = candidate.get("port")
            if (
                candidate.get("type") == "tcp"
                and isinstance(port, int)
                and any(
                    entry["port"] == port and entry["protocol"] == "tcp"
                    for entry in ports
                )
            ):
                readiness = {
                    "type": "tcp",
                    "port": port,
                    "source": f"adapter-metadata:{benchmark}/{task_identity}/{service.name}",
                }
            elif image_config_resolved:
                raise RuntimeError(
                    f"OpenSandbox adapter readiness metadata has no matching TCP internal port for {service.name!r}"
                )

    return {
        "start_argv": start_argv,
        "start_argv_source": start_source,
        "workdir": workdir,
        "workdir_source": workdir_source,
        "internal_ports": ports,
        "readiness": readiness,
    }


def _service_manifest(
    service: ServiceSpec,
    artifact: dict[str, object],
    environment_dir: Path,
    *,
    benchmark: str,
    task_identity: str,
    definition_kind: str,
) -> dict[str, object]:
    build: dict[str, object] | None = None
    if service.build is not None:
        build = {
            "context": _path_relative_to_environment(
                service.build.context_dir, environment_dir
            ),
            "dockerfile": _path_relative_to_environment(
                service.build.dockerfile, environment_dir
            ),
            "target": service.build.target,
            # Values may be credentials. The immutable manifest exposes only
            # names; runtime overrides never participate in image identity.
            "build_arg_names": artifact["build_arg_names"],
        }
    image_config = artifact.get("config")
    if not isinstance(image_config, dict):
        raise TypeError(f"service {service.name!r} has no normalized OCI image config")
    return {
        "image": artifact,
        "build": build,
        "source_image": service.source_image,
        "entrypoint": service.entrypoint,
        "entrypoint_present": service.entrypoint_present,
        "command": service.command,
        "command_present": service.command_present,
        "working_dir": service.working_dir,
        "environment": service.environment,
        "ports": service.ports,
        "expose": service.expose,
        "aliases": service.aliases,
        "depends_on": service.depends_on,
        "healthcheck": service.healthcheck,
        "volumes": service.volumes,
        "networks": service.networks,
        "cap_add": service.cap_add,
        "privileged": service.privileged,
        "container_name": service.container_name,
        "resources": service.resources,
        "unsupported_fields": service.unsupported_fields,
        "runtime": _compose_runtime(
            service,
            image_config,
            benchmark=benchmark,
            task_identity=task_identity,
            image_config_resolved=bool(artifact.get("config_resolved", True)),
            legacy_dockerfile_keepalive=definition_kind == "dockerfile",
        ),
    }


def _bundle_identity(bundle: BundleSpec, services: dict[str, dict[str, object]]) -> str:
    # Registry location/tag/schema are materialization details. A copied
    # Bundle with identical artifacts and topology retains its identity.
    images = {
        name: services[name]["image"]["artifact_digest"] for name in sorted(services)
    }
    payload = {
        "main_service": bundle.main_service,
        "images": images,
        "topology": {
            name: {
                key: services[name].get(key)
                for key in (
                    "entrypoint",
                    "entrypoint_present",
                    "command",
                    "command_present",
                    "working_dir",
                    "environment",
                    "ports",
                    "expose",
                    "aliases",
                    "depends_on",
                    "healthcheck",
                    "volumes",
                    "networks",
                    "cap_add",
                    "privileged",
                    "resources",
                    "unsupported_fields",
                    "runtime",
                )
            }
            for name in sorted(services)
        },
    }
    return "sha256:" + hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def assemble_bundle_manifest(
    bundle: BundleSpec,
    artifacts: dict[str, dict[str, object]],
    *,
    benchmark: str,
    registry_host: str,
    project: str,
    task_repository: str,
    repository: str,
) -> dict[str, object]:
    """Assemble service runtime/config and Bundle metadata from resolved images."""
    if set(artifacts) != set(bundle.services):
        raise ValueError("image artifacts must match the Bundle service names")
    services = {
        name: _service_manifest(
            bundle.services[name], artifacts[name], bundle.environment_dir,
            benchmark=benchmark, task_identity=bundle.task_identity,
            definition_kind=bundle.definition_kind,
        )
        for name in sorted(bundle.services)
    }
    return {
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "bundle_format": BUNDLE_FORMAT_VERSION,
        "benchmark": {"name": benchmark},
        "task_identity": bundle.task_identity,
        "registry": {
            "host": registry_host,
            "project": project,
            "task_repository": task_repository,
            "repository": repository,
        },
        "definition_kind": bundle.definition_kind,
        "definition_identity": f"sha256:{bundle.definition_identity}",
        "bundle_identity": _bundle_identity(bundle, services),
        "normalization_backend": bundle.normalization_backend,
        "main": bundle.main_service,
        "services": services,
        "requirements": bundle.requirements,
    }
