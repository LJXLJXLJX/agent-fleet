"""Analysis output: the markdown report and write_outputs persistence."""

from __future__ import annotations

import csv
import json
from pathlib import Path

from ...gateway.services.apt import compile_apt_gateway_plan
from ...gateway.services.git import compile_git_mirror_plan
from ..preparer.direct_download import compile_download_gateway_plan


def markdown(result: dict, top: int) -> str:
    summary = result["summary"]
    lines = [
        "# Harbor benchmark dataset dependency composition analysis",
        "",
        f"Dataset: `{result['dataset']}`",
        "",
        "## Overview",
        "",
        "| Metric | Value |",
        "| --- | ---: |",
        f"| Tasks | {summary['tasks']:,} |",
        f"| Dockerfiles | {summary['dockerfiles']:,} |",
        f"| Scanned build shell files | {summary['scanned_shell_files']:,} |",
        f"| Image occurrences | {summary['image_occurrences']:,} |",
        f"| Unique image references | {summary['unique_images']:,} |",
        f"| Build stage contexts | {summary['build_contexts']:,} |",
        f"| Dependency resolution environments | {summary['resolution_environments']:,} |",
        f"| Package occurrences | {summary['package_occurrences']:,} |",
        f"| Unique manager/package combinations | {summary['unique_manager_packages']:,} |",
        f"| APT external dependency URLs | {summary['apt_dependency_urls']:,} |",
        f"| APT cache-candidate URLs | {summary['apt_cache_candidate_urls']:,} |",
        f"| APT URLs requiring dynamic validation | {summary['apt_needs_validation_urls']:,} |",
        f"| APT packages requiring image-aware resolution | {summary['apt_packages_needing_resolution']:,} |",
        f"| Git/direct-download URLs | {summary['external_build_input_urls']:,} |",
        f"| Git/direct-download cache suggestions | {summary['external_cache_candidate_urls']:,} |",
        f"| Git/direct-download items needing review | {summary['external_needs_review_urls']:,} |",
        f"| Git/direct-download unresolved dynamic commands | {summary['external_unresolved_commands']:,} |",
        "",
        ("Counting rules: `occurrences` is the number of times an image or package is explicitly "
        "declared in build files, while `tasks` is the number of distinct tasks that include it. "
        "Repeated installs within one task only increase the occurrence count, never the task count."),
        "",
        "## Image statistics",
        "",
        "| Image | Occurrences | Tasks |",
        "| --- | ---: | ---: |",
    ]
    rows = result["images"] if top == 0 else result["images"][:top]
    lines.extend(f"| `{r['image']}` | {r['occurrences']} | {r['tasks']} |" for r in rows)

    apt = result["apt"]
    lines += [
        "",
        "## APT domestic mirror coverage monitoring",
        "",
        ("Static classification only treats official Ubuntu/Debian distribution repositories as "
        "replaceable by domestic mirrors. Third-party repositories, signing keys, and direct `.deb` "
        "artifacts are not covered by ordinary domestic distribution mirrors and must go through the "
        "Artifact Cache Gateway. Packages that appear only as names without an explicit source still "
        "require dynamic resolution based on the base image, distribution codename, and components; "
        "they are not reported here as covered. URL query parameters are stripped from the report so "
        "that transient tokens do not end up in the artifacts."),
        "",
        "### Must cache",
        "",
    ]
    if apt["cache_candidates"]:
        lines += [
            "| Type | URL | Occurrences | Tasks | Example task |",
            "| --- | --- | ---: | ---: | --- |",
        ]
        lines.extend(
            f"| {row['kind']} | `{row['url']}` | {row['occurrences']} | {row['tasks']} | "
            f"{', '.join('`' + str(example['task']) + '`' for example in row['examples'][:3]) or '-'} |"
            for row in apt["cache_candidates"]
        )
    else:
        lines.append("No explicit third-party APT repository, signing key, or direct `.deb` was found.")
    lines += ["", "### Awaiting dynamic validation", ""]
    if apt["needs_validation"]:
        lines += [
            "| Type | URL | Reason | Occurrences | Tasks |",
            "| --- | --- | --- | ---: | ---: |",
        ]
        lines.extend(
            f"| {row['kind']} | `{row['url']}` | {row['reason']} | "
            f"{row['occurrences']} | {row['tasks']} |"
            for row in apt["needs_validation"]
        )
    else:
        lines.append("No APT URL that could not be statically classified was found; APT package names still require later dynamic resolution.")

    external_inputs = result["external_inputs"]
    lines += [
        "",
        "## Git and direct-download build inputs",
        "",
        ("Only explicit `git clone`, `curl`, and `wget` commands are counted here. HTTP downloads are "
        "suited to a `frozen-download` source allowlisted by origin, refreshed manually; Git clones "
        "must use a Git-aware mirror and cannot be cached as a single file. Report URLs have their "
        "credentials and query strings removed; URLs containing shell expansion are flagged as "
        "`review` and are never auto-approved for a new origin."),
        "",
    ]
    if external_inputs["dependencies"]:
        lines += [
            "| Type | Tool | URL | ref | Suggestion | Occurrences | Tasks |",
            "| --- | --- | --- | --- | --- | ---: | ---: |",
        ]
        lines.extend(
            f"| {row['kind']} | {row['tool']} | `{row['url']}` | "
            f"`{row.get('reference') or '-'}` | {row['action']} | "
            f"{row['occurrences']} | {row['tasks']} |"
            for row in external_inputs["dependencies"]
        )
    else:
        lines.append("No explicit Git clone or curl/wget HTTP(S) download was found.")
    if external_inputs["unresolved"]:
        lines += [
            "",
            ("Dynamic targets record only classification counts and examples; commands and variable "
            "values are never persisted:"),
            "",
            "| Type | Tool | Reason | Occurrences | Tasks |",
            "| --- | --- | --- | ---: | ---: |",
        ]
        lines.extend(
            f"| {row['kind']} | {row['tool']} | {row['reason']} | "
            f"{row['occurrences']} | {row['tasks']} |"
            for row in external_inputs["unresolved"]
        )

    manager_rows = []
    for manager in sorted(summary["install_invocations_by_manager"]):
        manager_packages = [row for row in result["packages"] if row["manager"] == manager]
        manager_rows.append({
            "manager": manager,
            "commands": summary["install_invocations_by_manager"][manager],
            "occurrences": sum(row["occurrences"] for row in manager_packages),
            "unique": len(manager_packages),
        })
    lines += [
        "",
        "## Package manager summary",
        "",
        "| Package manager | Install commands | Package occurrences | Unique packages |",
        "| --- | ---: | ---: | ---: |",
    ]
    lines.extend(
        f"| {row['manager']} | {row['commands']} | {row['occurrences']} | {row['unique']} |"
        for row in manager_rows
    )
    lines += ["", "## Package statistics"]
    if top == 0:
        for manager in sorted(summary["install_invocations_by_manager"]):
            manager_packages = [row for row in result["packages"] if row["manager"] == manager]
            manager_packages.sort(key=lambda row: (-row["occurrences"], row["package"]))
            lines += [
                "",
                f"### {manager}",
                "",
                "| Package | Occurrences | Tasks |",
                "| --- | ---: | ---: |",
            ]
            lines.extend(
                f"| `{row['package']}` | {row['occurrences']} | {row['tasks']} |"
                for row in manager_packages
            )
    else:
        lines += [
            "",
            f"The following are the {top} most frequent manager/package combinations across the dataset.",
            "",
            "| Package manager | Package | Occurrences | Tasks |",
            "| --- | --- | ---: | ---: |",
        ]
        rows = result["packages"][:top]
        lines.extend(
            f"| {row['manager']} | `{row['package']}` | {row['occurrences']} | {row['tasks']} |"
            for row in rows
        )
    if top and len(result["packages"]) > top:
        lines += [
            "",
            (f"Showing the top {top} of {len(result['packages'])} manager/package combinations; "
            "use `--top 0` to output all records."),
        ]
    if summary["unresolved_requirement_files"]:
        lines += [
            "",
            "## Unresolved requirements files",
            "",
            "| Task | Source file | Requirements path |",
            "| --- | --- | --- |",
        ]
        lines.extend(
            f"| `{row['task']}` | `{row['source']}` | `{row['requirement']}` |"
            for row in result["unresolved_requirements"]
        )
    lines += [
        "",
        "## Analysis method and limitations",
        "",
        "- Scans every task directory that contains both `task.toml` and an `environment/` directory.",
        ("- Analyzes the `FROM` and `RUN` instructions in each Dockerfile and binds install commands "
        "to the specific Dockerfile stage; referenced build shell scripts inherit the stage of the "
        "instruction that references them."),
        ("- Expands readable pip requirements files, as well as `requirements.txt` and `package.json` "
        "dynamically generated via heredoc."),
        ("- Recognizes explicit `git clone` and static `curl`/`wget` HTTP(S) URLs; it never guesses "
        "variables or URLs assembled at script runtime. Git mirrors cover all refs of a repository; "
        "the plan is used for warming, submodule discovery, and coverage statistics, not as the "
        "allowlist for live request routing."),
        ("- This tool never actually builds images. Indirect dependencies that are selected "
        "dynamically by an arbitrary program, exist only in a lockfile, or depend on variables that "
        "cannot be resolved may not be attributed accurately by static analysis."),
    ]
    return "\n".join(lines) + "\n"


