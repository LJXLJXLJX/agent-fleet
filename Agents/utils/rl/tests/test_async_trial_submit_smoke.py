#!/usr/bin/env python3
"""In-repository 32-trial smoke for async submit and handle recovery."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock


TEST_DIR = Path(__file__).resolve().parent
SERVER_SCRIPT = TEST_DIR.parent / "rollout_remote_harbor.py"
DRIVER_SCRIPT = TEST_DIR / "async_batch_submit_driver.py"
SUMMARY_SCRIPT = TEST_DIR / "async_trace_summary.py"
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
                trace_completed: subprocess.CompletedProcess[str] | None = None
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
                    if completed.returncode == 0:
                        driver_summary = json.loads(completed.stdout)
                        batch_id = str(driver_summary["batch_id"])
                        job_queue = job_queue_root / "step-3-submit-smoke"
                        active_dir = job_queue / "active"
                        results_dir = job_queue / "results"
                        for pending in sorted((job_queue / "pending").glob("*.json")):
                            pending.replace(active_dir / pending.name)
                        with urllib.request.urlopen(
                            f"http://127.0.0.1:{server.server_port}/async_trial_batches/{batch_id}",
                            timeout=3,
                        ) as response:
                            response.read()
                        time.sleep(0.01)
                        for active in sorted(active_dir.glob("*.json")):
                            result_path = results_dir / active.name
                            result_path.write_text(
                                json.dumps({"ok": True, "reward": 1.0}),
                                encoding="utf-8",
                            )
                            active.unlink()
                        with urllib.request.urlopen(
                            f"http://127.0.0.1:{server.server_port}/async_trial_batches/{batch_id}/results",
                            timeout=3,
                        ) as response:
                            response.read()
                        trace_completed = subprocess.run(
                            [
                                sys.executable,
                                str(SUMMARY_SCRIPT),
                                "--trace-log",
                                str(root / "requests.jsonl"),
                                "--batch-id",
                                batch_id,
                                "--expected-trials",
                                "32",
                            ],
                            check=False,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            text=True,
                            timeout=10,
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
            self.assertEqual(summary["snapshot_http_status"], 200)
            self.assertEqual(summary["snapshot_state"], "QUEUED")
            self.assertEqual(summary["snapshot_requested_trials"], 32)
            self.assertGreaterEqual(summary["state_recovery_ms"], 0)
            self.assertIsNotNone(trace_completed)
            self.assertEqual(
                trace_completed.returncode,
                0,
                trace_completed.stderr or trace_completed.stdout,
            )
            trace_summary = json.loads(trace_completed.stdout)
            self.assertEqual(trace_summary["correlated_trials"], 32)
            self.assertEqual(trace_summary["queue_latency_ms"]["count"], 32)
            self.assertEqual(trace_summary["run_latency_ms"]["count"], 32)
            self.assertEqual(
                trace_summary["run_latency_ms"]["source"],
                "control_plane_observed",
            )
            self.assertEqual(trace_summary["state_transition_counts"]["RUNNING"], 32)
            self.assertEqual(trace_summary["state_transition_counts"]["SUCCEEDED"], 32)
            if os.environ.get("RL_TEST_REPORT_METRICS") == "1":
                print(
                    "async handle recovery: "
                    f"state_recovery_ms={summary['state_recovery_ms']} unresolved=0 "
                    f"trace_summary={json.dumps(trace_summary, sort_keys=True)}"
                )
            ensure_zellij.assert_called_once()


if __name__ == "__main__":
    unittest.main()
