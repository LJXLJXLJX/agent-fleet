"""Normalize Harbor Dockerfile and Compose task environments into one bundle.

This module deliberately owns definition parsing only.  It does not build or
publish images and it does not create runtime environments.  Provider-specific
image managers attach immutable image artifacts to the returned service specs;
runtime environments consume the resulting manifest later.

The fallback Compose loader supports the subset required by the current SETA
tasks.  Build fields outside that subset fail explicitly so that an image is
never built with silently different semantics.  Runtime features that require
provider capabilities are retained in the bundle requirements for a later
preflight gate.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import yaml
except ModuleNotFoundError as exc:  # pragma: no cover - runner dependency guard
    raise RuntimeError(
        "PyYAML is required to parse Docker Compose task environments"
    ) from exc


# The manifest writer is deliberately versioned independently from the
# provider-neutral parser below.  Readers in the image manager and runtime
# retain a narrow v1 compatibility path, but all newly materialized bundles
# use the project/task repository model introduced in v2.
BUNDLE_SCHEMA_VERSION = 2
BUNDLE_FORMAT_VERSION = "harbor-environment-bundle-v2"
COMPOSE_FILENAMES = ("docker-compose.yaml", "docker-compose.yml")
BUILD_KEYS = {"context", "dockerfile", "args", "target"}
SERVICE_KEYS = {
    "build",
    "image",
    "entrypoint",
    "command",
    "environment",
    "ports",
    "expose",
    "hostname",
    "container_name",
    "depends_on",
    "healthcheck",
    "volumes",
    "networks",
    "cap_add",
    "privileged",
    "deploy",
}
BUILD_ARG_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
INTERPOLATION = re.compile(
    r"\$\{(?P<name>[A-Za-z_][A-Za-z0-9_]*)"
    r"(?:(?P<operator>:-?)(?P<default>[^}]*))?\}"
)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _relative(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError as exc:
        raise ValueError(f"path escapes task environment: {path}") from exc


def _resolve_under(root: Path, raw: str, *, label: str) -> Path:
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError(f"{label} escapes task environment: {raw!r}") from exc
    return resolved


def _interpolate(value: str, variables: dict[str, str], *, label: str) -> str:
    """Resolve the Compose ${VAR}, ${VAR-default}, and ${VAR:-default} forms."""

    def replace(match: re.Match[str]) -> str:
        name = match.group("name")
        operator = match.group("operator")
        default = match.group("default") or ""
        current = variables.get(name)
        if current is not None and (operator != ":-" or current != ""):
            return current
        if operator in {"-", ":-"}:
            return default
        raise ValueError(f"unresolved Compose variable {name!r} in {label}")

    return INTERPOLATION.sub(replace, value).replace("$$", "$")


def _normalize_build_args(
    raw: Any, variables: dict[str, str], *, service: str
) -> dict[str, str]:
    if raw is None:
        return {}
    entries: dict[str, Any]
    if isinstance(raw, dict):
        entries = raw
    elif isinstance(raw, list):
        entries = {}
        for item in raw:
            if not isinstance(item, str):
                raise TypeError(f"service {service!r} build args must contain strings")
            name, separator, value = item.partition("=")
            entries[name] = value if separator else None
    else:
        raise TypeError(f"service {service!r} build.args must be a map or list")

    normalized: dict[str, str] = {}
    for name, value in entries.items():
        if not isinstance(name, str) or not BUILD_ARG_NAME.fullmatch(name):
            raise ValueError(
                f"service {service!r} has invalid build arg name: {name!r}"
            )
        if value is None:
            if name not in variables:
                raise ValueError(
                    f"service {service!r} build arg {name!r} has no value"
                )
            normalized[name] = variables[name]
        elif isinstance(value, (str, int, float, bool)):
            text = str(value).lower() if isinstance(value, bool) else str(value)
            normalized[name] = _interpolate(
                text, variables, label=f"service {service!r} build arg {name!r}"
            )
        else:
            raise TypeError(
                f"service {service!r} has invalid build arg value for {name!r}"
            )
    return normalized


def _normalize_depends_on(raw: Any, *, service: str) -> dict[str, dict[str, Any]]:
    if raw is None:
        return {}
    if isinstance(raw, list):
        dependencies = raw
        result: dict[str, dict[str, Any]] = {}
        for dependency in dependencies:
            if not isinstance(dependency, str) or not dependency:
                raise TypeError(f"service {service!r} depends_on entries must be names")
            result[dependency] = {"condition": "service_started", "required": True}
        return result
    if not isinstance(raw, dict):
        raise TypeError(f"service {service!r} depends_on must be a map or list")

    result = {}
    for dependency, config in raw.items():
        if not isinstance(dependency, str) or not dependency:
            raise TypeError(f"service {service!r} depends_on keys must be names")
        if config is None:
            normalized = {"condition": "service_started", "required": True}
        elif isinstance(config, str):
            normalized = {"condition": config, "required": True}
        elif isinstance(config, dict):
            normalized = {
                "condition": str(config.get("condition") or "service_started"),
                "required": bool(config.get("required", True)),
            }
            if "restart" in config:
                normalized["restart"] = bool(config["restart"])
        else:
            raise TypeError(
                f"service {service!r} dependency {dependency!r} has invalid config"
            )
        result[dependency] = normalized
    return result


def _normalize_networks(raw: Any, *, service: str) -> dict[str, dict[str, Any]]:
    if raw is None:
        return {}
    if isinstance(raw, list):
        result = {}
        for name in raw:
            if not isinstance(name, str) or not name:
                raise TypeError(f"service {service!r} network entries must be names")
            result[name] = {}
        return result
    if not isinstance(raw, dict):
        raise TypeError(f"service {service!r} networks must be a map or list")
    result = {}
    for name, config in raw.items():
        if not isinstance(name, str) or not name:
            raise TypeError(f"service {service!r} network keys must be names")
        if config is None:
            result[name] = {}
        elif isinstance(config, dict):
            result[name] = dict(config)
        else:
            raise TypeError(
                f"service {service!r} network {name!r} config must be a map"
            )
    return result


def _normalize_string_list(raw: Any, *, label: str) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise TypeError(f"{label} must be a list of strings")
    return list(raw)


def _normalize_environment(raw: Any, *, service: str) -> dict[str, str | None]:
    if raw is None:
        return {}
    if isinstance(raw, dict):
        result: dict[str, str | None] = {}
        for name, value in raw.items():
            if not isinstance(name, str):
                raise TypeError(f"service {service!r} environment keys must be strings")
            if value is None:
                result[name] = None
            elif isinstance(value, (str, int, float, bool)):
                result[name] = str(value).lower() if isinstance(value, bool) else str(value)
            else:
                raise TypeError(
                    f"service {service!r} environment value for {name!r} is invalid"
                )
        return result
    if isinstance(raw, list):
        result = {}
        for item in raw:
            if not isinstance(item, str):
                raise TypeError(
                    f"service {service!r} environment list must contain strings"
                )
            name, separator, value = item.partition("=")
            if not name:
                raise ValueError(f"service {service!r} has an empty environment name")
            result[name] = value if separator else None
        return result
    raise TypeError(f"service {service!r} environment must be a map or list")


def _normalize_expose(raw: Any, *, service: str) -> list[str | int]:
    """Retain Compose's internal-port declaration without interpreting it.

    ``expose`` is metadata for service-to-service reachability, not a host
    publication.  The materializer resolves it together with the final OCI
    image config; keeping the original scalar form here avoids prematurely
    losing its protocol suffix.
    """
    if raw is None:
        return []
    if not isinstance(raw, list) or not all(isinstance(item, (str, int)) for item in raw):
        raise TypeError(f"service {service!r} expose must be a list of strings or integers")
    return list(raw)


@dataclass(frozen=True)
class BuildSpec:
    context_dir: Path
    dockerfile: Path
    args: dict[str, str]
    target: str | None = None

    def identity_payload(self, environment_dir: Path) -> dict[str, Any]:
        return {
            "context": _relative(self.context_dir, environment_dir),
            "dockerfile": _relative(self.dockerfile, environment_dir),
            "args": self.args,
            "target": self.target,
        }


@dataclass(frozen=True)
class ServiceSpec:
    name: str
    build: BuildSpec | None
    source_image: str | None
    entrypoint: Any
    entrypoint_present: bool
    command: Any
    command_present: bool
    environment: dict[str, str | None]
    ports: list[Any]
    expose: list[str | int]
    aliases: list[str]
    depends_on: dict[str, dict[str, Any]]
    healthcheck: dict[str, Any] | None
    volumes: list[Any]
    networks: dict[str, dict[str, Any]]
    cap_add: list[str]
    privileged: bool
    container_name: str | None
    resources: dict[str, Any]
    unsupported_fields: list[str]

    def topology_payload(self, environment_dir: Path) -> dict[str, Any]:
        return {
            "name": self.name,
            "build": (
                self.build.identity_payload(environment_dir) if self.build else None
            ),
            "source_image": self.source_image,
            "entrypoint": self.entrypoint,
            "entrypoint_present": self.entrypoint_present,
            "command": self.command,
            "command_present": self.command_present,
            "environment": self.environment,
            "ports": self.ports,
            "expose": self.expose,
            "aliases": self.aliases,
            "depends_on": self.depends_on,
            "healthcheck": self.healthcheck,
            "volumes": self.volumes,
            "networks": self.networks,
            "cap_add": self.cap_add,
            "privileged": self.privileged,
            "container_name": self.container_name,
            "resources": self.resources,
            "unsupported_fields": self.unsupported_fields,
        }


@dataclass(frozen=True)
class BundleSpec:
    task_dir: Path
    environment_dir: Path
    definition_kind: str
    main_service: str
    services: dict[str, ServiceSpec]
    requirements: dict[str, Any]
    normalization_backend: str

    @property
    def task_identity(self) -> str:
        return self.task_dir.name

    @property
    def definition_identity(self) -> str:
        return _digest(self.identity_payload())

    def identity_payload(self) -> dict[str, Any]:
        return {
            "format": BUNDLE_FORMAT_VERSION,
            "definition_kind": self.definition_kind,
            "main_service": self.main_service,
            "services": {
                name: self.services[name].topology_payload(self.environment_dir)
                for name in sorted(self.services)
            },
            "requirements": self.requirements,
        }


def _compose_path(environment_dir: Path) -> Path | None:
    found = [environment_dir / name for name in COMPOSE_FILENAMES]
    existing = [path for path in found if path.is_file()]
    if len(existing) > 1:
        raise ValueError(
            f"multiple Compose definitions found under {environment_dir}: "
            + ", ".join(path.name for path in existing)
        )
    return existing[0] if existing else None


def _parse_build(
    raw: Any,
    *,
    environment_dir: Path,
    variables: dict[str, str],
    service: str,
) -> BuildSpec | None:
    if raw is None:
        return None
    if isinstance(raw, str):
        config: dict[str, Any] = {"context": raw}
    elif isinstance(raw, dict):
        config = dict(raw)
    else:
        raise TypeError(f"service {service!r} build must be a string or map")

    unsupported = sorted(set(config) - BUILD_KEYS)
    if unsupported:
        raise ValueError(
            f"service {service!r} uses unsupported build fields: "
            + ", ".join(unsupported)
        )

    raw_context = str(config.get("context") or ".")
    context_value = _interpolate(
        raw_context, variables, label=f"service {service!r} build context"
    )
    context_dir = _resolve_under(
        environment_dir, context_value, label=f"service {service!r} build context"
    )
    if not context_dir.is_dir():
        raise ValueError(
            f"service {service!r} build context is not a directory: {context_dir}"
        )

    raw_dockerfile = str(config.get("dockerfile") or "Dockerfile")
    dockerfile_value = _interpolate(
        raw_dockerfile, variables, label=f"service {service!r} Dockerfile"
    )
    dockerfile_candidate = Path(dockerfile_value).expanduser()
    if not dockerfile_candidate.is_absolute():
        dockerfile_candidate = context_dir / dockerfile_candidate
    dockerfile = dockerfile_candidate.resolve()
    # A Compose Dockerfile may be outside the context but must remain inside
    # the task's environment directory.
    _relative(dockerfile, environment_dir)
    if not dockerfile.is_file():
        raise ValueError(f"service {service!r} Dockerfile not found: {dockerfile}")

    target = config.get("target")
    if target is not None and not isinstance(target, str):
        raise TypeError(f"service {service!r} build target must be a string")
    return BuildSpec(
        context_dir=context_dir,
        dockerfile=dockerfile,
        args=_normalize_build_args(config.get("args"), variables, service=service),
        target=target or None,
    )


def _compose_service(
    name: str,
    raw: Any,
    *,
    environment_dir: Path,
    variables: dict[str, str],
) -> ServiceSpec:
    if not isinstance(raw, dict):
        raise TypeError(f"Compose service {name!r} must be a map")
    build = _parse_build(
        raw.get("build"),
        environment_dir=environment_dir,
        variables=variables,
        service=name,
    )
    source_image = raw.get("image")
    if source_image is not None and not isinstance(source_image, str):
        raise TypeError(f"service {name!r} image must be a string")
    if build is None and not source_image:
        raise ValueError(f"service {name!r} must define build or image")

    container_name = raw.get("container_name")
    if container_name is not None and not isinstance(container_name, str):
        raise TypeError(f"service {name!r} container_name must be a string")
    deploy = raw.get("deploy") or {}
    if not isinstance(deploy, dict):
        raise TypeError(f"service {name!r} deploy must be a map")
    resources = deploy.get("resources") or {}
    if not isinstance(resources, dict):
        raise TypeError(f"service {name!r} deploy.resources must be a map")
    unsupported_fields = [
        f"services.{name}.{field}" for field in sorted(set(raw) - SERVICE_KEYS)
    ]
    unsupported_fields.extend(
        f"services.{name}.deploy.{field}"
        for field in sorted(set(deploy) - {"resources"})
    )

    networks = _normalize_networks(raw.get("networks"), service=name)
    aliases = {name}
    hostname = raw.get("hostname")
    if hostname is not None:
        if not isinstance(hostname, str) or not hostname:
            raise TypeError(f"service {name!r} hostname must be a non-empty string")
        aliases.add(hostname)
    for network in networks.values():
        network_aliases = network.get("aliases") or []
        if not isinstance(network_aliases, list) or not all(
            isinstance(alias, str) and alias for alias in network_aliases
        ):
            raise TypeError(f"service {name!r} network aliases must be strings")
        aliases.update(network_aliases)

    ports = raw.get("ports") or []
    if not isinstance(ports, list):
        raise TypeError(f"service {name!r} ports must be a list")
    volumes = raw.get("volumes") or []
    if not isinstance(volumes, list):
        raise TypeError(f"service {name!r} volumes must be a list")
    healthcheck = raw.get("healthcheck")
    if healthcheck is not None and not isinstance(healthcheck, dict):
        raise TypeError(f"service {name!r} healthcheck must be a map")

    return ServiceSpec(
        name=name,
        build=build,
        source_image=source_image,
        entrypoint=raw.get("entrypoint"),
        entrypoint_present="entrypoint" in raw,
        command=raw.get("command"),
        command_present="command" in raw,
        environment=_normalize_environment(raw.get("environment"), service=name),
        ports=list(ports),
        expose=_normalize_expose(raw.get("expose"), service=name),
        aliases=sorted(aliases),
        depends_on=_normalize_depends_on(raw.get("depends_on"), service=name),
        healthcheck=dict(healthcheck) if healthcheck is not None else None,
        volumes=list(volumes),
        networks=networks,
        cap_add=[
            item.upper()
            for item in _normalize_string_list(
                raw.get("cap_add"), label=f"service {name!r} cap_add"
            )
        ],
        privileged=bool(raw.get("privileged", False)),
        container_name=container_name,
        resources=dict(resources),
        unsupported_fields=unsupported_fields,
    )


def _volume_source(raw: Any) -> str | None:
    if isinstance(raw, str):
        source, separator, _target = raw.partition(":")
        if not separator or not source or source.startswith(("/", ".", "~", "${")):
            return None
        return source
    if isinstance(raw, dict) and raw.get("type", "volume") == "volume":
        source = raw.get("source")
        return source if isinstance(source, str) and source else None
    return None


_LEGACY_LOG_BIND_MOUNTS = {
    f"${{HOST_{name}_PATH}}:${{ENV_{name}_PATH}}"
    for name in ("AGENT_LOGS", "ARTIFACTS", "VERIFIER_LOGS")
}


def _unsupported_bind_mount(raw: Any) -> bool:
    if isinstance(raw, str):
        source, separator, _target = raw.partition(":")
        return bool(
            separator
            and source.startswith(("/", ".", "~", "${"))
            and raw not in _LEGACY_LOG_BIND_MOUNTS
        )
    return isinstance(raw, dict) and raw.get("type", "volume") == "bind"


def _requirements(
    services: dict[str, ServiceSpec], compose: dict[str, Any]
) -> dict[str, Any]:
    top_networks = compose.get("networks") or {}
    if not isinstance(top_networks, dict):
        raise TypeError("top-level Compose networks must be a map")
    top_volumes = compose.get("volumes") or {}
    if not isinstance(top_volumes, dict):
        raise TypeError("top-level Compose volumes must be a map")

    fixed_ip = False
    for service in services.values():
        if any(
            "ipv4_address" in config or "ipv6_address" in config
            for config in service.networks.values()
        ):
            fixed_ip = True
    if any(
        isinstance(config, dict) and config.get("ipam")
        for config in top_networks.values()
    ):
        fixed_ip = True

    named_volume_sources = {
        source
        for service in services.values()
        for volume in service.volumes
        if (source := _volume_source(volume)) is not None
    }
    capabilities = {
        capability for service in services.values() for capability in service.cap_add
    }
    unsupported: list[str] = []
    for field in ("configs", "secrets"):
        if compose.get(field):
            unsupported.append(field)
    for service in services.values():
        unsupported.extend(service.unsupported_fields)
        if any(_unsupported_bind_mount(volume) for volume in service.volumes):
            unsupported.append(f"services.{service.name}.volumes.bind")
        if service.privileged:
            unsupported.append(f"services.{service.name}.privileged")

    return {
        "multi_service": len(services) > 1,
        "service_aliases": len(services) > 1,
        "shared_volumes": bool(top_volumes or named_volume_sources),
        "fixed_ip": fixed_ip,
        "multiple_networks": len(top_networks) > 1,
        "net_admin": "NET_ADMIN" in capabilities,
        "sys_admin": "SYS_ADMIN" in capabilities,
        "privileged": any(service.privileged for service in services.values()),
        "unsupported_features": sorted(set(unsupported)),
    }


def _validate_dependencies(services: dict[str, ServiceSpec]) -> None:
    for service in services.values():
        missing = sorted(set(service.depends_on) - set(services))
        if missing:
            raise ValueError(
                f"service {service.name!r} depends on unknown services: "
                + ", ".join(missing)
            )

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(name: str) -> None:
        if name in visited:
            return
        if name in visiting:
            raise ValueError(f"Compose depends_on contains a cycle at service {name!r}")
        visiting.add(name)
        for dependency in services[name].depends_on:
            visit(dependency)
        visiting.remove(name)
        visited.add(name)

    for name in sorted(services):
        visit(name)


def _load_compose(path: Path) -> dict[str, Any]:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise TypeError(f"Compose definition must be a map: {path}")
    return loaded


def resolve_bundle_spec(task_dir: Path) -> BundleSpec:
    task_dir = task_dir.resolve()
    environment_dir = (task_dir / "environment").resolve()
    if not (task_dir / "task.toml").is_file():
        raise ValueError(f"task.toml not found under {task_dir}")
    if not environment_dir.is_dir():
        raise ValueError(f"environment directory not found under {task_dir}")

    compose_path = _compose_path(environment_dir)
    if compose_path is None:
        dockerfile = environment_dir / "Dockerfile"
        if not dockerfile.is_file():
            raise ValueError(f"Dockerfile not found under {environment_dir}")
        main = ServiceSpec(
            name="main",
            build=BuildSpec(
                context_dir=environment_dir,
                dockerfile=dockerfile,
                args={},
            ),
            source_image=None,
            entrypoint=None,
            entrypoint_present=False,
            command=None,
            command_present=False,
            environment={},
            ports=[],
            expose=[],
            aliases=["main"],
            depends_on={},
            healthcheck=None,
            volumes=[],
            networks={},
            cap_add=[],
            privileged=False,
            container_name=None,
            resources={},
            unsupported_fields=[],
        )
        return BundleSpec(
            task_dir=task_dir,
            environment_dir=environment_dir,
            definition_kind="dockerfile",
            main_service="main",
            services={"main": main},
            requirements={
                "multi_service": False,
                "service_aliases": False,
                "shared_volumes": False,
                "fixed_ip": False,
                "multiple_networks": False,
                "net_admin": False,
                "sys_admin": False,
                "privileged": False,
                "unsupported_features": [],
            },
            normalization_backend="implicit-dockerfile",
        )

    compose = _load_compose(compose_path)
    raw_services = compose.get("services")
    if not isinstance(raw_services, dict) or not raw_services:
        raise ValueError(f"Compose definition has no services: {compose_path}")
    if "main" not in raw_services:
        raise ValueError("Compose task must define the Harbor main service")

    variables = {**os.environ, "CONTEXT_DIR": str(environment_dir)}
    services = {
        str(name): _compose_service(
            str(name),
            raw,
            environment_dir=environment_dir,
            variables=variables,
        )
        for name, raw in raw_services.items()
    }
    _validate_dependencies(services)
    return BundleSpec(
        task_dir=task_dir,
        environment_dir=environment_dir,
        definition_kind="compose",
        main_service="main",
        services=services,
        requirements=_requirements(services, compose),
        normalization_backend="pyyaml-restricted-compose",
    )
