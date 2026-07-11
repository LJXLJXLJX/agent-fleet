#!/usr/bin/env python3
"""Characterization tests for the RL rollout Harbor HTTP service."""

from __future__ import annotations

import http.client
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).parents[1] / "rollout_remote_harbor.py"
SPEC = importlib.util.spec_from_file_location("rollout_remote_harbor", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
sys.path.insert(0, str(SCRIPT.parent))
try:
    SPEC.loader.exec_module(MODULE)
finally:
    sys.path.remove(str(SCRIPT.parent))


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
            DEFAULT_API_KEY="server-api-key",
            DEFAULT_API_KEY_MODE="static",
            TRACE_LOG=self.trace_log,
            QUEUE_DIR=self.queue_root,
            PENDING_DIR=self.queue_root / "pending",
            ACTIVE_DIR=self.queue_root / "active",
            RESULTS_DIR=self.queue_root / "results",
            JOB_QUEUE_ROOT=self.job_queue_root,
            JOB_RUNTIME_ROOT=self.root / "runtime",
            ENABLE_DYNAMIC_JOB_ZELLIJ=False,
            ENABLE_ASYNC_TRIAL_BATCHES=True,
            ASYNC_TRIAL_REGISTRY_PATH=self.root / "async-trial-registry.sqlite3",
            ASYNC_MAX_TRIALS_PER_BATCH=8,
            ASYNC_MAX_REQUEST_BYTES=4096,
            ASYNC_MAX_BULK_STATUS_IDS=3,
            ASYNC_REGISTRY=None,
            ASYNC_METRICS=MODULE.AsyncControlPlaneMetrics(),
        )
        self.module_patcher.start()
        self.environment_patcher = mock.patch.dict(
            os.environ,
            {"RL_DATASET_ROOTS": "", "RL_API_BASE": "", "RL_AGENT": "claude-code"},
        )
        self.environment_patcher.start()
        self.zellij_patcher = mock.patch.object(
            MODULE,
            "_ensure_submission_zellij",
            return_value="test-zellij-session",
        )
        self.ensure_submission_zellij = self.zellij_patcher.start()

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
            "ray_submission_id": "ray-job-001",
            "request_timeout": 2,
        }
        request.update(overrides)
        return request

    def _valid_async_request(
        self,
        *,
        request_id: str = "async-request-001",
        trial_count: int = 2,
    ) -> dict[str, object]:
        return {
            "request_id": request_id,
            "client_batch_id": f"client-{request_id}",
            "trainer_run_id": "trainer-run-001",
            "batching_key": {
                "dataset_name": "test-dataset",
                "ray_submission_id": "ray-job-async",
                "policy_version": "policy-0",
            },
            "trials": [
                {
                    "client_trial_id": f"client-trial-{index}",
                    "session_id": f"session-{index}",
                    "task_id": "1",
                    "group_id": index,
                    "rollout_step": 0,
                    "policy_version": "policy-0",
                    "payload": {},
                }
                for index in range(trial_count)
            ],
        }

    def _wait_for_path(self, path: Path, timeout: float = 2.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if path.exists():
                return
            time.sleep(0.01)
        self.fail(f"timed out waiting for path: {path}")

    def _async_queue_path(
        self,
        response: dict[str, object],
        trial_index: int,
        state: str,
    ) -> Path:
        trial = response["trials"][trial_index]
        return (
            self.job_queue_root
            / "ray-job-async"
            / state
            / f"{trial['trial_execution_id']}.json"
        )

    def _trace_events(self) -> list[dict[str, object]]:
        if not self.trace_log.exists():
            return []
        return [
            json.loads(line)
            for line in self.trace_log.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

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
        self.assertEqual(payload["async_max_request_bytes"], 4096)
        self.assertEqual(payload["async_max_bulk_status_ids"], 3)
        control_plane = payload["async_control_plane"]
        self.assertTrue(control_plane["ready"])
        self.assertEqual(control_plane["registry"]["schema_version"], 2)
        self.assertEqual(control_plane["registry"]["journal_mode"], "wal")
        self.assertEqual(control_plane["registry"]["outstanding_batches"], 0)
        self.assertEqual(control_plane["queue_depth"], {"pending": 0, "active": 0})

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
            {"request_id": "missing-task", "ray_submission_id": "ray-job-001"},
        )

        self.assertEqual(status, 400)
        self.assertEqual(payload["detail"]["exception_type"], "ValueError")
        self.assertIn("task_id or task_path is required", payload["detail"]["exception_message"])
        self.ensure_submission_zellij.assert_not_called()

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
        self.ensure_submission_zellij.assert_called_once_with(
            "ray-job-001",
            "test-dataset",
            job_queue,
            MODULE.DEFAULT_MODEL_NAME,
            "ray-job-001",
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

    def test_async_single_trial_submit_returns_accepted_and_materializes_one_queue_item(self) -> None:
        request = self._valid_async_request(trial_count=1)
        request["trials"][0]["payload"]["api_key"] = "client-secret-must-not-be-durable"

        status, response = self._request("POST", "/async_trial_batches", request)

        self.assertEqual(status, 202)
        self.assertEqual(response["state"], "QUEUED")
        self.assertEqual(response["requested_trials"], 1)
        trial = response["trials"][0]
        pending_path = (
            self.job_queue_root
            / "ray-job-async"
            / "pending"
            / f"{trial['trial_execution_id']}.json"
        )
        self.assertTrue(pending_path.exists())
        queued = json.loads(pending_path.read_text(encoding="utf-8"))
        self.assertEqual(queued["request_id"], trial["trial_execution_id"])
        self.assertEqual(queued["batch_id"], response["batch_id"])
        self.assertEqual(queued["client_trial_id"], "client-trial-0")
        self.assertEqual(queued["session_id"], "session-0")
        self.assertEqual(queued["task_id"], "1")
        self.assertEqual(queued["metadata"]["group_id"], 0)
        self.assertEqual(queued["api_key"], "server-api-key")
        registry_files = MODULE.ASYNC_TRIAL_REGISTRY_PATH.parent.glob(
            f"{MODULE.ASYNC_TRIAL_REGISTRY_PATH.name}*"
        )
        self.assertNotIn(
            b"client-secret-must-not-be-durable",
            b"".join(path.read_bytes() for path in registry_files),
        )

    def test_async_multi_trial_retry_returns_original_mapping_without_duplicate_queue_items(self) -> None:
        request = self._valid_async_request(trial_count=3)

        first_status, first = self._request("POST", "/async_trial_batches", request)
        second_status, second = self._request("POST", "/async_trial_batches", request)

        self.assertEqual((first_status, second_status), (202, 202))
        self.assertEqual(second, first)
        pending_dir = self.job_queue_root / "ray-job-async" / "pending"
        self.assertEqual(len(list(pending_dir.glob("*.json"))), 3)
        self.ensure_submission_zellij.assert_called_once()

    def test_async_retry_recovers_handle_and_get_returns_compact_queued_snapshot(self) -> None:
        request = self._valid_async_request(trial_count=2)

        first_status, first = self._request("POST", "/async_trial_batches", request)
        retry_status, recovered = self._request("POST", "/async_trial_batches", request)
        get_status, snapshot = self._request(
            "GET",
            f"/async_trial_batches/{recovered['batch_id']}",
        )

        self.assertEqual((first_status, retry_status, get_status), (202, 202, 200))
        self.assertEqual(recovered, first)
        self.assertEqual(snapshot["batch_id"], first["batch_id"])
        self.assertEqual(snapshot["state"], "QUEUED")
        self.assertEqual(snapshot["revision"], 1)
        self.assertEqual(snapshot["queued_trials"], 2)
        self.assertNotIn("trials", snapshot)
        self.assertNotIn("batching_key", snapshot)

    def test_async_get_reconciles_worker_queue_states_and_is_idempotent(self) -> None:
        _, admission = self._request(
            "POST",
            "/async_trial_batches",
            self._valid_async_request(trial_count=2),
        )
        first_pending = self._async_queue_path(admission, 0, "pending")
        first_active = self._async_queue_path(admission, 0, "active")
        first_result = self._async_queue_path(admission, 0, "results")
        second_pending = self._async_queue_path(admission, 1, "pending")
        second_active = self._async_queue_path(admission, 1, "active")
        second_result = self._async_queue_path(admission, 1, "results")

        first_pending.replace(first_active)
        running_status, running = self._request(
            "GET",
            f"/async_trial_batches/{admission['batch_id']}",
        )
        registry = MODULE._async_registry()
        with mock.patch.object(
            registry,
            "reconcile_batch_trial_states",
            wraps=registry.reconcile_batch_trial_states,
        ) as reconcile:
            _, repeated_running = self._request(
                "GET",
                f"/async_trial_batches/{admission['batch_id']}",
            )
        reconcile.assert_not_called()

        self.assertEqual(running_status, 200)
        self.assertEqual(running["state"], "RUNNING")
        self.assertEqual(running["queued_trials"], 1)
        self.assertEqual(running["running_trials"], 1)
        self.assertEqual(repeated_running["revision"], running["revision"])

        first_result.write_text(
            json.dumps({"ok": True, "large_result_body": "x" * 10_000}),
            encoding="utf-8",
        )
        first_active.unlink()
        second_pending.replace(second_active)
        second_result.write_text(json.dumps({"ok": False}), encoding="utf-8")
        second_active.unlink()

        MODULE.ASYNC_REGISTRY = None
        completed_status, completed = self._request(
            "GET",
            f"/async_trial_batches/{admission['batch_id']}",
        )
        _, repeated_completed = self._request(
            "GET",
            f"/async_trial_batches/{admission['batch_id']}",
        )

        self.assertEqual(completed_status, 200)
        self.assertEqual(completed["state"], "COMPLETED")
        self.assertEqual(completed["succeeded_trials"], 1)
        self.assertEqual(completed["failed_trials"], 1)
        self.assertEqual(completed["running_trials"], 0)
        self.assertEqual(completed["revision"], running["revision"] + 1)
        self.assertEqual(repeated_completed["revision"], completed["revision"])
        self.assertNotIn("large_result_body", completed)

    def test_async_bulk_get_returns_snapshots_and_missing_ids(self) -> None:
        _, first = self._request(
            "POST",
            "/async_trial_batches",
            self._valid_async_request(request_id="async-first", trial_count=1),
        )
        _, second = self._request(
            "POST",
            "/async_trial_batches",
            self._valid_async_request(request_id="async-second", trial_count=1),
        )
        missing = "atb-" + "0" * 32

        status, response = self._request(
            "GET",
            f"/async_trial_batches?ids={second['batch_id']},{missing},{first['batch_id']}",
        )

        self.assertEqual(status, 200)
        self.assertEqual(
            [batch["batch_id"] for batch in response["batches"]],
            [second["batch_id"], first["batch_id"]],
        )
        self.assertEqual(response["missing_ids"], [missing])
        self.assertNotIn("expired_ids", response)

    def test_async_results_return_partial_exact_payloads_and_survive_registry_reopen(self) -> None:
        _, admission = self._request(
            "POST",
            "/async_trial_batches",
            self._valid_async_request(trial_count=2),
        )
        first_pending = self._async_queue_path(admission, 0, "pending")
        first_active = self._async_queue_path(admission, 0, "active")
        first_result = self._async_queue_path(admission, 0, "results")
        second_pending = self._async_queue_path(admission, 1, "pending")
        second_active = self._async_queue_path(admission, 1, "active")
        second_result = self._async_queue_path(admission, 1, "results")
        first_payload = {
            "ok": True,
            "task_id": "1",
            "reward": 1.0,
            "verifier_result": {"rewards": {"reward": 1.0}},
            "large_result_body": "x" * 10_000,
        }

        first_pending.replace(first_active)
        first_result.write_text(json.dumps(first_payload), encoding="utf-8")
        first_active.unlink()
        partial_status, partial = self._request(
            "GET",
            f"/async_trial_batches/{admission['batch_id']}/results",
        )
        _, repeated_partial = self._request(
            "GET",
            f"/async_trial_batches/{admission['batch_id']}/results",
        )

        self.assertEqual(partial_status, 200)
        self.assertEqual(partial["state"], "RUNNING")
        self.assertEqual(partial["requested_trials"], 2)
        self.assertEqual(partial["terminal_trials"], 1)
        self.assertEqual(partial["available_results"], 1)
        self.assertEqual(partial["unavailable_results"], 0)
        self.assertEqual(partial["results"][0]["result"], first_payload)
        self.assertEqual(repeated_partial, partial)

        second_payload = {
            "ok": False,
            "task_id": "1",
            "reward": 0.0,
            "exception_info": {"exception_type": "SyntheticFailure"},
        }
        second_pending.replace(second_active)
        second_result.write_text(json.dumps(second_payload), encoding="utf-8")
        second_active.unlink()
        MODULE.ASYNC_REGISTRY = None

        completed_status, completed = self._request(
            "GET",
            f"/async_trial_batches/{admission['batch_id']}/results",
        )

        self.assertEqual(completed_status, 200)
        self.assertEqual(completed["state"], "COMPLETED")
        self.assertEqual(completed["terminal_trials"], 2)
        self.assertEqual(completed["available_results"], 2)
        self.assertEqual(completed["unavailable_results"], 0)
        self.assertEqual(
            [result["result"] for result in completed["results"]],
            [first_payload, second_payload],
        )
        self.assertEqual(completed["results"][1]["state"], "FAILED")
        self.assertEqual(
            completed["results"][1]["error_category"],
            "WORKER_RESULT_FAILED",
        )

    def test_async_results_report_malformed_terminal_artifact_explicitly(self) -> None:
        _, admission = self._request(
            "POST",
            "/async_trial_batches",
            self._valid_async_request(trial_count=1),
        )
        pending = self._async_queue_path(admission, 0, "pending")
        active = self._async_queue_path(admission, 0, "active")
        result = self._async_queue_path(admission, 0, "results")
        pending.replace(active)
        result.write_text("{not-json", encoding="utf-8")
        active.unlink()

        status, response = self._request(
            "GET",
            f"/async_trial_batches/{admission['batch_id']}/results",
        )

        self.assertEqual(status, 200)
        self.assertEqual(response["state"], "COMPLETED")
        self.assertEqual(response["failed_trials"], 1)
        self.assertEqual(response["terminal_trials"], 1)
        self.assertEqual(response["available_results"], 0)
        self.assertEqual(response["unavailable_results"], 1)
        self.assertIsNone(response["results"][0]["result"])
        self.assertEqual(
            response["results"][0]["error"],
            {"category": "MALFORMED_WORKER_RESULT"},
        )

    def test_async_get_distinguishes_malformed_unknown_and_missing_ids(self) -> None:
        malformed_status, malformed = self._request(
            "GET",
            "/async_trial_batches/not-a-batch-id",
        )
        unknown_status, unknown = self._request(
            "GET",
            f"/async_trial_batches/atb-{'0' * 32}",
        )
        malformed_results_status, _ = self._request(
            "GET",
            "/async_trial_batches/not-a-batch-id/results",
        )
        unknown_results_status, _ = self._request(
            "GET",
            f"/async_trial_batches/atb-{'0' * 32}/results",
        )
        missing_status, missing = self._request("GET", "/async_trial_batches")

        self.assertEqual(malformed_status, 400)
        self.assertEqual(malformed["detail"]["exception_type"], "ValueError")
        self.assertEqual(unknown_status, 404)
        self.assertEqual(unknown["detail"]["exception_type"], "BatchNotFound")
        self.assertEqual(malformed_results_status, 400)
        self.assertEqual(unknown_results_status, 404)
        self.assertEqual(missing_status, 400)
        self.assertIn("ids", missing["detail"]["exception_message"])

    def test_async_invalid_trial_rejects_whole_batch_before_registry_or_queue_write(self) -> None:
        request = self._valid_async_request(trial_count=2)
        request["trials"][1]["task_id"] = "missing-task"

        status, response = self._request("POST", "/async_trial_batches", request)

        self.assertEqual(status, 400)
        self.assertEqual(response["detail"]["exception_type"], "FileNotFoundError")
        self.assertFalse(MODULE.ASYNC_TRIAL_REGISTRY_PATH.exists())
        self.assertFalse(self.job_queue_root.exists())
        self.ensure_submission_zellij.assert_not_called()

    def test_async_endpoint_is_hidden_when_feature_flag_is_disabled(self) -> None:
        with mock.patch.object(MODULE, "ENABLE_ASYNC_TRIAL_BATCHES", False):
            post_status, post_response = self._request(
                "POST",
                "/async_trial_batches",
                self._valid_async_request(trial_count=1),
            )
            list_status, list_response = self._request(
                "GET",
                f"/async_trial_batches?ids=atb-{'0' * 32}",
            )
            get_status, get_response = self._request(
                "GET",
                f"/async_trial_batches/atb-{'0' * 32}",
            )
            results_status, results_response = self._request(
                "GET",
                f"/async_trial_batches/atb-{'0' * 32}/results",
            )

        self.assertEqual((post_status, post_response), (404, {"detail": "not found"}))
        self.assertEqual((list_status, list_response), (404, {"detail": "not found"}))
        self.assertEqual((get_status, get_response), (404, {"detail": "not found"}))
        self.assertEqual((results_status, results_response), (404, {"detail": "not found"}))
        self.assertFalse(MODULE.ASYNC_TRIAL_REGISTRY_PATH.exists())
        self.ensure_submission_zellij.assert_not_called()

    def test_async_oversized_batch_is_rejected_before_admission(self) -> None:
        request = self._valid_async_request(trial_count=9)

        status, response = self._request("POST", "/async_trial_batches", request)

        self.assertEqual(status, 400)
        self.assertIn("RL_ASYNC_MAX_TRIALS_PER_BATCH=8", response["detail"]["exception_message"])
        self.assertFalse(MODULE.ASYNC_TRIAL_REGISTRY_PATH.exists())
        self.assertFalse(self.job_queue_root.exists())
        self.ensure_submission_zellij.assert_not_called()

    def test_async_request_body_at_configured_limit_is_accepted(self) -> None:
        body = json.dumps(self._valid_async_request(trial_count=1)).encode("utf-8")

        with mock.patch.object(MODULE, "ASYNC_MAX_REQUEST_BYTES", len(body)):
            status, response = self._request(
                "POST",
                "/async_trial_batches",
                raw_body=body,
            )

        self.assertEqual(status, 202)
        self.assertEqual(response["requested_trials"], 1)

    def test_async_oversized_request_body_is_rejected_before_admission(self) -> None:
        body = json.dumps(self._valid_async_request(trial_count=1)).encode("utf-8")

        with mock.patch.object(MODULE, "ASYNC_MAX_REQUEST_BYTES", len(body) - 1):
            status, response = self._request(
                "POST",
                "/async_trial_batches",
                raw_body=body,
            )

        self.assertEqual(status, 413)
        self.assertEqual(
            response["detail"]["exception_type"],
            "AsyncRequestBodyTooLarge",
        )
        self.assertFalse(MODULE.ASYNC_TRIAL_REGISTRY_PATH.exists())
        self.assertFalse(self.job_queue_root.exists())
        self.ensure_submission_zellij.assert_not_called()

    def test_async_bulk_status_id_limit_is_rejected_before_registry_open(self) -> None:
        batch_ids = [f"atb-{index:032x}" for index in range(4)]

        status, response = self._request(
            "GET",
            f"/async_trial_batches?ids={','.join(batch_ids)}",
        )

        self.assertEqual(status, 400)
        self.assertIn(
            "RL_ASYNC_MAX_BULK_STATUS_IDS=3",
            response["detail"]["exception_message"],
        )
        self.assertFalse(MODULE.ASYNC_TRIAL_REGISTRY_PATH.exists())
        self.ensure_submission_zellij.assert_not_called()

    def test_async_same_request_id_with_changed_payload_returns_conflict(self) -> None:
        request = self._valid_async_request(trial_count=1)
        changed = json.loads(json.dumps(request))
        changed["trials"][0]["payload"]["model_name"] = "another-model"

        first_status, first = self._request("POST", "/async_trial_batches", request)
        conflict_status, conflict = self._request("POST", "/async_trial_batches", changed)

        self.assertEqual(first_status, 202)
        self.assertEqual(conflict_status, 409)
        self.assertEqual(conflict["detail"]["exception_type"], "IdempotencyConflict")
        admission = MODULE._async_registry().get_admission("async-request-001")
        self.assertIsNotNone(admission)
        self.assertEqual(admission.response, first)
        pending_dir = self.job_queue_root / "ray-job-async" / "pending"
        self.assertEqual(len(list(pending_dir.glob("*.json"))), 1)

    def test_concurrent_duplicate_async_submits_materialize_each_trial_once(self) -> None:
        request = self._valid_async_request(trial_count=2)

        with ThreadPoolExecutor(max_workers=8) as executor:
            responses = list(
                executor.map(
                    lambda _: self._request("POST", "/async_trial_batches", request),
                    range(8),
                )
            )

        self.assertEqual({status for status, _ in responses}, {202})
        bodies = [body for _, body in responses]
        self.assertTrue(all(body == bodies[0] for body in bodies))
        pending_dir = self.job_queue_root / "ray-job-async" / "pending"
        self.assertEqual(len(list(pending_dir.glob("*.json"))), 2)
        self.ensure_submission_zellij.assert_called_once()

    def test_async_lifecycle_events_link_logical_and_execution_identity(self) -> None:
        request = self._valid_async_request(trial_count=1)
        _, admission = self._request("POST", "/async_trial_batches", request)
        pending = self._async_queue_path(admission, 0, "pending")
        active = self._async_queue_path(admission, 0, "active")
        result = self._async_queue_path(admission, 0, "results")
        trial = admission["trials"][0]

        pending.replace(active)
        self._request("GET", f"/async_trial_batches/{admission['batch_id']}")
        result.write_text(json.dumps({"ok": True, "reward": 1.0}), encoding="utf-8")
        active.unlink()
        self._request("GET", f"/async_trial_batches/{admission['batch_id']}/results")

        events = self._trace_events()
        event_names = {event["event"] for event in events}
        self.assertTrue(
            {
                "async_batch_admitted",
                "async_trial_queued",
                "async_trial_state_observed",
                "async_status_response_delivered",
                "async_results_response_delivered",
            }.issubset(event_names)
        )
        trial_events = [
            event
            for event in events
            if event.get("trial_execution_id") == trial["trial_execution_id"]
        ]
        self.assertTrue(trial_events)
        self.assertTrue(
            all(event.get("batch_id") == admission["batch_id"] for event in trial_events)
        )
        self.assertTrue(
            all(event.get("client_trial_id") == trial["client_trial_id"] for event in trial_events)
        )
        observed_states = {
            event.get("to_state")
            for event in trial_events
            if event["event"] == "async_trial_state_observed"
        }
        self.assertEqual(observed_states, {"RUNNING", "SUCCEEDED"})

    def test_async_response_drop_retry_recovers_same_durable_admission(self) -> None:
        request = self._valid_async_request(trial_count=1)
        with mock.patch.object(
            MODULE.Handler,
            "_send_json",
            side_effect=BrokenPipeError("injected response loss"),
        ):
            with self.assertRaises(http.client.RemoteDisconnected):
                self._request("POST", "/async_trial_batches", request)

        retry_status, recovered = self._request("POST", "/async_trial_batches", request)

        self.assertEqual(retry_status, 202)
        admission = MODULE._async_registry().get_admission("async-request-001")
        self.assertIsNotNone(admission)
        self.assertEqual(recovered, admission.response)
        pending_dir = self.job_queue_root / "ray-job-async" / "pending"
        self.assertEqual(len(list(pending_dir.glob("*.json"))), 1)
        events = self._trace_events()
        self.assertTrue(
            any(event["event"] == "async_response_delivery_failed" for event in events)
        )
        self.assertEqual(
            [
                event["admission_outcome"]
                for event in events
                if event["event"] == "async_batch_admitted"
            ],
            ["created", "recovered"],
        )

    def test_trace_write_failure_does_not_change_accepted_work(self) -> None:
        request = self._valid_async_request(trial_count=1)
        with mock.patch.object(MODULE, "TRACE_LOG", self.root):
            status, response = self._request("POST", "/async_trial_batches", request)

        self.assertEqual(status, 202)
        self.assertIsNotNone(MODULE._async_registry().get_admission("async-request-001"))
        self.assertTrue(self._async_queue_path(response, 0, "pending").exists())

    def test_async_responses_and_trace_do_not_expose_secret_markers(self) -> None:
        secret = "step-10-secret-marker"
        request = self._valid_async_request(trial_count=1)
        request["trials"][0]["payload"]["api_key"] = secret
        submit_status, admission = self._request("POST", "/async_trial_batches", request)
        status_status, snapshot = self._request(
            "GET",
            f"/async_trial_batches/{admission['batch_id']}",
        )
        with mock.patch.object(
            MODULE,
            "reconcile_async_batch_status",
            side_effect=RuntimeError(f"internal failure containing {secret}"),
        ):
            error_status, error = self._request(
                "GET",
                f"/async_trial_batches/{admission['batch_id']}",
            )
        pending = self._async_queue_path(admission, 0, "pending")
        result_path = self._async_queue_path(admission, 0, "results")
        pending.unlink()
        result_path.write_text(
            json.dumps(
                {
                    "ok": True,
                    "metadata": {
                        "api_key": secret,
                        "authorization": f"Bearer {secret}",
                    },
                }
            ),
            encoding="utf-8",
        )
        results_status, results = self._request(
            "GET",
            f"/async_trial_batches/{admission['batch_id']}/results",
        )
        health_status, health = self._request("GET", "/health")

        self.assertEqual(
            (submit_status, status_status, error_status, results_status, health_status),
            (202, 200, 500, 200, 200),
        )
        self.assertEqual(error["detail"]["category"], "STATUS_INTERNAL_ERROR")
        metrics = health["async_control_plane"]["metrics"]
        self.assertEqual(metrics["counters"]["status_requests"], 2)
        self.assertEqual(
            metrics["rejection_categories"]["STATUS_INTERNAL_ERROR"],
            1,
        )
        serialized = json.dumps(
            [admission, snapshot, error, results, health, self._trace_events()]
        )
        self.assertNotIn(secret, serialized)

    def test_startup_recovery_closes_crash_window_after_queue_file_publication(self) -> None:
        request = self._valid_async_request(trial_count=1)
        real_enqueue = MODULE._enqueue_request

        def enqueue_then_crash(
            payload: dict[str, object],
            *,
            zellij_session: str | None = None,
        ) -> tuple[str, Path]:
            result = real_enqueue(payload, zellij_session=zellij_session)
            raise RuntimeError("injected crash after queue publication")

        with mock.patch.object(MODULE, "_enqueue_request", side_effect=enqueue_then_crash):
            status, response = self._request("POST", "/async_trial_batches", request)

        self.assertEqual(status, 500)
        self.assertEqual(response["detail"]["category"], "ADMISSION_INTERNAL_ERROR")
        self.assertEqual(response["detail"]["exception_message"], "internal server error")
        registry = MODULE._async_registry()
        admission = registry.get_admission("async-request-001")
        self.assertIsNotNone(admission)
        trial_execution_id = admission.response["trials"][0]["trial_execution_id"]
        pending_path = (
            self.job_queue_root
            / "ray-job-async"
            / "pending"
            / f"{trial_execution_id}.json"
        )
        self.assertTrue(pending_path.exists())
        self.assertIsNone(
            registry.list_enqueue_intents(admission.batch_id)[0]["materialized_at"]
        )

        MODULE.ASYNC_REGISTRY = None
        first_recovery = MODULE.reconcile_async_state_on_startup()
        second_recovery = MODULE.reconcile_async_state_on_startup()

        self.assertEqual(first_recovery["batches_scanned"], 1)
        self.assertEqual(first_recovery["intents_materialized"], 1)
        self.assertEqual(second_recovery["intents_materialized"], 0)
        self.assertEqual(len(list(pending_path.parent.glob("*.json"))), 1)
        self.assertIsNotNone(
            MODULE._async_registry().list_enqueue_intents(admission.batch_id)[0]["materialized_at"]
        )

    def test_startup_recovery_materializes_committed_intent_exactly_once(self) -> None:
        normalized = MODULE._validate_and_normalize_async_batch_request(
            self._valid_async_request(trial_count=1)
        )
        admission = MODULE._async_registry().admit_batch(normalized)
        pending_path = self._async_queue_path(admission.response, 0, "pending")
        self.assertFalse(pending_path.exists())

        MODULE.ASYNC_REGISTRY = None
        first_recovery = MODULE.reconcile_async_state_on_startup()
        second_recovery = MODULE.reconcile_async_state_on_startup()

        self.assertEqual(first_recovery["batches_scanned"], 1)
        self.assertEqual(first_recovery["intents_materialized"], 1)
        self.assertEqual(second_recovery["intents_materialized"], 0)
        self.assertTrue(pending_path.exists())
        self.assertEqual(len(list(pending_path.parent.glob("*.json"))), 1)
        self.ensure_submission_zellij.assert_called_once()

    def test_startup_recovery_fails_missing_materialized_artifact_without_reexecution(self) -> None:
        _, admission = self._request(
            "POST",
            "/async_trial_batches",
            self._valid_async_request(trial_count=1),
        )
        pending_path = self._async_queue_path(admission, 0, "pending")
        pending_path.unlink()
        self.ensure_submission_zellij.reset_mock()
        MODULE.ASYNC_REGISTRY = None

        recovery = MODULE.reconcile_async_state_on_startup()
        repeated_recovery = MODULE.reconcile_async_state_on_startup()
        snapshot = MODULE._async_registry().get_batch_snapshot(admission["batch_id"])
        result_status, results = self._request(
            "GET",
            f"/async_trial_batches/{admission['batch_id']}/results",
        )

        self.assertEqual(recovery["completed_batches"], 1)
        self.assertEqual(repeated_recovery["batches_scanned"], 0)
        self.assertEqual(snapshot["state"], "COMPLETED")
        self.assertEqual(snapshot["failed_trials"], 1)
        self.assertEqual(result_status, 200)
        self.assertEqual(results["results"][0]["result"], None)
        self.assertEqual(
            results["results"][0]["error"],
            {"category": "QUEUE_ARTIFACT_MISSING"},
        )
        pending_dir = self.job_queue_root / "ray-job-async" / "pending"
        self.assertEqual(len(list(pending_dir.glob("*.json"))), 0)
        self.ensure_submission_zellij.assert_not_called()

    def test_startup_recovery_never_reexecutes_terminal_unmaterialized_intent(self) -> None:
        normalized = MODULE._validate_and_normalize_async_batch_request(
            self._valid_async_request(trial_count=1)
        )
        registry = MODULE._async_registry()
        admission = registry.admit_batch(normalized)
        trial_execution_id = admission.response["trials"][0]["trial_execution_id"]
        registry.reconcile_batch_trial_states(
            admission.batch_id,
            {
                trial_execution_id: MODULE.TrialStateObservation(
                    MODULE.TrialState.FAILED,
                    normalized_error_category="PREEXISTING_TERMINAL_FAILURE",
                )
            },
        )
        self.ensure_submission_zellij.reset_mock()
        MODULE.ASYNC_REGISTRY = None

        recovery = MODULE.reconcile_async_state_on_startup()

        self.assertEqual(recovery["batches_scanned"], 1)
        self.assertEqual(recovery["completed_batches"], 1)
        self.assertIsNotNone(
            MODULE._async_registry().list_enqueue_intents(admission.batch_id)[0]["materialized_at"]
        )
        pending_dir = self.job_queue_root / "ray-job-async" / "pending"
        self.assertEqual(len(list(pending_dir.glob("*.json"))), 0)
        self.ensure_submission_zellij.assert_not_called()

    def test_fresh_python_process_recovers_running_and_terminal_artifacts(self) -> None:
        _, admission = self._request(
            "POST",
            "/async_trial_batches",
            self._valid_async_request(trial_count=2),
        )
        first_pending = self._async_queue_path(admission, 0, "pending")
        first_active = self._async_queue_path(admission, 0, "active")
        second_pending = self._async_queue_path(admission, 1, "pending")
        second_active = self._async_queue_path(admission, 1, "active")
        second_result = self._async_queue_path(admission, 1, "results")
        first_pending.replace(first_active)
        second_pending.replace(second_active)
        second_result.write_text(json.dumps({"ok": True, "reward": 1.0}), encoding="utf-8")
        second_active.unlink()

        child_program = """
import json
import rollout_remote_harbor as service

print(json.dumps(service.reconcile_async_state_on_startup()))
"""
        child_env = os.environ.copy()
        child_env.update(
            {
                "PYTHONPATH": str(SCRIPT.parent),
                "RL_DATASET_NAME": "test-dataset",
                "RL_DATASET_ROOT": str(self.dataset_root),
                "RL_TRACE_LOG": str(self.trace_log),
                "RL_QUEUE_DIR": str(self.queue_root),
                "RL_ACTIVE_DIR": str(self.queue_root / "active"),
                "RL_JOB_QUEUE_ROOT": str(self.job_queue_root),
                "RL_JOB_RUNTIME_ROOT": str(self.root / "runtime"),
                "RL_DYNAMIC_JOB_ZELLIJ": "0",
                "RL_ASYNC_TRIAL_BATCHES_ENABLED": "1",
                "RL_ASYNC_TRIAL_REGISTRY_PATH": str(MODULE.ASYNC_TRIAL_REGISTRY_PATH),
            }
        )

        first_child = subprocess.run(
            [sys.executable, "-c", child_program],
            check=True,
            env=child_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        second_child = subprocess.run(
            [sys.executable, "-c", child_program],
            check=True,
            env=child_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        MODULE.ASYNC_REGISTRY = None
        snapshot = MODULE._async_registry().get_batch_snapshot(admission["batch_id"])

        self.assertEqual(json.loads(first_child.stdout)["batches_scanned"], 1)
        self.assertEqual(json.loads(second_child.stdout)["intents_materialized"], 0)
        self.assertEqual(snapshot["state"], "RUNNING")
        self.assertEqual(snapshot["running_trials"], 1)
        self.assertEqual(snapshot["succeeded_trials"], 1)
        self.assertEqual(snapshot["revision"], 2)
        self.assertTrue(first_active.exists())
        self.assertTrue(second_result.exists())

    def test_main_recovers_async_state_before_opening_listener(self) -> None:
        events: list[str] = []
        fake_server = mock.Mock()
        fake_server.serve_forever.side_effect = lambda: events.append("serve")

        def recover() -> dict[str, int]:
            events.append("recover")
            return {"batches_scanned": 1, "intents_materialized": 1, "completed_batches": 0}

        def open_listener(*_args: object, **_kwargs: object) -> mock.Mock:
            events.append("listen")
            return fake_server

        with (
            mock.patch.object(MODULE, "reconcile_async_state_on_startup", side_effect=recover),
            mock.patch.object(MODULE, "ThreadingHTTPServer", side_effect=open_listener),
        ):
            exit_code = MODULE.main()

        self.assertEqual(exit_code, 0)
        self.assertEqual(events, ["recover", "listen", "serve"])


if __name__ == "__main__":
    unittest.main()
