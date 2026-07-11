#!/usr/bin/env python3
"""Contract tests for durable async trial batch admission."""

from __future__ import annotations

import copy
import importlib.util
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "async_trial_registry.py"
SPEC = importlib.util.spec_from_file_location("async_trial_registry", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class AsyncTrialRegistryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "registry.sqlite3"
        self.registry = MODULE.AsyncTrialRegistry(self.db_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    @staticmethod
    def _request(request_id: str = "request-001", trial_count: int = 2) -> dict[str, object]:
        return {
            "request_id": request_id,
            "client_batch_id": f"client-{request_id}",
            "trainer_run_id": "trainer-run-001",
            "batching_key": {
                "dataset_name": "seta",
                "ray_submission_id": "ray-job-001",
                "policy_version": "policy-0",
            },
            "trials": [
                {
                    "client_trial_id": f"session-{index}",
                    "session_id": f"session-{index}",
                    "task_id": str(index),
                    "group_id": index,
                    "rollout_step": 0,
                    "policy_version": "policy-0",
                    "payload": {
                        "session_id": f"session-{index}",
                        "task_id": str(index),
                        "ray_submission_id": "ray-job-001",
                    },
                }
                for index in range(trial_count)
            ],
        }

    def _table_count(self, table: str) -> int:
        self.assertIn(
            table,
            {
                "async_trial_batches",
                "trial_executions",
                "enqueue_intents",
                "idempotency_records",
            },
        )
        with closing(sqlite3.connect(self.db_path)) as connection:
            return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])

    def test_admission_persists_batch_trials_intents_and_original_response(self) -> None:
        request = self._request()

        admission = self.registry.admit_batch(request)

        self.assertTrue(admission.created)
        self.assertEqual(admission.response["state"], "QUEUED")
        self.assertEqual(admission.response["revision"], 1)
        self.assertEqual(admission.response["requested_trials"], 2)
        self.assertEqual(self._table_count("async_trial_batches"), 1)
        self.assertEqual(self._table_count("trial_executions"), 2)
        self.assertEqual(self._table_count("enqueue_intents"), 2)
        self.assertEqual(self._table_count("idempotency_records"), 1)

        reopened = MODULE.AsyncTrialRegistry(self.db_path)
        persisted = reopened.get_admission("request-001")
        self.assertIsNotNone(persisted)
        self.assertEqual(persisted.response, admission.response)
        batch = reopened.get_batch(admission.batch_id)
        self.assertEqual(batch["request_id"], "request-001")
        self.assertEqual(batch["queued_trials"], 2)
        self.assertEqual(
            [trial["client_trial_id"] for trial in batch["trials"]],
            ["session-0", "session-1"],
        )
        intents = reopened.list_enqueue_intents(admission.batch_id)
        self.assertEqual(
            [intent["payload"]["session_id"] for intent in intents],
            ["session-0", "session-1"],
        )

    def test_same_request_returns_original_admission_without_duplicate_records(self) -> None:
        request = self._request()

        first = self.registry.admit_batch(request)
        second = self.registry.admit_batch(copy.deepcopy(request))

        self.assertTrue(first.created)
        self.assertFalse(second.created)
        self.assertEqual(second.response, first.response)
        self.assertEqual(self._table_count("async_trial_batches"), 1)
        self.assertEqual(self._table_count("trial_executions"), 2)
        self.assertEqual(self._table_count("enqueue_intents"), 2)

    def test_materialized_enqueue_intent_is_durable_and_idempotent(self) -> None:
        admission = self.registry.admit_batch(self._request())
        trial_execution_id = admission.response["trials"][0]["trial_execution_id"]

        first_timestamp = self.registry.mark_enqueue_intent_materialized(trial_execution_id)
        second_timestamp = self.registry.mark_enqueue_intent_materialized(trial_execution_id)

        self.assertEqual(second_timestamp, first_timestamp)
        pending = self.registry.list_enqueue_intents(
            admission.batch_id,
            unmaterialized_only=True,
        )
        self.assertEqual(len(pending), 1)
        self.assertNotEqual(pending[0]["trial_execution_id"], trial_execution_id)

        reopened = MODULE.AsyncTrialRegistry(self.db_path)
        intents = reopened.list_enqueue_intents(admission.batch_id)
        self.assertEqual(intents[0]["materialized_at"], first_timestamp)

    def test_admission_survives_fresh_python_process_and_retry(self) -> None:
        request = self._request()
        first = self.registry.admit_batch(request)
        request_path = Path(self.temp_dir.name) / "request.json"
        request_path.write_text(json.dumps(request), encoding="utf-8")
        child_program = """
import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

module_path, database_path, request_path = sys.argv[1:4]
spec = importlib.util.spec_from_file_location("child_async_trial_registry", module_path)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)

registry = module.AsyncTrialRegistry(database_path)
request = json.loads(Path(request_path).read_text(encoding="utf-8"))
admission = registry.admit_batch(request)
connection = sqlite3.connect(database_path)
try:
    journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
finally:
    connection.close()
print(json.dumps({
    "created": admission.created,
    "response": admission.response,
    "journal_mode": journal_mode,
    "sqlite_version": sqlite3.sqlite_version,
}))
"""

        child = subprocess.run(
            [
                sys.executable,
                "-c",
                child_program,
                str(SCRIPT),
                str(self.db_path),
                str(request_path),
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        recovered = json.loads(child.stdout)
        self.assertFalse(recovered["created"])
        self.assertEqual(recovered["response"], first.response)
        self.assertEqual(recovered["journal_mode"], "wal")
        self.assertEqual(recovered["sqlite_version"], sqlite3.sqlite_version)
        self.assertEqual(self._table_count("async_trial_batches"), 1)
        self.assertEqual(self._table_count("trial_executions"), 2)
        self.assertEqual(self._table_count("enqueue_intents"), 2)
        self.assertEqual(self._table_count("idempotency_records"), 1)

    def test_terminal_result_record_survives_fresh_python_process(self) -> None:
        admission = self.registry.admit_batch(self._request(trial_count=1))
        trial_execution_id = admission.response["trials"][0]["trial_execution_id"]
        result_path = Path(self.temp_dir.name) / "result.json"
        result_path.write_text(json.dumps({"ok": True, "reward": 1.0}), encoding="utf-8")
        self.registry.reconcile_batch_trial_states(
            admission.batch_id,
            {
                trial_execution_id: MODULE.TrialStateObservation(
                    MODULE.TrialState.SUCCEEDED,
                    result_uri=str(result_path),
                )
            },
        )
        child_program = """
import importlib.util
import json
import sys

module_path, database_path, batch_id = sys.argv[1:4]
spec = importlib.util.spec_from_file_location("child_async_trial_registry", module_path)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)

registry = module.AsyncTrialRegistry(database_path)
print(json.dumps(registry.get_batch_result_records(batch_id)))
"""

        child = subprocess.run(
            [
                sys.executable,
                "-c",
                child_program,
                str(SCRIPT),
                str(self.db_path),
                admission.batch_id,
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        recovered = json.loads(child.stdout)
        self.assertEqual(recovered["state"], "COMPLETED")
        self.assertEqual(recovered["terminal_trials"], 1)
        self.assertEqual(recovered["results"][0]["result_uri"], str(result_path))

    def test_same_request_id_with_different_payload_conflicts(self) -> None:
        original = self._request()
        changed = copy.deepcopy(original)
        changed["trials"][0]["payload"]["task_id"] = "different"
        self.registry.admit_batch(original)

        with self.assertRaises(MODULE.IdempotencyConflict) as raised:
            self.registry.admit_batch(changed)

        self.assertEqual(raised.exception.request_id, "request-001")
        self.assertNotEqual(
            raised.exception.existing_digest,
            raised.exception.supplied_digest,
        )
        self.assertEqual(self._table_count("async_trial_batches"), 1)
        self.assertEqual(self._table_count("trial_executions"), 2)

    def test_canonical_digest_ignores_json_object_key_order(self) -> None:
        request = self._request(trial_count=1)
        reordered = json.loads(json.dumps(request, sort_keys=True))

        self.assertEqual(
            MODULE.canonical_request_digest(request),
            MODULE.canonical_request_digest(reordered),
        )

    def test_invalid_batch_is_rejected_before_any_record_is_written(self) -> None:
        request = self._request()
        request["trials"][1]["client_trial_id"] = "session-0"

        with self.assertRaises(MODULE.InvalidAdmissionRequest):
            self.registry.admit_batch(request)

        self.assertEqual(self._table_count("async_trial_batches"), 0)
        self.assertEqual(self._table_count("trial_executions"), 0)
        self.assertEqual(self._table_count("enqueue_intents"), 0)
        self.assertEqual(self._table_count("idempotency_records"), 0)

    def test_mid_transaction_database_failure_rolls_back_all_records(self) -> None:
        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.execute(
                """
                CREATE TRIGGER reject_second_trial
                BEFORE INSERT ON trial_executions
                WHEN NEW.client_trial_id = 'session-1'
                BEGIN
                    SELECT RAISE(ABORT, 'injected admission failure');
                END
                """
            )
            connection.commit()

        with self.assertRaises(sqlite3.IntegrityError):
            self.registry.admit_batch(self._request())

        self.assertEqual(self._table_count("async_trial_batches"), 0)
        self.assertEqual(self._table_count("trial_executions"), 0)
        self.assertEqual(self._table_count("enqueue_intents"), 0)
        self.assertEqual(self._table_count("idempotency_records"), 0)

        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.execute("DROP TRIGGER reject_second_trial")
            connection.commit()
        retry = self.registry.admit_batch(self._request())
        self.assertTrue(retry.created)
        self.assertEqual(self._table_count("async_trial_batches"), 1)
        self.assertEqual(self._table_count("trial_executions"), 2)
        self.assertEqual(self._table_count("enqueue_intents"), 2)
        self.assertEqual(self._table_count("idempotency_records"), 1)

    def test_concurrent_retries_create_one_batch_in_twenty_rounds(self) -> None:
        for round_number in range(20):
            request = self._request(f"concurrent-{round_number}", trial_count=1)
            with ThreadPoolExecutor(max_workers=8) as executor:
                admissions = list(
                    executor.map(lambda _: self.registry.admit_batch(request), range(8))
                )

            self.assertEqual(sum(admission.created for admission in admissions), 1)
            self.assertEqual(len({admission.batch_id for admission in admissions}), 1)

        self.assertEqual(self._table_count("async_trial_batches"), 20)
        self.assertEqual(self._table_count("trial_executions"), 20)
        self.assertEqual(self._table_count("enqueue_intents"), 20)
        self.assertEqual(self._table_count("idempotency_records"), 20)

    def test_queue_reconciliation_is_monotonic_and_survives_missed_running_state(self) -> None:
        admission = self.registry.admit_batch(self._request(trial_count=2))
        first_trial_id = admission.response["trials"][0]["trial_execution_id"]
        second_trial_id = admission.response["trials"][1]["trial_execution_id"]

        partially_completed = self.registry.reconcile_batch_trial_states(
            admission.batch_id,
            {
                first_trial_id: MODULE.TrialStateObservation(
                    MODULE.TrialState.SUCCEEDED,
                    result_uri="/tmp/result.json",
                )
            },
        )
        repeated = self.registry.reconcile_batch_trial_states(
            admission.batch_id,
            {
                first_trial_id: MODULE.TrialStateObservation(
                    MODULE.TrialState.SUCCEEDED,
                    result_uri="/tmp/result.json",
                )
            },
        )
        stale_active_observation = self.registry.reconcile_batch_trial_states(
            admission.batch_id,
            {first_trial_id: MODULE.TrialStateObservation(MODULE.TrialState.RUNNING)},
        )
        completed = self.registry.reconcile_batch_trial_states(
            admission.batch_id,
            {
                second_trial_id: MODULE.TrialStateObservation(
                    MODULE.TrialState.FAILED,
                    result_uri="/tmp/failed-result.json",
                )
            },
        )
        result_records = self.registry.get_batch_result_records(admission.batch_id)

        self.assertEqual(partially_completed["state"], "RUNNING")
        self.assertEqual(partially_completed["queued_trials"], 1)
        self.assertEqual(partially_completed["succeeded_trials"], 1)
        self.assertEqual(partially_completed["revision"], 2)
        self.assertEqual(completed["state"], "COMPLETED")
        self.assertEqual(completed["succeeded_trials"], 1)
        self.assertEqual(completed["failed_trials"], 1)
        self.assertEqual(completed["revision"], 3)
        self.assertEqual(repeated["revision"], 2)
        self.assertEqual(stale_active_observation["revision"], 2)
        self.assertEqual(result_records["state"], "COMPLETED")
        self.assertEqual(result_records["terminal_trials"], 2)
        self.assertEqual(
            [result["client_trial_id"] for result in result_records["results"]],
            ["session-0", "session-1"],
        )
        self.assertEqual(
            result_records["result_manifest_uri"],
            f"/async_trial_batches/{admission.batch_id}/results",
        )
        self.assertEqual(
            self.registry.get_batch(admission.batch_id)["trials"][0]["result_uri"],
            "/tmp/result.json",
        )

    def test_compact_batch_snapshots_preserve_request_order_and_report_missing_ids(self) -> None:
        first = self.registry.admit_batch(self._request("request-first", trial_count=1))
        second = self.registry.admit_batch(self._request("request-second", trial_count=1))
        missing = "atb-" + "0" * 32

        snapshots, missing_ids = self.registry.get_batch_snapshots(
            [second.batch_id, missing, first.batch_id, second.batch_id]
        )

        self.assertEqual(
            [snapshot["batch_id"] for snapshot in snapshots],
            [second.batch_id, first.batch_id],
        )
        self.assertEqual(missing_ids, [missing])
        self.assertNotIn("trials", snapshots[0])
        self.assertNotIn("batching_key", snapshots[0])
        self.assertEqual(snapshots[0]["queued_trials"], 1)

    def test_health_snapshot_reports_readiness_and_compact_workload_counts(self) -> None:
        admission = self.registry.admit_batch(self._request(trial_count=2))
        first_trial_id = admission.response["trials"][0]["trial_execution_id"]
        self.registry.mark_enqueue_intent_materialized(first_trial_id)
        self.registry.reconcile_batch_trial_states(
            admission.batch_id,
            {first_trial_id: MODULE.TrialStateObservation(MODULE.TrialState.RUNNING)},
        )

        health = self.registry.health_snapshot()

        self.assertTrue(health["ready"])
        self.assertEqual(health["schema_version"], 2)
        self.assertEqual(health["journal_mode"], "wal")
        self.assertTrue(health["readable"])
        self.assertEqual(health["outstanding_batches"], 1)
        self.assertEqual(health["unmaterialized_intents"], 1)
        self.assertEqual(health["batch_counts"], {"RUNNING": 1})
        self.assertEqual(health["trial_counts"], {"QUEUED": 1, "RUNNING": 1})

    def test_schema_version_is_durable_and_incompatible_version_is_rejected(self) -> None:
        self.assertEqual(self.registry.schema_version, 2)
        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.execute("PRAGMA user_version = 999")
            connection.commit()

        with self.assertRaises(MODULE.RegistrySchemaError):
            MODULE.AsyncTrialRegistry(self.db_path)


if __name__ == "__main__":
    unittest.main()
