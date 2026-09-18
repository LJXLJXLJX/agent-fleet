#!/usr/bin/env python3
"""Count images and packages in a Harbor benchmark framework dataset.

A task is a directory containing task.toml and environment/. The analyzer is
static: it never builds an image or runs an install command. Counts include:

* occurrences: explicit FROM/package specifications in build files;
* tasks: distinct tasks containing the image/package.

Dockerfiles and their referenced shell scripts are scanned by default. Pip
requirements referenced through -r/--requirement are expanded when resolvable.
"""


from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Sequence

from . import analyze
from .report import (
    markdown,
    write_outputs,
)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "dataset",
        type=Path,
        help="Harbor benchmark framework dataset root; symbolic links are accepted",
    )
    parser.add_argument("--shell-scope", choices=("referenced", "all", "none"), default="referenced")
    parser.add_argument("--top", type=int, default=50, help="rows per stdout table; 0 prints all")
    parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    parser.add_argument("--report", type=Path, help="write a complete Markdown report (all package rows)")
    parser.add_argument(
        "--output-dir", type=Path,
        help="write complete report.md, summary.json, images.csv and packages.csv",
    )
    parser.add_argument(
        "--probe-domestic-packages",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="probe all discovered packages using provider-specific domestic sources",
    )
    parser.add_argument("--package-probe-timeout", type=float, default=15.0)
    parser.add_argument("--package-probe-slow-seconds", type=float, default=3.0)
    parser.add_argument("--package-probe-sample-bytes", type=int, default=128 * 1024)
    parser.add_argument("--package-probe-concurrency", type=int, default=16)
    args = parser.parse_args(argv)
    if args.top < 0:
        parser.error("--top must be zero or positive")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    dataset = args.dataset.expanduser().resolve()
    if not dataset.is_dir():
        print(f"error: dataset directory does not exist: {dataset}", file=sys.stderr)
        return 2
    result = analyze(dataset, args.shell_scope)
    if result["summary"]["tasks"] == 0:
        print(
            f"error: no task.toml + environment task directories found below {dataset}",
            file=sys.stderr,
        )
        return 2
    if args.probe_domestic_packages:
        from ..preparer.models import ProbeSettings
        from ..preparer.orchestrator import probe_packages

        settings = ProbeSettings(
            timeout_seconds=args.package_probe_timeout,
            slow_seconds=args.package_probe_slow_seconds,
            sample_bytes=args.package_probe_sample_bytes,
            concurrency=args.package_probe_concurrency,
        )
        try:
            with tempfile.TemporaryDirectory(
                prefix="artifact-cache-probe-"
            ) as temporary:
                result["package_preparation"] = probe_packages(
                    result,
                    settings=settings,
                    work_dir=Path(temporary),
                )
        except (OSError, RuntimeError, ValueError) as exc:
            print(f"error: package probe failed: {exc}", file=sys.stderr)
            return 2
    if args.output_dir:
        write_outputs(args.output_dir.expanduser(), result)
    if args.report:
        report = args.report.expanduser()
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(markdown(result, 0), encoding="utf-8")
    if args.format == "json":
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(markdown(result, args.top), end="")
    return 0
