#!/usr/bin/env python3
"""In-repository 32-trial smoke for the async submit boundary."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock


TEST_DIR = Path(__file__).resolve().parent
SERVER_SCRIPT = TEST_DIR.parent / "rollout_remote_harbor.py"
DRIVER_SCRIPT = TEST_DIR / "async_batch_submit_driver.py"
SPEC = importlib.util.spec_from_file_location("smoke_rollout_remote_harbor", SERVER_SCRIPT)
assert SPEC and SPEC.loader
SERVER_MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = SERVER_MODULE
sys.path.insert(0, str(SERVER_SCRIPT.parent))
try:
    SPEC.loader.exec_module(SERVER_MODULE)
finally:
    sys.path.remove(str(SERVER_SCRIPT.parent))


class AsyncSubmitSmokeTest(unittest.TestCase):
    def test_driver_submits_32_trials_and_finds_32_real_queue_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            dataset_root = root / "dataset"
            (dataset_root / "1").mkdir(parents=True)
            queue_root = root / "queue"
            job_queue_root = queue_root / "jobs"
            with (
                mock.patch.multiple(
                    SERVER_MODULE,
                    DEFAULT_DATASET_NAME="smoke-dataset",
                    DEFAULT_DATASET_ROOT=dataset_root,
                    DEFAULT_DISABLED_TASK_IDS="",
                    DEFAULT_API_KEY="synthetic-server-key",
                    TRACE_LOG=root / "requests.jsonl",
                    QUEUE_DIR=queue_root,
                    PENDING_DIR=queue_root / "pending",
                    ACTIVE_DIR=queue_root / "active",
                    RESULTS_DIR=queue_root / "results",
                    JOB_QUEUE_ROOT=job_queue_root,
                    JOB_RUNTIME_ROOT=root / "runtime",
                    ENABLE_DYNAMIC_JOB_ZELLIJ=False,
                    ENABLE_ASYNC_TRIAL_BATCHES=True,
                    ASYNC_TRIAL_REGISTRY_PATH=root / "registry.sqlite3",
                    ASYNC_MAX_TRIALS_PER_BATCH=64,
                    ASYNC_REGISTRY=None,
                ),
                mock.patch.dict(os.environ, {"RL_DATASET_ROOTS": ""}),
                mock.patch.object(
                    SERVER_MODULE,
                    "_ensure_submission_zellij",
                    return_value="synthetic-zellij-session",
                ) as ensure_zellij,
            ):
                server = ThreadingHTTPServer(("127.0.0.1", 0), SERVER_MODULE.Handler)
                server_thread = threading.Thread(target=server.serve_forever, daemon=True)
                server_thread.start()
                try:
                    completed = subprocess.run(
                        [
                            sys.executable,
                            str(DRIVER_SCRIPT),
                            "--harbor-url",
                            f"http://127.0.0.1:{server.server_port}",
                            "--dataset-name",
                            "smoke-dataset",
                            "--ray-submission-id",
                            "step-3-submit-smoke",
                            "--task-id",
                            "1",
                            "--trial-count",
                            "32",
                            "--queue-root",
                            str(job_queue_root),
                        ],
                        check=False,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        timeout=15,
                    )
                finally:
                    server.shutdown()
                    server.server_close()
                    server_thread.join(timeout=2)

            self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
            summary = json.loads(completed.stdout)
            self.assertEqual(summary["http_status"], 202)
            self.assertEqual(summary["requested_trials"], 32)
            self.assertEqual(summary["returned_trial_mappings"], 32)
            self.assertEqual(summary["queue_artifacts"], 32)
            self.assertEqual(summary["queue_states"], {"active": 0, "pending": 32, "results": 0})
            ensure_zellij.assert_called_once()


if __name__ == "__main__":
    unittest.main()
