"""Prepare a task Bundle from one or more service images."""

from __future__ import annotations

import argparse
import copy
from functools import cache
from pathlib import Path

if __package__ and __package__.startswith("Agents."):
    from ..compose_bundle import resolve_bundle_spec
else:
    from compose_bundle import resolve_bundle_spec


from .build_engine import (
    DEFAULT_PLATFORM,
    base_image_registry_host,
    normalize_base_image_registry,
    optional_package_source_urls,
    package_source_build_args,
    package_source_hosts,
    parse_build_args,
    proxy_build_args,
    validate_download_source_url,
    validate_github_mirror_url,
)
from .build_sources import select_build_sources
from .image_build_state import atomic_write_json
from .registry import (
    RegistryClient,
    RegistryTarget,
    SkopeoPublisher,
    check_task_repository,
    registry_credentials,
    validate_registry_host,
)
from .service_images import prepare_service_image
from .task_bundle.manifest import (
    PreparedBundle,
    assemble_bundle_manifest,
)
from .uploaded_bundle_cache import UploadedBundleCache


def resolve_task_dir(
    task_dir: Path | None, dataset_root: Path | None, include: str
) -> Path:
    if task_dir is not None:
        resolved = task_dir.resolve()
    elif dataset_root is not None:
        root = dataset_root.resolve()
        if (root / "task.toml").is_file():
            resolved = root
        else:
            task_names = [item.strip() for item in include.split(",") if item.strip()]
            if len(task_names) != 1:
                raise ValueError(
                    "automatic Harbor task image preparation requires exactly one "
                    "included task"
                )
            resolved = (root / task_names[0]).resolve()
    else:
        raise ValueError("provide --task-dir or --dataset-root")

    if not (resolved / "task.toml").is_file():
        raise ValueError(f"task.toml not found under {resolved}")
    return resolved


def prepare_task_images(args: argparse.Namespace) -> PreparedBundle:
    """Prepare a task once; propagate failures without changing sources and retrying."""
    return _prepare_task_images(copy.deepcopy(args))


def _prepare_task_images(args: argparse.Namespace) -> PreparedBundle:
    # Platform deliberately stays out of the content hash. If a real task ever
    # requires another architecture, isolate its images and local artifacts in
    # an architecture-specific Harbor Project/namespace before relaxing this
    # guard; never reuse or overwrite the default amd64 cache with --force.
    args.platform = getattr(args, "platform", DEFAULT_PLATFORM)
    if args.platform != DEFAULT_PLATFORM:
        raise NotImplementedError(
            f"Harbor task image platform {args.platform!r} is not implemented; "
            f"only {DEFAULT_PLATFORM!r} is currently supported"
        )
    args.registry = validate_registry_host(args.registry)
    task_dir = resolve_task_dir(args.task_dir, args.dataset_root, args.include)
    cache_root = args.cache_root.resolve()
    benchmark = args.benchmark_name or args.project
    target = RegistryTarget(
        registry=args.registry,
        project=args.project,
        task_repository=args.task_repository
        or check_task_repository(
            task_dir.name,
            maximum_length=255 - len(args.project) - 1,
        ),
    )
    reuse_local_upload = bool(getattr(args, "reuse_local_upload_cache", False))
    uploaded_cache = UploadedBundleCache(
        cache_root=cache_root,
        target=target,
        benchmark=benchmark,
        platform=args.platform,
        task_identity=task_dir.name,
    )
    configured_output = getattr(args, "bundle_manifest_output", None)

    @cache
    def load_bundle():
        return resolve_bundle_spec(task_dir)

    cached = uploaded_cache.try_restore(
        enabled=reuse_local_upload,
        force=args.force,
        dry_run=args.dry_run,
        skip_hash_verification=bool(getattr(args, "skip_hash_verification", False)),
        load_bundle=load_bundle,
        configured_output=configured_output,
    )
    if cached is not None:
        return cached
    bundle = load_bundle()
    args = select_build_sources(args)
    build_network = getattr(args, "build_network", "default")
    args.base_image_registry = normalize_base_image_registry(
        getattr(args, "base_image_registry", "")
    )
    args.download_source_url = validate_download_source_url(
        getattr(args, "download_source_url", ""), build_network
    )
    args.github_mirror_url = validate_github_mirror_url(
        getattr(args, "github_mirror_url", ""), build_network
    )
    args.package_build_args = package_source_build_args(args, build_network)
    args.rustup_init_url, args.pytorch_index_url = optional_package_source_urls(
        args, build_network
    )
    explicit_build_args = parse_build_args(args.build_args_json)
    publisher: SkopeoPublisher | None = None
    registry: RegistryClient | None = None
    proxy_args: dict[str, str] = {}
    if not args.dry_run:
        username, password = (
            registry_credentials(args.docker_config, args.registry)
            if reuse_local_upload or args.force
            else ("", "")
        )
        publisher = SkopeoPublisher(
            target,
            username,
            password,
            tls_verify=args.registry_tls_verify,
        )
        registry = RegistryClient(target, publisher)
        direct_hosts = package_source_hosts(
            args.package_build_args,
            args.github_mirror_url,
            args.rustup_init_url,
            args.pytorch_index_url,
            args.download_source_url,
        )
        base_registry_host = base_image_registry_host(args.base_image_registry)
        if base_registry_host:
            direct_hosts.add(base_registry_host)
        proxy_args = proxy_build_args(
            args.use_proxy,
            build_network,
            direct_hosts=direct_hosts,
        )

    artifacts: dict[str, dict[str, object]] = {}
    for name in sorted(bundle.services):
        artifacts[name] = prepare_service_image(
            service=bundle.services[name],
            bundle=bundle,
            args=args,
            explicit_build_args=explicit_build_args,
            proxy_args=proxy_args,
            cache_root=cache_root,
            target=target,
            publisher=publisher,
            registry=registry,
        )

    manifest = assemble_bundle_manifest(
        bundle,
        artifacts,
        benchmark=benchmark,
        registry_host=target.registry,
        project=target.project,
        task_repository=target.task_repository,
        repository=target.repository,
    )
    bundle_identity = str(manifest["bundle_identity"])

    if reuse_local_upload and not args.dry_run:
        uploaded_cache.store(manifest)
    manifest_path: Path | None = None
    if configured_output is not None:
        manifest_path = Path(configured_output).expanduser().resolve()
        atomic_write_json(manifest_path, manifest)
    elif not args.dry_run:
        manifest_path = (
            cache_root / "bundles" / f"{bundle_identity.removeprefix('sha256:')}.json"
        )
        atomic_write_json(manifest_path, manifest)

    main_image_ref = str(artifacts[bundle.main_service]["digest_ref"])
    prepared = PreparedBundle(
        main_image_ref=main_image_ref,
        manifest=manifest,
        manifest_path=manifest_path,
    )
    if publisher is not None:
        publisher.close()
    return prepared
