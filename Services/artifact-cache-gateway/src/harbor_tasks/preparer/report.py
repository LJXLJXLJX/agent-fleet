from __future__ import annotations

import csv
import json
from pathlib import Path


def package_probe_markdown(report: dict[str, object]) -> str:
    summary = report.get("summary", {})
    by_status = summary.get("by_status", {}) if isinstance(summary, dict) else {}
    results = int(summary.get("results", 0)) if isinstance(summary, dict) else 0
    probed = int(summary.get("probed_results", 0)) if isinstance(summary, dict) else 0
    unsupported_count = (
        int(summary.get("unsupported_results", 0))
        if isinstance(summary, dict)
        else 0
    )
    coverage = (
        float(summary.get("probe_coverage_percent", 0.0))
        if isinstance(summary, dict)
        else 0.0
    )
    lines = [
        "# Dataset Package Domestic-Source Probe",
        "",
        f"Dataset: `{report.get('dataset', '')}`",
        "",
        ("Probing is enabled by default and explicitly disables ambient environment "
        "proxies. `slow` and `unavailable` are the inputs to `prepare`; `unsupported` "
        "means no provider is available yet and must not be treated as domestic-source "
        "reachable or already cached."),
        "",
        (f"Actual source probe coverage: **{probed:,}/{results:,} ({coverage:.2f}%)**; "
        f"unsupported: **{unsupported_count:,}**."),
        "",
        "| Status | Count |",
        "| --- | ---: |",
    ]
    for status in (
        "fast",
        "slow",
        "unavailable",
        "not-applicable",
        "unsupported",
    ):
        lines.append(f"| {status} | {int(by_status.get(status, 0)):,} |")
    lines += [
        "",
        "## Slow or Unavailable",
        "",
        "| Package manager | Package/requirement | Environment | Status | Elapsed (s) | Exact repository context | Reason |",
        "| --- | --- | --- | --- | ---: | --- | --- |",
    ]
    problems = report.get("problems", [])
    if isinstance(problems, list) and problems:
        for row in problems:
            if not isinstance(row, dict):
                continue
            elapsed = row.get("elapsed_seconds")
            elapsed_text = f"{float(elapsed):.3f}" if elapsed is not None else "-"
            raw_contexts = row.get("repository_contexts", [])
            repositories: list[str] = []
            if isinstance(raw_contexts, (list, tuple)):
                for context in raw_contexts:
                    if not isinstance(context, dict):
                        continue
                    components = context.get("components", [])
                    component_text = (
                        ",".join(str(item) for item in components)
                        if isinstance(components, list)
                        else ""
                    )
                    value = (
                        f"`{context.get('upstream_url')} "
                        f"{context.get('suite')} {component_text}`"
                    )
                    if value not in repositories:
                        repositories.append(value)
            lines.append(
                f"| {row.get('manager')} | `"
                f"{row.get('requirement') or row.get('package')}` | "
                f"`{row.get('environment') or '-'}` | {row.get('status')} | "
                f"{elapsed_text} | {'<br>'.join(repositories) or '-'} | "
                f"{row.get('reason')} |"
            )
    else:
        lines.append("| - | - | - | - | - | - | no slow or unavailable packages found |")
    lines += ["", "## Providers Not Yet Supported", ""]
    unsupported = report.get("unsupported", [])
    if isinstance(unsupported, list) and unsupported:
        lines += [
            "| Package manager | Package/requirement | Analysis environment | Consumer contexts | Reason |",
            "| --- | --- | --- | --- | --- |",
        ]
        for row in unsupported:
            if isinstance(row, dict):
                lines.append(
                    f"| {row.get('manager')} | `"
                    f"{row.get('requirement') or row.get('package')}` | `"
                    f"{row.get('environment_id') or '-'}` | `"
                    f"{', '.join(str(item) for item in row.get('build_contexts', [])) or '-'}"
                    f"` | {row.get('reason')} |"
                )
    else:
        lines.append("Every discovered package manager has a runnable provider.")
    return "\n".join(lines) + "\n"


def write_probe_report(directory: Path, report: dict[str, object]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "package-probe-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (directory / "package-probe-report.md").write_text(
        package_probe_markdown(report), encoding="utf-8"
    )
    results = report.get("results", [])
    fields = [
        "manager",
        "package",
        "environment",
        "environment_id",
        "build_contexts",
        "status",
        "reason",
        "requirement",
        "python_version",
        "implementation",
        "abi",
        "platform",
        "domestic_source",
        "domestic_url",
        "filename",
        "version",
        "sha256",
        "integrity",
        "shasum",
        "size",
        "elapsed_seconds",
        "bytes_sampled",
        "bytes_per_second",
        "repository_contexts",
        "consumer_environment_ids",
    ]
    with (directory / "package-probe-report.csv").open(
        "w", encoding="utf-8", newline=""
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        if isinstance(results, list):
            for row in results:
                if not isinstance(row, dict):
                    continue
                output = dict(row)
                output["build_contexts"] = json.dumps(
                    output.get("build_contexts", []), ensure_ascii=False
                )
                output["repository_contexts"] = json.dumps(
                    output.get("repository_contexts", []), ensure_ascii=False
                )
                output["consumer_environment_ids"] = json.dumps(
                    output.get("consumer_environment_ids", []),
                    ensure_ascii=False,
                )
                writer.writerow(output)


def write_warm_result(path: Path, result: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