def write_outputs(directory: Path, result: dict) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "report.md").write_text(markdown(result, 0), encoding="utf-8")
    (directory / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    apt_report = {
        "schema_version": 2,
        "kind": "artifact-cache-apt-candidate-report",
        "dataset": result["dataset"],
        "classification": result["apt"]["classification"],
        "dependencies": result["apt"]["dependencies"],
        "cache_candidates": result["apt"]["cache_candidates"],
        "needs_validation": result["apt"]["needs_validation"],
        "packages_needing_resolution": result["apt"]["packages"],
    }
    (directory / "apt-cache-candidates.json").write_text(
        json.dumps(apt_report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (directory / "apt-gateway-plan.json").write_text(
        json.dumps(
            compile_apt_gateway_plan(apt_report), ensure_ascii=False, indent=2
        )
        + "\n",
        encoding="utf-8",
    )
    external_report = {
        "schema_version": 1,
        "kind": "artifact-cache-external-build-input-report",
        "dataset": result["dataset"],
        **result["external_inputs"],
    }
    (directory / "external-build-inputs.json").write_text(
        json.dumps(external_report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    download_plan = compile_download_gateway_plan(external_report)
    (directory / "download-gateway-plan.json").write_text(
        json.dumps(download_plan, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    git_plan = compile_git_mirror_plan(external_report)
    (directory / "git-mirror-plan.json").write_text(
        json.dumps(git_plan, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    with (directory / "external-build-inputs.csv").open(
        "w", encoding="utf-8", newline=""
    ) as handle:
        fields = [
            "url",
            "host",
            "kind",
            "tool",
            "action",
            "cache_mode",
            "refresh_policy",
            "reason",
            "query_present",
            "credentials_present",
            "reference",
            "references",
            "occurrences",
            "tasks",
            "examples",
        ]
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in result["external_inputs"]["dependencies"]:
            output = dict(row)
            output["references"] = json.dumps(
                output["references"], ensure_ascii=False
            )
            output["examples"] = json.dumps(
                output["examples"], ensure_ascii=False
            )
            writer.writerow(output)
    with (directory / "images.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["image", "occurrences", "tasks"])
        writer.writeheader(); writer.writerows(result["images"])
    with (directory / "packages.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["manager", "package", "occurrences", "tasks", "specs", "images", "contexts", "repository_contexts"])
        writer.writeheader()
        for row in result["packages"]:
            output = dict(row); output["specs"] = json.dumps(output["specs"], ensure_ascii=False, sort_keys=True)
            output["images"] = json.dumps(output["images"], ensure_ascii=False)
            output["contexts"] = json.dumps(
                output["contexts"], ensure_ascii=False
            )
            output["repository_contexts"] = json.dumps(
                output["repository_contexts"], ensure_ascii=False
            )
            writer.writerow(output)
    package_preparation = result.get("package_preparation")
    if isinstance(package_preparation, dict):
        from ..preparer.report import write_probe_report

        write_probe_report(directory, package_preparation)
    with (directory / "apt-cache-candidates.csv").open(
        "w", encoding="utf-8", newline=""
    ) as handle:
        fields = [
            "url", "host", "kind", "coverage", "action", "cache_mode", "reason",
            "query_present", "occurrences", "tasks", "packages_in_same_tasks",
            "examples",
        ]
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in result["apt"]["cache_candidates"]:
            output = dict(row)
            output["packages_in_same_tasks"] = json.dumps(
                output["packages_in_same_tasks"], ensure_ascii=False
            )
            output["examples"] = json.dumps(output["examples"], ensure_ascii=False)
            writer.writerow(output)
