#!/usr/bin/env python3
"""Characterization tests for the RL rollout Harbor HTTP service."""

from __future__ import annotations

import http.client
import importlib.util
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).parents[1] / "rollout_remote_harbor.py"
SPEC = importlib.util.spec_from_file_location("rollout_remote_harbor", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class RolloutRemoteHarborHTTPTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.dataset_root = self.root / "dataset"
        (self.dataset_root / "1").mkdir(parents=True)

        self.queue_root = self.root / "queue"
        self.job_queue_root = self.queue_root / "jobs"
        self.trace_log = self.root / "logs" / "requests.jsonl"
        self.module_patcher = mock.patch.multiple(
            MODULE,
            DEFAULT_DATASET_NAME="test-dataset",
            DEFAULT_DATASET_ROOT=self.dataset_root,
            DEFAULT_DISABLED_TASK_IDS="",
            DEFAULT_TIMEOUT=1.0,
            TRACE_LOG=self.trace_log,
            QUEUE_DIR=self.queue_root,
            PENDING_DIR=self.queue_root / "pending",
            ACTIVE_DIR=self.queue_root / "active",
            RESULTS_DIR=self.queue_root / "results",
            JOB_QUEUE_ROOT=self.job_queue_root,
            JOB_RUNTIME_ROOT=self.root / "runtime",
            ENABLE_DYNAMIC_JOB_ZELLIJ=False,
        )
        self.module_patcher.start()
        self.environment_patcher = mock.patch.dict(
            os.environ,
            {"RL_DATASET_ROOTS": "", "RL_API_BASE": "", "RL_AGENT": "claude-code"},
        )
        self.environment_patcher.start()
        self.zellij_patcher = mock.patch.object(
            MODULE,
            "_ensure_job_zellij",
            return_value="test-zellij-session",
        )
        self.ensure_job_zellij = self.zellij_patcher.start()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), MODULE.Handler)
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(timeout=2)
        self.zellij_patcher.stop()
        self.environment_patcher.stop()
        self.module_patcher.stop()
        self.temp_dir.cleanup()

    def _request(
        self,
        method: str,
        path: str,
        payload: object | None = None,
        *,
        raw_body: bytes | None = None,
    ) -> tuple[int, dict[str, object]]:
        body = raw_body
        headers: dict[str, str] = {}
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=3)
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            response_body = response.read()
            return response.status, json.loads(response_body.decode("utf-8"))
        finally:
            connection.close()

    def _valid_request(self, **overrides: object) -> dict[str, object]:
        request: dict[str, object] = {
            "request_id": "request-001",
            "session_id": "session-001",
            "task_id": "1",
            "ray_job_id": "ray-job-001",
            "request_timeout": 2,
        }
        request.update(overrides)
        return request

    def _wait_for_path(self, path: Path, timeout: float = 2.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if path.exists():
                return
            time.sleep(0.01)
        self.fail(f"timed out waiting for path: {path}")

    def test_health_reports_current_rollout_configuration(self) -> None:
        status, payload = self._request("GET", "/health")

        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["mode"], "rollout")
        self.assertEqual(payload["default_dataset"], "test-dataset")
        self.assertEqual(payload["dataset_roots"], {"test-dataset": str(self.dataset_root)})
        self.assertEqual(payload["queue_dir"], str(self.queue_root))
        self.assertEqual(payload["job_queue_root"], str(self.job_queue_root))
        self.assertFalse(payload["dynamic_job_zellij"])

    def test_unknown_get_and_post_paths_return_not_found(self) -> None:
        get_status, get_payload = self._request("GET", "/unknown")
        post_status, post_payload = self._request("POST", "/unknown", {})

        self.assertEqual((get_status, get_payload), (404, {"detail": "not found"}))
        self.assertEqual((post_status, post_payload), (404, {"detail": "not found"}))

    def test_run_trial_rejects_non_object_json(self) -> None:
        status, payload = self._request("POST", "/run_trial", [])

        self.assertEqual(status, 400)
        self.assertEqual(payload["detail"]["exception_type"], "ValueError")
        self.assertIn("JSON object", payload["detail"]["exception_message"])
        self.assertFalse(self.job_queue_root.exists())

    def test_run_trial_rejects_request_without_task(self) -> None:
        status, payload = self._request(
            "POST",
            "/run_trial",
            {"request_id": "missing-task", "ray_job_id": "ray-job-001"},
        )

        self.assertEqual(status, 400)
        self.assertEqual(payload["detail"]["exception_type"], "ValueError")
        self.assertIn("task_id or task_path is required", payload["detail"]["exception_message"])
        self.ensure_job_zellij.assert_not_called()

    def test_run_trial_waits_for_worker_result_and_returns_it_unchanged(self) -> None:
        response: dict[str, object] = {}

        def send_request() -> None:
            status, payload = self._request("POST", "/run_trial", self._valid_request())
            response["status"] = status
            response["payload"] = payload

        request_thread = threading.Thread(target=send_request)
        request_thread.start()

        job_queue = self.job_queue_root / "ray-job-001"
        pending_path = job_queue / "pending" / "request-001.json"
        result_path = job_queue / "results" / "request-001.json"
        self._wait_for_path(pending_path)

        expected_result = {
            "ok": True,
            "request_id": "request-001",
            "task_id": "1",
            "reward": 1.0,
            "result_path": "/tmp/trial/result.json",
        }
        try:
            self.assertTrue(request_thread.is_alive())
            self.assertEqual(list((job_queue / "pending").glob("*.json")), [pending_path])
            queued_request = json.loads(pending_path.read_text(encoding="utf-8"))
            self.assertEqual(queued_request["request_id"], "request-001")
            self.assertEqual(queued_request["session_id"], "session-001")
            self.assertEqual(queued_request["task_id"], "1")
            self.assertEqual(queued_request["task_path"], str((self.dataset_root / "1").resolve()))
            self.assertEqual(queued_request["queue_dir"], str(job_queue))
            self.assertEqual(queued_request["zellij_session"], "test-zellij-session")
        finally:
            result_path.write_text(json.dumps(expected_result), encoding="utf-8")
        request_thread.join(timeout=3)

        self.assertFalse(request_thread.is_alive())
        self.assertEqual(response, {"status": 200, "payload": expected_result})
        self.ensure_job_zellij.assert_called_once_with(
            "ray-job-001",
            "test-dataset",
            job_queue,
        )
        trace_events = [
            json.loads(line)
            for line in self.trace_log.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual([event["event"] for event in trace_events], ["queued", "returned"])

    def test_run_trial_timeout_uses_existing_internal_error_response(self) -> None:
        status, payload = self._request(
            "POST",
            "/run_trial",
            self._valid_request(request_id="request-timeout", request_timeout=0.01),
        )

        self.assertEqual(status, 500)
        self.assertEqual(payload["detail"]["exception_type"], "TimeoutError")
        self.assertIn("timed out waiting for rollout worker result", payload["detail"]["exception_message"])
        pending_path = self.job_queue_root / "ray-job-001" / "pending" / "request-timeout.json"
        self.assertTrue(pending_path.exists())


if __name__ == "__main__":
    unittest.main()
