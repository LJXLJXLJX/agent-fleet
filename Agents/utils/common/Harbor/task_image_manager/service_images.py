"""Prepare one service image: resolve, build, publish, and inspect."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 is still used by some H-side tools.
    tomllib = None  # type: ignore[assignment]

if __package__ and __package__.startswith("Agents."):
    from ..compose_bundle import BundleSpec, ServiceSpec
else:
    from compose_bundle import BundleSpec, ServiceSpec


from .build_engine import build_image, inspect_external_image
from .image_build_state import (
    atomic_write_json,
    exclusive_image_build_lock,
    image_build_state_paths,
    read_json_record,
)
from .oci import (
    DOCKER_MANIFEST,
    digest_bytes,
)
from .registry import (
    RegistryClient,
    RegistryTarget,
    SkopeoPublisher,
    log,
    registry_credentials,
)
from .task_image_identity import image_identity


def _transport_record(
    *,
    dockerhub_mirror_prefix: str,
    download_source_url: str,
    package_build_args: dict[str, str],
    github_mirror_url: str,
    rustup_init_url: str,
    pytorch_index_url: str,
) -> dict[str, str]:
    return {
        "download_source_configured": str(bool(download_source_url)).lower(),
        "dockerhub_mirror_prefix": dockerhub_mirror_prefix,
        "github_mirror_configured": str(bool(github_mirror_url)).lower(),
        "package_source_args": ",".join(sorted(package_build_args)),
        "pytorch_index_configured": str(bool(pytorch_index_url)).lower(),
        "rustup_init_configured": str(bool(rustup_init_url)).lower(),
    }


def load_build_timeout(task_dir: Path) -> float:
    task_config_path = task_dir / "task.toml"
    if tomllib is not None:
        with task_config_path.open("rb") as handle:
            task_config = tomllib.load(handle)
        value = (task_config.get("environment") or {}).get("build_timeout_sec", 600)
    else:
        value = 600
        section = ""
        for raw_line in task_config_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.split("#", 1)[0].strip()
            if line.startswith("[") and line.endswith("]"):
                section = line[1:-1].strip()
            elif section == "environment" and line.startswith("build_timeout_sec"):
                _, raw_value = line.split("=", 1)
                value = raw_value.strip()
                break
    timeout = float(value)
    if timeout <= 0:
        raise ValueError(f"invalid environment.build_timeout_sec: {value!r}")
    return timeout


def _service_image_inputs(
    service: ServiceSpec,
    *,
    bundle: BundleSpec,
    dockerhub_mirror_prefix: str,
    package_build_args: dict[str, str],
    platform: str,
    explicit_build_args: dict[str, str],
    dry_run: bool,
) -> tuple[
    str,
    dict[str, str],
    dict[str, str],
    str | None,
    str | None,
]:
    if service.build is not None:
        # Mirror routes are defaults. A task/Compose build arg may select an
        # explicit package source, and the operator's explicit JSON override
        # remains authoritative over both.
        effective_build_args = {
            **package_build_args,
            **service.build.args,
            **explicit_build_args,
        }
        declared_build_args = {
            **service.build.args,
            **explicit_build_args,
        }
        # P0 invariant: image identity comes only from the original static task
        # environment. Including renderer/runtime/mirror mutations is a P0 bug.
        identity = image_identity(bundle.environment_dir)
        return identity, declared_build_args, effective_build_args, None, None
    if not service.source_image:
        raise ValueError(f"service {service.name!r} has no build or source image")
    resolved_ref, source_digest = inspect_external_image(
        service.source_image,
        dockerhub_mirror_prefix,
        platform,
        dry_run=dry_run,
    )
    identity = image_identity(
        bundle.environment_dir,
        docker_image=service.source_image,
    )
    return identity, {}, {}, resolved_ref, source_digest


def prepare_service_image(
    *,
    service: ServiceSpec,
    bundle: BundleSpec,
    args: argparse.Namespace,
    explicit_build_args: dict[str, str],
    proxy_args: dict[str, str],
    cache_root: Path,
    target: RegistryTarget,
    publisher: SkopeoPublisher | None,
    registry: RegistryClient | None,
) -> dict[str, object]:
    # Local uploaded-Bundle verification and forced prebuilds retain their
    # maintenance contract. Normal consumers resolve by task before hashing.
    if (
        not args.dry_run
        and not args.force
        and not getattr(args, "reuse_local_upload_cache", False)
    ):
        if registry is None or publisher is None:
            raise RuntimeError("Registry client is unavailable outside dry-run mode")
        try:
            existing = registry.latest_image(
                service.name, single_service=len(bundle.services) == 1
            )
        except HTTPError as exc:
            if exc.code not in {401, 403} or publisher.username or publisher.password:
                raise
            publisher.username, publisher.password = registry_credentials(
                args.docker_config, args.registry
            )
            existing = registry.latest_image(
                service.name, single_service=len(bundle.services) == 1
            )
        if existing is not None:
            if getattr(args, "validate_image_hash", True):
                identity = (
                    image_identity(bundle.environment_dir)
                    if service.build is not None
                    else image_identity(
                        bundle.environment_dir, docker_image=service.source_image
                    )
                )
                expected_tag = target.tag(service.name, identity)
                if existing["tag"] != expected_tag:
                    raise RuntimeError(
                        f"task image hash validation failed for task={bundle.task_identity} "
                        f"service={service.name}: selected {existing['tag_ref']}, "
                        f"expected tag {expected_tag!r} from the local task definition; "
                        "the remote image may be stale or its tag may not encode the content hash"
                    )
            else:
                log(
                    f"WARNING: skipping task image hash validation for "
                    f"task={bundle.task_identity} service={service.name}: "
                    "the local dataset task definition may be inconsistent with "
                    f"the image currently available in the remote repository ({existing['tag_ref']}); "
                    "enable --validate-image-hash or HARBOR_TASK_IMAGE_VALIDATE_HASH=1 "
                    "to check the content-hash tag prefix"
                )
            log(
                f"resolved task={bundle.task_identity} service={service.name}: {existing['tag_ref']}"
            )
            declared_args = (
                {**service.build.args, **explicit_build_args}
                if service.build is not None
                else {}
            )
            return {
                **existing,
                "source": "registry",
                # The uploader's full content hash cannot be inferred from a tag.
                "input_hash": None,
                "platform": args.platform,
                "build_arg_names": sorted(declared_args),
                "config": publisher.inspect_config(existing["digest_ref"]),
                "config_resolved": True,
            }
        # An empty task repository requires the original build/push flow.
        publisher.username, publisher.password = registry_credentials(
            args.docker_config, args.registry
        )
    (
        identity,
        declared_build_args,
        effective_service_build_args,
        resolved_external_ref,
        source_digest,
    ) = _service_image_inputs(
        service,
        bundle=bundle,
        dockerhub_mirror_prefix=args.dockerhub_mirror_prefix,
        package_build_args=args.package_build_args,
        platform=args.platform,
        explicit_build_args=explicit_build_args,
        dry_run=args.dry_run,
    )
    input_hash = f"sha256:{identity}"
    tag = target.tag(service.name, input_hash)
    tag_ref = target.tag_ref(service.name, input_hash)
    image_source = "build" if service.build is not None else "external-mirror"
    artifact: dict[str, object] = {
        "source": image_source,
        "input_hash": input_hash,
        "tag": tag,
        "tag_ref": tag_ref,
        "artifact_digest": None,
        "digest_ref": None,
        "media_type": DOCKER_MANIFEST,
        "platform": args.platform,
        "build_arg_names": sorted(declared_build_args),
        "config": {
            "entrypoint": None,
            "cmd": None,
            "exposed_ports": [],
            "healthcheck": None,
        },
        "config_resolved": False,
    }
    if source_digest is not None:
        artifact["source_manifest_digest"] = source_digest
    if args.dry_run:
        artifact["artifact_digest"] = digest_bytes(f"dry-run\0{identity}".encode())
        artifact["digest_ref"] = target.digest_ref(str(artifact["artifact_digest"]))
        return artifact

    if publisher is None:
        raise RuntimeError("Skopeo publisher is unavailable outside dry-run mode")
    lock_path, record_path, log_path = image_build_state_paths(
        cache_root, target.repository, tag, bundle.task_identity
    )
    with exclusive_image_build_lock(lock_path):
        inspected = None if args.force else registry.manifest(tag) if registry else None
        if inspected is not None:
            log(f"registry cache hit service={service.name}: {tag_ref}")
            existing_record = read_json_record(record_path)
            artifact["artifact_digest"] = inspected["artifact_digest"]
            artifact["digest_ref"] = target.digest_ref(inspected["artifact_digest"])
            artifact["media_type"] = inspected["media_type"]
            artifact["config"] = publisher.inspect_config(tag_ref)
            artifact["config_resolved"] = True
            atomic_write_json(
                record_path,
                {
                    **existing_record,
                    "input_hash": input_hash,
                    "platform": args.platform,
                    "tag": tag,
                    "tag_ref": tag_ref,
                    "artifact_digest": inspected["artifact_digest"],
                    "digest_ref": artifact["digest_ref"],
                    "source": existing_record.get("source", image_source),
                    "last_resolution": "registry-cache",
                    "transport": _transport_record(
                        dockerhub_mirror_prefix=args.dockerhub_mirror_prefix,
                        download_source_url=args.download_source_url,
                        package_build_args=args.package_build_args,
                        github_mirror_url=args.github_mirror_url,
                        rustup_init_url=args.rustup_init_url,
                        pytorch_index_url=args.pytorch_index_url,
                    ),
                    "build_arg_names": sorted(declared_build_args),
                    "service": service.name,
                    "task_dir": str(bundle.task_dir),
                    "last_resolved_at": datetime.now(timezone.utc).isoformat(),
                },
            )
            return artifact

        local_image_config: dict[str, object] | None = None
        cache_root.mkdir(parents=True, exist_ok=True)
        if service.build is None:
            if not resolved_external_ref:
                raise RuntimeError("external service resolved without an image ref")
            inspected = publisher.copy(resolved_external_ref, tag_ref)
        else:
            log(
                f"building task={bundle.task_identity} service={service.name} "
                f"platform={args.platform}; log={log_path}"
            )
            with build_image(
                context_dir=service.build.context_dir,
                dockerfile=service.build.dockerfile,
                temporary_root=cache_root,
                log_path=log_path,
                platform=args.platform,
                timeout_sec=getattr(args, "build_timeout_sec", None)
                or load_build_timeout(bundle.task_dir),
                build_args={**proxy_args, **effective_service_build_args},
                package_build_args=args.package_build_args,
                dockerhub_mirror_prefix=args.dockerhub_mirror_prefix,
                base_image_registry=args.base_image_registry,
                download_source_url=args.download_source_url,
                github_mirror_url=args.github_mirror_url,
                rustup_init_url=args.rustup_init_url,
                pytorch_index_url=args.pytorch_index_url,
                target=service.build.target,
                no_cache=getattr(args, "no_cache", False),
                build_network=getattr(args, "build_network", "default"),
            ) as built:
                local_image_config = built.config
                log(f"publishing service={service.name}: {tag_ref}")
                inspected = publisher.copy(
                    str(built.archive_path), tag_ref, source_is_archive=True
                )
        artifact["artifact_digest"] = inspected["artifact_digest"]
        artifact["digest_ref"] = target.digest_ref(inspected["artifact_digest"])
        artifact["media_type"] = inspected["media_type"]
        resolution = "built-and-pushed"
        artifact["config"] = local_image_config or publisher.inspect_config(tag_ref)
        artifact["config_resolved"] = True
        atomic_write_json(
            record_path,
            {
                "build_log": str(log_path),
                "input_hash": input_hash,
                "tag": tag,
                "tag_ref": tag_ref,
                "artifact_digest": artifact["artifact_digest"],
                "digest_ref": artifact["digest_ref"],
                "media_type": artifact["media_type"],
                "platform": args.platform,
                "proxy_configured": bool(proxy_args),
                "source": image_source,
                "source_manifest_digest": source_digest,
                "last_resolution": resolution,
                "transport": _transport_record(
                    dockerhub_mirror_prefix=args.dockerhub_mirror_prefix,
                    download_source_url=args.download_source_url,
                    package_build_args=args.package_build_args,
                    github_mirror_url=args.github_mirror_url,
                    rustup_init_url=args.rustup_init_url,
                    pytorch_index_url=args.pytorch_index_url,
                ),
                "build_arg_names": sorted(declared_build_args),
                "service": service.name,
                "task_dir": str(bundle.task_dir),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
        )
    log(f"ready service={service.name}: {artifact['digest_ref']}")
    return artifact
