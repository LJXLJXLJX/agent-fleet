#!/usr/bin/env python3
"""Run a reproducible case/batch-size benchmark matrix sequentially."""

from __future__ import annotations

import argparse
import json
import os
import platform
import socket
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4


SCRIPT_DIR = Path(__file__).resolve().parent
RUN_CASE_SCRIPT = SCRIPT_DIR / "run_case.py"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", action="append", type=Path, required=True)
    parser.add_argument("--batch-sizes", default="16,32,64")
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--modes", default="sync,async")
    parser.add_argument("--output-root", type=Path, default=SCRIPT_DIR / "results")
    parser.add_argument("--skip-metrics", action="store_true")
    return parser.parse_args()


def _atomic_write_json(path: Path, value: Any) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=True, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    temporary.replace(path)


def main() -> int:
    args = _parse_args()
    if args.repetitions <= 0:
        raise ValueError("--repetitions must be positive")
    batch_sizes = [int(item.strip()) for item in args.batch_sizes.split(",") if item.strip()]
    if not batch_sizes or any(size <= 0 for size in batch_sizes):
        raise ValueError("--batch-sizes must contain positive comma-separated integers")
    cases = [path.resolve() for path in args.case]
    for path in cases:
        if not path.is_file():
            raise FileNotFoundError(path)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    matrix_dir = args.output_root.resolve() / f"{timestamp}-matrix-{uuid4().hex[:6]}"
    matrix_dir.mkdir(parents=True)
    manifest_path = matrix_dir / "matrix.json"
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "running",
        "configuration": {
            "cases": [str(path) for path in cases],
            "batch_sizes": batch_sizes,
            "repetitions": args.repetitions,
            "modes": args.modes,
            "metrics_skipped": args.skip_metrics,
        },
        "environment": {
            "hostname": socket.gethostname(),
            "platform": platform.platform(),
            "python_version": platform.python_version(),
            "sqlite_version": sqlite3.sqlite_version,
            "cpu_count": os.cpu_count(),
        },
        "runs": [],
    }
    _atomic_write_json(manifest_path, manifest)

    for case_path in cases:
        for batch_size in batch_sizes:
            for repetition in range(1, args.repetitions + 1):
                run_record = {
                    "case": str(case_path),
                    "batch_size": batch_size,
                    "repetition": repetition,
                    "status": "running",
                    "started_at": datetime.now(timezone.utc).isoformat(),
                }
                manifest["runs"].append(run_record)
                _atomic_write_json(manifest_path, manifest)
                print(
                    f"matrix case={case_path.name} batch_size={batch_size} "
                    f"repetition={repetition}/{args.repetitions}",
                    flush=True,
                )
                before = {path for path in matrix_dir.iterdir() if path.is_dir()}
                command = [
                    sys.executable,
                    str(RUN_CASE_SCRIPT),
                    "--case",
                    str(case_path),
                    "--async-batch-size",
                    str(batch_size),
                    "--modes",
                    args.modes,
                    "--output-root",
                    str(matrix_dir),
                ]
                if args.skip_metrics:
                    command.append("--skip-metrics")
                completed = subprocess.run(command, check=False)
                created = sorted(
                    str(path)
                    for path in matrix_dir.iterdir()
                    if path.is_dir() and path not in before
                )
                run_record.update(
                    {
                        "finished_at": datetime.now(timezone.utc).isoformat(),
                        "exit_code": completed.returncode,
                        "result_directories": created,
                        "status": "passed" if completed.returncode == 0 else "failed",
                    }
                )
                _atomic_write_json(manifest_path, manifest)
                if completed.returncode != 0:
                    manifest["status"] = "failed"
                    manifest["finished_at"] = datetime.now(timezone.utc).isoformat()
                    _atomic_write_json(manifest_path, manifest)
                    print(f"matrix stopped after failed run; manifest={manifest_path}", file=sys.stderr)
                    return completed.returncode

    manifest["status"] = "passed"
    manifest["finished_at"] = datetime.now(timezone.utc).isoformat()
    _atomic_write_json(manifest_path, manifest)
    print(json.dumps({"matrix_dir": str(matrix_dir), "manifest": str(manifest_path)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
