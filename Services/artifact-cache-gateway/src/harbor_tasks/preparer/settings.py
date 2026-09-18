"""Loading prepare CLI run settings and generating the analysis plan."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from ...core.local_env import load_local_env
from ...gateway.services.image import (
    MirrorError,
    Platform,
    atomic_write_json,
    build_image_plan,
    normalize_registry,
    parse_source_prefix_map,
    temporary_registry_authfile,
)
from ..analyzer import (
    analyze,
    write_outputs,
)
from ._defaults import (
    _DEFAULT_SOURCE_PREFIX_MAP_JSON,
)
from .models import ProbeSettings
from .orchestrator import probe_packages


def _boolean_setting(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise MirrorError(f"{name} must be a boolean")


def load_settings(args: argparse.Namespace) -> dict[str, object]:
    env_file = args.env_file.expanduser().resolve()
    if not env_file.is_file():
        raise MirrorError(f"project local env does not exist: {env_file}")
    load_local_env(env_file)

    registry = getattr(args, "registry", None) or os.environ.get(
        "ARTIFACT_MIRROR_REGISTRY"
    ) or os.environ.get("YICLOUD_HARBOR_HOST")
    if not registry:
        raise MirrorError(
            "target Registry is required via --registry, ARTIFACT_MIRROR_REGISTRY, "
            "or YICLOUD_HARBOR_HOST"
        )
    project = getattr(args, "project", None) or os.environ.get(
        "ARTIFACT_MIRROR_PROJECT", "public-mirror"
    )
    platform = getattr(args, "platform", None) or os.environ.get(
        "ARTIFACT_MIRROR_PLATFORM", "linux/amd64"
    )
    source_map_raw = getattr(args, "source_prefix_map_json", None) or os.environ.get(
        "ARTIFACT_MIRROR_SOURCE_PREFIX_MAP_JSON",
        _DEFAULT_SOURCE_PREFIX_MAP_JSON,
    )
    target_tls_verify = getattr(args, "target_tls_verify", None)
    if target_tls_verify is None:
        target_tls_verify = _boolean_setting(
            "ARTIFACT_MIRROR_TARGET_TLS_VERIFY", True
        )
    upstream_proxy = getattr(args, "upstream_proxy", None) or os.environ.get(
        "ARTIFACT_MIRROR_UPSTREAM_PROXY"
    )
    direct_upstream_with_proxy = getattr(
        args, "direct_upstream_with_proxy", None
    )
    if direct_upstream_with_proxy is None:
        direct_upstream_with_proxy = _boolean_setting(
            "ARTIFACT_MIRROR_DIRECT_UPSTREAM_WITH_PROXY", False
        )
    if direct_upstream_with_proxy and not upstream_proxy:
        raise MirrorError(
            "direct upstream fallback requires --upstream-proxy or "
            "ARTIFACT_MIRROR_UPSTREAM_PROXY"
        )
    concurrency_raw = getattr(args, "concurrency", None)
    if concurrency_raw is None:
        concurrency_raw = os.environ.get("ARTIFACT_MIRROR_CONCURRENCY", "4")
    try:
        concurrency = int(concurrency_raw)
    except (TypeError, ValueError) as exc:
        raise MirrorError("ARTIFACT_MIRROR_CONCURRENCY must be an integer") from exc
    if concurrency < 1 or concurrency > 32:
        raise MirrorError("image mirror concurrency must be between 1 and 32")
    return {
        "env_file": env_file,
        "registry": normalize_registry(registry),
        "project": project,
        "platform": Platform.parse(str(platform)),
        "source_prefix_map": parse_source_prefix_map(source_map_raw),
        "target_tls_verify": bool(target_tls_verify),
        "upstream_proxy": upstream_proxy,
        "direct_upstream_with_proxy": direct_upstream_with_proxy,
        "concurrency": concurrency,
        "username": os.environ.get("YICLOUD_HARBOR_USERNAME", ""),
        "password": os.environ.get("YICLOUD_HARBOR_PASSWORD", ""),
    }


@contextmanager
def resolved_authfile(
    args: argparse.Namespace, settings: dict[str, object]
) -> Iterator[Path]:
    configured = getattr(args, "authfile", None)
    if configured:
        path = configured.expanduser().resolve()
        if not path.is_file():
            raise MirrorError(f"authfile does not exist: {path}")
        yield path
        return
    with temporary_registry_authfile(
        str(settings["registry"]),
        str(settings["username"]),
        str(settings["password"]),
    ) as path:
        yield path


def generate_analysis_plan(
    args: argparse.Namespace, settings: dict[str, object]
) -> tuple[dict[str, object], Path, dict[str, object]]:
    dataset = args.dataset.expanduser().resolve()
    output_dir = args.output_dir.expanduser().resolve()
    if not dataset.is_dir():
        raise MirrorError(f"dataset directory does not exist: {dataset}")

    analysis = analyze(dataset, args.shell_scope)
    if analysis["summary"]["tasks"] == 0:
        raise MirrorError(
            f"no task.toml + environment task directories found below {dataset}"
        )
    if args.probe_domestic_packages:
        probe_settings = ProbeSettings(
            timeout_seconds=args.package_probe_timeout,
            slow_seconds=args.package_probe_slow_seconds,
            sample_bytes=args.package_probe_sample_bytes,
            concurrency=args.package_probe_concurrency,
        )
        with tempfile.TemporaryDirectory(prefix="artifact-cache-probe-") as temporary:
            probe_report = probe_packages(
                analysis,
                settings=probe_settings,
                work_dir=Path(temporary),
            )
        analysis["package_preparation"] = probe_report
    else:
        analysis["package_preparation"] = {
            "schema_version": 2,
            "kind": "artifact-cache-package-probe-report",
            "dataset": analysis["dataset"],
            "enabled": False,
            "summary": {
                "results": 0,
                "by_status": {},
                "by_manager": {},
                "problem_results": 0,
                "unsupported_results": 0,
                "probed_results": 0,
                "probe_coverage_percent": 0.0,
            },
            "results": [],
            "problems": [],
            "unsupported": [],
        }
    write_outputs(output_dir, analysis)

    # Treat summary.json as the boundary between dataset analysis and image-only
    # preparation.  This keeps the one-click path identical to the reviewable,
    # two-step workflow instead of relying on a private in-memory representation.
    analysis_path = output_dir / "summary.json"
    with analysis_path.open("r", encoding="utf-8") as handle:
        persisted_analysis = json.load(handle)
    if not isinstance(persisted_analysis, dict):
        raise MirrorError("dataset summary.json must contain a JSON object")
    plan = build_image_plan(
        persisted_analysis,
        registry=str(settings["registry"]),
        project=str(settings["project"]),
        platform=settings["platform"],
        source_prefix_map=settings["source_prefix_map"],
    )
    plan_path = output_dir / "image-mirror-plan.json"
    atomic_write_json(plan_path, plan)
    return persisted_analysis, plan_path, plan
