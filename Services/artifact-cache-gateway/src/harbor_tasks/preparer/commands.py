"""prepare CLI core subcommands: analyze / mirror-images / prepare."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

from ...core.local_env import LocalEnvError
from ...gateway.services.image import (
    MirrorError,
    mirror_images,
)
from .orchestrator import warm_packages
from .report import write_warm_result
from .settings import (
    generate_analysis_plan,
    load_settings,
    resolved_authfile,
)


def analyze_command(args: argparse.Namespace) -> int:
    dataset = args.dataset.expanduser().resolve()

    try:
        settings = load_settings(args)
        analysis, plan_path, _plan = generate_analysis_plan(args, settings)
        summary = analysis["summary"]
        print(
            json.dumps(
                {
                    "dataset": str(dataset),
                    "tasks": summary["tasks"],
                    "unique_images": summary["unique_images"],
                    "image_mirror_plan": str(plan_path),
                    "apt_candidate_report": str(
                        plan_path.with_name("apt-cache-candidates.json")
                    ),
                    "apt_gateway_plan": str(
                        plan_path.with_name("apt-gateway-plan.json")
                    ),
                    "apt_cache_candidates": summary["apt_cache_candidate_urls"],
                    "apt_needs_validation": summary["apt_needs_validation_urls"],
                    "apt_packages_needing_resolution": summary[
                        "apt_packages_needing_resolution"
                    ],
                    "external_build_input_report": str(
                        plan_path.with_name("external-build-inputs.json")
                    ),
                    "download_gateway_plan": str(
                        plan_path.with_name("download-gateway-plan.json")
                    ),
                    "download_cache_candidates": summary[
                        "external_download_cache_candidate_urls"
                    ],
                    "git_mirror_candidates": summary[
                        "external_git_mirror_candidate_urls"
                    ],
                    "git_mirror_plan": str(
                        plan_path.with_name("git-mirror-plan.json")
                    ),
                    "package_probe_report": str(
                        plan_path.with_name("package-probe-report.json")
                    ),
                    "package_probe_problems": analysis["package_preparation"][
                        "summary"
                    ]["problem_results"],
                    "package_probe_unsupported": analysis["package_preparation"][
                        "summary"
                    ]["unsupported_results"],
                    "package_probe_probed": analysis["package_preparation"][
                        "summary"
                    ]["probed_results"],
                    "package_probe_coverage_percent": analysis[
                        "package_preparation"
                    ]["summary"]["probe_coverage_percent"],
                    "mode": "plan-only",
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0
    except (LocalEnvError, MirrorError, OSError, RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


def mirror_command(args: argparse.Namespace) -> int:
    plan_path = args.plan.expanduser().resolve()
    result_path = (
        args.output.expanduser().resolve()
        if args.output
        else plan_path.with_name("image-mirror-result.json")
    )
    try:
        settings = load_settings(args)
        with plan_path.open("r", encoding="utf-8") as handle:
            plan = json.load(handle)
        if not isinstance(plan, dict):
            raise MirrorError("image mirror plan must be a JSON object")
        target = plan.get("target")
        if not isinstance(target, dict) or target.get("registry") != settings["registry"]:
            raise MirrorError(
                "reviewed plan target Registry does not match project local env"
            )
        with resolved_authfile(args, settings) as authfile:
            result = mirror_images(
                plan,
                output_path=result_path,
                selection_path=plan_path.with_name("image-mirror-selection.json"),
                authfile=authfile,
                target_tls_verify=bool(settings["target_tls_verify"]),
                upstream_proxy=str(settings["upstream_proxy"])
                if settings["upstream_proxy"]
                else None,
                direct_upstream_with_proxy=bool(
                    settings["direct_upstream_with_proxy"]
                ),
                concurrency=int(settings["concurrency"]),
            )
        print(
            json.dumps(
                {
                    "image_mirror_result": str(result_path),
                    "already_present": sum(
                        row.get("status") == "already-present"
                        for row in result["images"]
                    ),
                    "uploaded": sum(
                        row.get("status") == "uploaded" for row in result["images"]
                    ),
                    "concurrency": settings["concurrency"],
                    "direct_upstream_with_proxy": settings[
                        "direct_upstream_with_proxy"
                    ],
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0
    except (
        json.JSONDecodeError,
        LocalEnvError,
        MirrorError,
        OSError,
        RuntimeError,
        ValueError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


def prepare_command(args: argparse.Namespace) -> int:
    dataset = args.dataset.expanduser().resolve()
    output_dir = args.output_dir.expanduser().resolve()
    try:
        settings = load_settings(args)
        analysis, plan_path, plan = generate_analysis_plan(args, settings)
        package_warm_result = None
        if args.warm_problem_packages:
            with tempfile.TemporaryDirectory(prefix="artifact-cache-warm-") as temporary:
                package_warm_result = warm_packages(
                    analysis["package_preparation"],
                    gateway_url=args.gateway_url,
                    timeout_seconds=args.package_warm_timeout,
                    work_dir=Path(temporary),
                )
            write_warm_result(
                output_dir / "package-warm-result.json", package_warm_result
            )
            if package_warm_result["summary"]["failures"]:
                raise MirrorError(
                    "one or more problem packages could not be warmed; see package-warm-result.json"
                )
        result_path = output_dir / "image-mirror-result.json"
        selection_path = output_dir / "image-mirror-selection.json"
        with resolved_authfile(args, settings) as authfile:
            result = mirror_images(
                plan,
                output_path=result_path,
                selection_path=selection_path,
                authfile=authfile,
                target_tls_verify=bool(settings["target_tls_verify"]),
                upstream_proxy=str(settings["upstream_proxy"])
                if settings["upstream_proxy"]
                else None,
                direct_upstream_with_proxy=bool(
                    settings["direct_upstream_with_proxy"]
                ),
                concurrency=int(settings["concurrency"]),
            )
        selection = json.loads(selection_path.read_text(encoding="utf-8"))
        statuses = [row.get("status") for row in result["images"]]
        print(
            json.dumps(
                {
                    "dataset": str(dataset),
                    "tasks": analysis["summary"]["tasks"],
                    "unique_images": analysis["summary"]["unique_images"],
                    "image_mirror_plan": str(plan_path),
                    "image_mirror_selection": str(selection_path),
                    "image_mirror_result": str(result_path),
                    "already_present": statuses.count("already-present"),
                    "missing_before_upload": len(selection["missing"]),
                    "uploaded": statuses.count("uploaded"),
                    "concurrency": settings["concurrency"],
                    "direct_upstream_with_proxy": settings[
                        "direct_upstream_with_proxy"
                    ],
                    "apt_candidate_report": str(
                        output_dir / "apt-cache-candidates.json"
                    ),
                    "apt_gateway_plan": str(output_dir / "apt-gateway-plan.json"),
                    "apt_cache_candidates": analysis["summary"][
                        "apt_cache_candidate_urls"
                    ],
                    "apt_needs_validation": analysis["summary"][
                        "apt_needs_validation_urls"
                    ],
                    "apt_packages_needing_resolution": analysis["summary"][
                        "apt_packages_needing_resolution"
                    ],
                    "external_build_input_report": str(
                        output_dir / "external-build-inputs.json"
                    ),
                    "download_gateway_plan": str(
                        output_dir / "download-gateway-plan.json"
                    ),
                    "download_cache_candidates": analysis["summary"][
                        "external_download_cache_candidate_urls"
                    ],
                    "git_mirror_candidates": analysis["summary"][
                        "external_git_mirror_candidate_urls"
                    ],
                    "git_mirror_plan": str(output_dir / "git-mirror-plan.json"),
                    "package_probe_report": str(
                        output_dir / "package-probe-report.json"
                    ),
                    "package_probe_problems": analysis["package_preparation"][
                        "summary"
                    ]["problem_results"],
                    "package_probe_unsupported": analysis["package_preparation"][
                        "summary"
                    ]["unsupported_results"],
                    "package_probe_probed": analysis["package_preparation"][
                        "summary"
                    ]["probed_results"],
                    "package_probe_coverage_percent": analysis[
                        "package_preparation"
                    ]["summary"]["probe_coverage_percent"],
                    "package_warm_result": (
                        str(output_dir / "package-warm-result.json")
                        if package_warm_result is not None
                        else None
                    ),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0
    except (
        json.JSONDecodeError,
        LocalEnvError,
        MirrorError,
        OSError,
        RuntimeError,
        ValueError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
