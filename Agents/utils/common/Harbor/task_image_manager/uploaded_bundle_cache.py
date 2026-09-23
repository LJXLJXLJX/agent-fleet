"""Local uploaded-Bundle index, validation, and restoration."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

if __package__ and __package__.startswith("Agents."):
    from ..compose_bundle import (
        BUNDLE_FORMAT_VERSION,
        BUNDLE_SCHEMA_VERSION,
        BundleSpec,
    )
else:
    from compose_bundle import (
        BUNDLE_FORMAT_VERSION,
        BUNDLE_SCHEMA_VERSION,
        BundleSpec,
    )

from .image_build_state import atomic_write_json, read_json_record
from .registry import RegistryTarget, log
from .task_bundle.manifest import PreparedBundle

LOCAL_UPLOAD_INDEX_VERSION = 2


@dataclass(frozen=True)
class UploadedBundleCache:
    """Own one task's uploaded-Bundle entry and its reuse policy."""

    cache_root: Path
    target: RegistryTarget
    benchmark: str
    platform: str
    task_identity: str

    @property
    def _path(self) -> Path:
        return local_upload_manifest_path(
            self.cache_root,
            self.target,
            benchmark=self.benchmark,
            platform=self.platform,
        )

    def try_restore(
        self,
        *,
        enabled: bool,
        force: bool,
        dry_run: bool,
        skip_hash_verification: bool,
        load_bundle: Callable[[], BundleSpec],
        configured_output: Path | None = None,
    ) -> PreparedBundle | None:
        """Return a validated result or None; load task inputs only if needed.

        Invalid/missing entries are misses. Task parsing or hash failures are
        propagated: they must not silently turn into trusted cache hits.
        """
        if not enabled or force or dry_run:
            return None
        path = self._path
        manifest = read_json_record(path)
        if not manifest:
            return None
        matches = _cached_bundle_matches_target(
            manifest,
            target=self.target,
            benchmark=self.benchmark,
            task_identity=self.task_identity,
            platform=self.platform,
        )
        if matches and not skip_hash_verification:
            matches = _cached_bundle_matches_content(manifest, load_bundle())
        if not matches:
            log(
                "local uploaded-Bundle cache miss "
                f"task={self.task_identity}; falling back to Registry resolution"
            )
            return None
        verification = "skipped" if skip_hash_verification else "content-hash"
        log(
            "local uploaded-Bundle cache hit "
            f"task={self.task_identity} verification={verification}: {path}"
        )
        return _prepared_from_cached_bundle(
            manifest,
            cached_path=path,
            configured_output=configured_output,
        )

    def store(self, manifest: dict[str, object]) -> None:
        """Persist a Bundle after all service images have been published/resolved."""
        atomic_write_json(self._path, manifest)


def local_upload_manifest_path(
    cache_root: Path,
    target: RegistryTarget,
    *,
    benchmark: str,
    platform: str,
) -> Path:
    """Return the persistent, target-scoped uploaded-Bundle index entry."""
    scope = json.dumps(
        {
            "benchmark": benchmark,
            "platform": platform,
            "project": target.project,
            "registry": target.registry,
            "version": LOCAL_UPLOAD_INDEX_VERSION,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    scope_key = hashlib.sha256(scope).hexdigest()[:20]
    return (
        cache_root / "uploaded-bundles" / scope_key / f"{target.task_repository}.json"
    )


def _cached_bundle_matches_target(
    manifest: dict[str, object],
    *,
    target: RegistryTarget,
    benchmark: str,
    task_identity: str,
    platform: str,
) -> bool:
    registry = manifest.get("registry")
    benchmark_record = manifest.get("benchmark")
    services = manifest.get("services")
    main = manifest.get("main")
    if (
        manifest.get("schema_version") != BUNDLE_SCHEMA_VERSION
        or manifest.get("bundle_format") != BUNDLE_FORMAT_VERSION
        or manifest.get("task_identity") != task_identity
        or not isinstance(registry, dict)
        or registry.get("host") != target.registry
        or registry.get("project") != target.project
        or registry.get("task_repository") != target.task_repository
        or registry.get("repository") != target.repository
        or not isinstance(benchmark_record, dict)
        or benchmark_record.get("name") != benchmark
        or not isinstance(services, dict)
        or not services
        or not isinstance(main, str)
        or main not in services
    ):
        return False

    for service_record in services.values():
        if not isinstance(service_record, dict):
            return False
        image = service_record.get("image")
        if not isinstance(image, dict) or image.get("platform") != platform:
            return False
        artifact_digest = image.get("artifact_digest")
        input_hash = image.get("input_hash")
        if (
            not isinstance(artifact_digest, str)
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", artifact_digest)
            or image.get("digest_ref") != target.digest_ref(artifact_digest)
            or not isinstance(input_hash, str)
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", input_hash)
        ):
            return False
    return True


def _cached_bundle_matches_content(
    manifest: dict[str, object], bundle: BundleSpec
) -> bool:
    # Load the Harbor hashing dependency only when content validation is needed.
    from .task_image_identity import image_identity

    if manifest.get("definition_identity") != f"sha256:{bundle.definition_identity}":
        return False
    services = manifest.get("services")
    if not isinstance(services, dict) or set(services) != set(bundle.services):
        return False
    for name, service in bundle.services.items():
        service_record = services.get(name)
        if not isinstance(service_record, dict):
            return False
        image = service_record.get("image")
        if not isinstance(image, dict):
            return False
        if service.build is not None:
            # P0 invariant: cache identity comes only from the original static
            # task environment, never from rendered or runtime-mutated inputs.
            expected_hash = "sha256:" + image_identity(bundle.environment_dir)
        else:
            expected_hash = "sha256:" + image_identity(
                bundle.environment_dir,
                docker_image=service.source_image,
            )
        if image.get("input_hash") != expected_hash:
            return False
    return True


def _prepared_from_cached_bundle(
    manifest: dict[str, object],
    *,
    cached_path: Path,
    configured_output: Path | None,
) -> PreparedBundle:
    manifest_path = cached_path
    if configured_output is not None:
        manifest_path = Path(configured_output).expanduser().resolve()
        atomic_write_json(manifest_path, manifest)
    services = manifest.get("services")
    main = manifest.get("main")
    if not isinstance(services, dict) or not isinstance(main, str):
        raise TypeError(f"invalid local uploaded-Bundle entry: {cached_path}")
    main_record = services.get(main)
    image = main_record.get("image") if isinstance(main_record, dict) else None
    if not isinstance(image, dict):
        raise TypeError(f"invalid local uploaded-Bundle main service: {cached_path}")
    return PreparedBundle(
        main_image_ref=str(image["digest_ref"]),
        manifest=manifest,
        manifest_path=manifest_path,
    )
