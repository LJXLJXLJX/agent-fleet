#!/usr/bin/env python3
"""Launch the real rollout HTTP handler with only zellij startup replaced."""

from __future__ import annotations

import argparse
import importlib.util
import os
import sys
from http.server import ThreadingHTTPServer
from pathlib import Path


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--max-trials-per-batch", type=int, default=2048)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    work_dir = args.work_dir.resolve()
    dataset_root = work_dir / "dataset"
    queue_root = work_dir / "queue"
    (dataset_root / "1").mkdir(parents=True, exist_ok=True)

    os.environ.update(
        {
            "RL_DATASET_NAME": "benchmark",
            "RL_DATASET_ROOT": str(dataset_root),
            "RL_DATASET_ROOTS": "",
            "RL_DISABLED_TASK_IDS": "",
            "RL_TRACE_LOG": str(work_dir / "requests.jsonl"),
            "RL_QUEUE_DIR": str(queue_root),
            "RL_ACTIVE_DIR": str(queue_root / "active"),
            "RL_JOB_QUEUE_ROOT": str(queue_root / "jobs"),
            "RL_JOB_RUNTIME_ROOT": str(work_dir / "runtime"),
            "RL_DYNAMIC_JOB_ZELLIJ": "0",
            "RL_ASYNC_TRIAL_BATCHES_ENABLED": "1",
            "RL_ASYNC_TRIAL_REGISTRY_PATH": str(work_dir / "registry.sqlite3"),
            "RL_ASYNC_MAX_TRIALS_PER_BATCH": str(args.max_trials_per_batch),
            "RL_REQUEST_TIMEOUT": "86400",
        }
    )

    script = Path(__file__).resolve().parents[2] / "rollout_remote_harbor.py"
    spec = importlib.util.spec_from_file_location("benchmark_rollout_remote_harbor", script)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load rollout server: {script}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    sys.path.insert(0, str(script.parent))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(script.parent))

    def synthetic_zellij(ray_job_id: str, dataset_name: str, queue_dir: Path) -> str:
        del dataset_name, queue_dir
        return f"benchmark-{ray_job_id}"

    module._ensure_job_zellij = synthetic_zellij
    server = ThreadingHTTPServer((args.host, args.port), module.Handler)
    print(
        f"benchmark rollout server pid={os.getpid()} address={args.host}:{args.port} "
        f"work_dir={work_dir}",
        flush=True,
    )
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
