from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Sequence

from .models import ProbeResult, ProbeSettings, WarmResult
from .providers import AptProvider, NpmProvider, PipProvider
from .providers.base import PackageProvider


def _providers(
    configured: Sequence[PackageProvider] | None = None,
) -> dict[str, PackageProvider]:
    values = (
        list(configured)
        if configured is not None
        else [AptProvider(), NpmProvider(), PipProvider()]
    )
    return {provider.manager: provider for provider in values}


def probe_packages(
    analysis: dict[str, object],
    *,
    settings: ProbeSettings,
    work_dir: Path,
    providers: Sequence[PackageProvider] | None = None,
) -> dict[str, object]:
    """Probe every discovered package using its package-manager provider.

    Unsupported managers remain explicit report rows.  They are never treated as
    domestic-source successes and cannot silently disappear from preparation.
    """

    settings.validate()
    registry = _providers(providers)
    raw_packages = analysis.get("packages", [])
    package_rows = raw_packages if isinstance(raw_packages, list) else []
    by_manager: dict[str, list[dict[str, object]]] = {}
    for row in package_rows:
        if isinstance(row, dict):
            by_manager.setdefault(str(row.get("manager", "")), []).append(row)
    results: list[ProbeResult] = []
    for manager in sorted(by_manager):
        provider = registry.get(manager)
        if provider is None:
            for row in by_manager[manager]:
                package = str(row.get("package", ""))
                raw_contexts = row.get("contexts", [])
                contexts = (
                    [item for item in raw_contexts if isinstance(item, dict)]
                    if isinstance(raw_contexts, list)
                    else []
                )
                if not contexts:
                    contexts = [{}]
                for context in contexts:
                    raw_build_contexts = context.get("build_contexts", [])
                    results.append(
                        ProbeResult(
                            manager,
                            package,
                            None,
                            "unsupported",
                            f"no {manager} package preparation provider is implemented",
                            requirement=str(
                                context.get("requirement") or package
                            ),
                            environment_id=str(
                                context.get("environment_id") or ""
                            )
                            or None,
                            build_contexts=tuple(
                                str(item)
                                for item in raw_build_contexts
                                if isinstance(raw_build_contexts, (list, tuple))
                            ),
                        )
                    )
            continue
        results.extend(
            provider.probe(by_manager[manager], settings, work_dir=work_dir)
        )
    status_counts = Counter(row.status for row in results)
    manager_counts = Counter(row.manager for row in results)
    probed_results = sum(
        status_counts[status] for status in ("fast", "slow", "unavailable")
    )
    probe_coverage_percent = (
        round(probed_results * 100.0 / len(results), 2) if results else 0.0
    )
    return {
        "schema_version": 2,
        "kind": "artifact-cache-package-probe-report",
        "dataset": analysis.get("dataset"),
        "policy": {
            "default_enabled": True,
            "ambient_proxy_used": False,
            "timeout_seconds": settings.timeout_seconds,
            "slow_seconds": settings.slow_seconds,
            "sample_bytes": settings.sample_bytes,
            "concurrency": settings.concurrency,
            "problem_statuses": ["slow", "unavailable"],
        },
        "summary": {
            "results": len(results),
            "by_status": dict(sorted(status_counts.items())),
            "by_manager": dict(sorted(manager_counts.items())),
            "problem_results": sum(
                status_counts[status] for status in ("slow", "unavailable")
            ),
            "unsupported_results": status_counts["unsupported"],
            "probed_results": probed_results,
            "probe_coverage_percent": probe_coverage_percent,
        },
        "results": [row.to_dict() for row in results],
        "problems": [
            row.to_dict()
            for row in results
            if row.status in {"slow", "unavailable"}
        ],
        "unsupported": [
            row.to_dict() for row in results if row.status == "unsupported"
        ],
    }


def warm_packages(
    probe_report: dict[str, object],
    *,
    gateway_url: str,
    timeout_seconds: float,
    work_dir: Path,
    providers: Sequence[PackageProvider] | None = None,
) -> dict[str, object]:
    if timeout_seconds <= 0:
        raise ValueError("package warm timeout must be positive")
    registry = _providers(providers)
    raw_problems = probe_report.get("problems", [])
    problems = [row for row in raw_problems if isinstance(row, dict)] if isinstance(raw_problems, list) else []
    by_manager: dict[str, list[dict[str, object]]] = {}
    for row in problems:
        by_manager.setdefault(str(row.get("manager", "")), []).append(row)
    results: list[WarmResult] = []
    for manager in sorted(by_manager):
        provider = registry.get(manager)
        if provider is None:
            results.extend(
                WarmResult(
                    manager,
                    str(row.get("package", "")),
                    str(row.get("environment") or "") or None,
                    "not-actionable",
                    f"no {manager} package preparation provider is implemented",
                )
                for row in by_manager[manager]
            )
            continue
        results.extend(
            provider.warm(
                by_manager[manager],
                gateway_url=gateway_url,
                timeout_seconds=timeout_seconds,
                work_dir=work_dir,
            )
        )
    status_counts = Counter(row.status for row in results)
    return {
        "schema_version": 2,
        "kind": "artifact-cache-package-warm-result",
        "dataset": probe_report.get("dataset"),
        "gateway_url": gateway_url,
        "summary": {
            "results": len(results),
            "by_status": dict(sorted(status_counts.items())),
            "failures": status_counts["failed"],
            "not_actionable": status_counts["not-actionable"],
        },
        "results": [row.to_dict() for row in results],
    }
