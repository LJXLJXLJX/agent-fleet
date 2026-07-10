#!/usr/bin/env python3
"""Durable control-plane registry for asynchronous Harbor trial batches.

The registry is intentionally independent from HTTP routing and the existing
file execution queue. SQLite provides the atomic admission boundary; durable
enqueue intents bridge a committed TrialExecution to queue materialization in a
later layer.

The database must live on reliable local disk and be opened by one Agent Fleet
service instance. SQLite WAL semantics must not be assumed for a shared NFS
deployment.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any
from uuid import uuid4


SCHEMA_VERSION = 1


class BatchState(str, Enum):
    PENDING = "PENDING"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"
    DEADLINE_EXCEEDED = "DEADLINE_EXCEEDED"


class TrialState(str, Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    DEADLINE_EXCEEDED = "DEADLINE_EXCEEDED"
    STALE_PRE_EXECUTION = "STALE_PRE_EXECUTION"
    STALE_POST_EXECUTION = "STALE_POST_EXECUTION"


BATCH_TRANSITIONS = {
    BatchState.PENDING: {BatchState.QUEUED},
    BatchState.QUEUED: {
        BatchState.RUNNING,
        BatchState.COMPLETED,
        BatchState.CANCEL_REQUESTED,
        BatchState.FAILED,
        BatchState.DEADLINE_EXCEEDED,
    },
    BatchState.RUNNING: {
        BatchState.COMPLETED,
        BatchState.CANCEL_REQUESTED,
        BatchState.FAILED,
        BatchState.DEADLINE_EXCEEDED,
    },
    BatchState.CANCEL_REQUESTED: {
        BatchState.CANCELLED,
        BatchState.COMPLETED,
        BatchState.FAILED,
        BatchState.DEADLINE_EXCEEDED,
    },
    BatchState.COMPLETED: set(),
    BatchState.CANCELLED: set(),
    BatchState.FAILED: set(),
    BatchState.DEADLINE_EXCEEDED: set(),
}

TRIAL_TRANSITIONS = {
    TrialState.QUEUED: {
        TrialState.RUNNING,
        TrialState.CANCELLED,
        TrialState.DEADLINE_EXCEEDED,
        TrialState.STALE_PRE_EXECUTION,
    },
    TrialState.RUNNING: {
        TrialState.SUCCEEDED,
        TrialState.FAILED,
        TrialState.CANCEL_REQUESTED,
        TrialState.DEADLINE_EXCEEDED,
        TrialState.STALE_POST_EXECUTION,
    },
    TrialState.CANCEL_REQUESTED: {
        TrialState.CANCELLED,
        TrialState.SUCCEEDED,
        TrialState.FAILED,
        TrialState.DEADLINE_EXCEEDED,
        TrialState.STALE_POST_EXECUTION,
    },
    TrialState.SUCCEEDED: set(),
    TrialState.FAILED: set(),
    TrialState.CANCELLED: set(),
    TrialState.DEADLINE_EXCEEDED: set(),
    TrialState.STALE_PRE_EXECUTION: set(),
    TrialState.STALE_POST_EXECUTION: set(),
}

TERMINAL_TRIAL_STATES = {
    TrialState.SUCCEEDED,
    TrialState.FAILED,
    TrialState.CANCELLED,
    TrialState.DEADLINE_EXCEEDED,
    TrialState.STALE_PRE_EXECUTION,
    TrialState.STALE_POST_EXECUTION,
}


class RegistryError(RuntimeError):
    """Base class for registry failures that callers may classify."""


class RegistrySchemaError(RegistryError):
    """Raised when the on-disk schema cannot be opened by this version."""


class InvalidAdmissionRequest(ValueError):
    """Raised when a batch cannot satisfy durable registry invariants."""


class IdempotencyConflict(RegistryError):
    """Raised when one request ID is reused with a different payload."""

    def __init__(self, request_id: str, existing_digest: str, supplied_digest: str) -> None:
        super().__init__(f"request_id {request_id!r} was already used with a different payload")
        self.request_id = request_id
        self.existing_digest = existing_digest
        self.supplied_digest = supplied_digest


class InvalidStateTransition(RegistryError):
    """Raised when a batch or trial attempts an illegal state transition."""


class BatchNotFound(RegistryError):
    """Raised when a requested batch does not exist."""


class TrialNotFound(RegistryError):
    """Raised when a requested trial execution does not exist."""


@dataclass(frozen=True)
class AdmissionResult:
    response: dict[str, Any]
    created: bool

    @property
    def batch_id(self) -> str:
        return str(self.response["batch_id"])


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _format_time(value: datetime) -> str:
    return value.isoformat()


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise InvalidAdmissionRequest(f"request must contain only canonical JSON values: {exc}") from exc


def canonical_request_digest(request: Mapping[str, Any]) -> str:
    """Return a stable digest for a JSON request independent of object key order."""

    if not isinstance(request, Mapping):
        raise InvalidAdmissionRequest("admission request must be a JSON object")
    return hashlib.sha256(_canonical_json(dict(request)).encode("utf-8")).hexdigest()


def validate_batch_transition(current: BatchState, target: BatchState) -> None:
    if current == target:
        return
    if target not in BATCH_TRANSITIONS[current]:
        raise InvalidStateTransition(f"batch cannot transition from {current.value} to {target.value}")


def validate_trial_transition(current: TrialState, target: TrialState) -> None:
    if current == target:
        return
    if target not in TRIAL_TRANSITIONS[current]:
        raise InvalidStateTransition(f"trial cannot transition from {current.value} to {target.value}")


class AsyncTrialRegistry:
    """Owns atomic batch admission and durable logical execution state."""

    def __init__(
        self,
        path: str | Path,
        *,
        busy_timeout_seconds: float = 5.0,
        idempotency_ttl_seconds: float | None = None,
    ) -> None:
        self.path = Path(path)
        self.busy_timeout_seconds = busy_timeout_seconds
        self.idempotency_ttl_seconds = idempotency_ttl_seconds
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=self.busy_timeout_seconds,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {int(self.busy_timeout_seconds * 1000)}")
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def _write_transaction(self) -> Iterator[sqlite3.Connection]:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except BaseException:
                connection.rollback()
                raise
            else:
                connection.commit()

    def _initialize(self) -> None:
        with self._connection() as connection:
            journal_mode = str(connection.execute("PRAGMA journal_mode = WAL").fetchone()[0])
            if journal_mode.lower() != "wal":
                raise RegistryError(
                    f"registry requires SQLite WAL on local disk, got journal_mode={journal_mode!r}"
                )
            connection.execute("PRAGMA synchronous = FULL")
            connection.execute("BEGIN IMMEDIATE")
            try:
                version = int(connection.execute("PRAGMA user_version").fetchone()[0])
                if version not in {0, SCHEMA_VERSION}:
                    raise RegistrySchemaError(
                        f"registry schema version {version} is incompatible with {SCHEMA_VERSION}"
                    )
                if version == 0:
                    self._create_schema(connection)
                    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    @staticmethod
    def _create_schema(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE async_trial_batches (
                batch_id TEXT PRIMARY KEY,
                request_id TEXT NOT NULL UNIQUE,
                client_batch_id TEXT NOT NULL,
                trainer_run_id TEXT,
                batching_key_json TEXT NOT NULL,
                state TEXT NOT NULL,
                revision INTEGER NOT NULL,
                requested_trials INTEGER NOT NULL,
                queued_trials INTEGER NOT NULL,
                running_trials INTEGER NOT NULL,
                succeeded_trials INTEGER NOT NULL,
                failed_trials INTEGER NOT NULL,
                cancelled_trials INTEGER NOT NULL,
                deadline_exceeded_trials INTEGER NOT NULL,
                stale_trials INTEGER NOT NULL,
                result_manifest_uri TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE trial_executions (
                trial_execution_id TEXT PRIMARY KEY,
                batch_id TEXT NOT NULL REFERENCES async_trial_batches(batch_id) ON DELETE CASCADE,
                ordinal INTEGER NOT NULL,
                client_trial_id TEXT NOT NULL,
                session_id TEXT,
                task_id TEXT,
                state TEXT NOT NULL,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                metadata_json TEXT NOT NULL,
                result_uri TEXT,
                normalized_error_category TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(batch_id, client_trial_id),
                UNIQUE(batch_id, ordinal)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE enqueue_intents (
                trial_execution_id TEXT PRIMARY KEY
                    REFERENCES trial_executions(trial_execution_id) ON DELETE CASCADE,
                batch_id TEXT NOT NULL REFERENCES async_trial_batches(batch_id) ON DELETE CASCADE,
                payload_json TEXT NOT NULL,
                materialized_at TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE idempotency_records (
                request_id TEXT PRIMARY KEY,
                request_digest TEXT NOT NULL,
                batch_id TEXT NOT NULL UNIQUE
                    REFERENCES async_trial_batches(batch_id) ON DELETE CASCADE,
                response_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT
            )
            """
        )
        connection.execute(
            "CREATE INDEX trial_executions_batch_state_idx ON trial_executions(batch_id, state)"
        )
        connection.execute("CREATE INDEX enqueue_intents_batch_idx ON enqueue_intents(batch_id)")

    @property
    def schema_version(self) -> int:
        with self._connection() as connection:
            return int(connection.execute("PRAGMA user_version").fetchone()[0])

    @staticmethod
    def _validate_admission(request: Mapping[str, Any]) -> tuple[str, str, list[dict[str, Any]]]:
        request_id = request.get("request_id")
        client_batch_id = request.get("client_batch_id")
        trials = request.get("trials")
        if not isinstance(request_id, str) or not request_id.strip():
            raise InvalidAdmissionRequest("request_id must be a non-empty string")
        if not isinstance(client_batch_id, str) or not client_batch_id.strip():
            raise InvalidAdmissionRequest("client_batch_id must be a non-empty string")
        if not isinstance(trials, list) or not trials:
            raise InvalidAdmissionRequest("trials must be a non-empty array")

        normalized_trials: list[dict[str, Any]] = []
        client_trial_ids: set[str] = set()
        for ordinal, trial in enumerate(trials):
            if not isinstance(trial, dict):
                raise InvalidAdmissionRequest(f"trials[{ordinal}] must be a JSON object")
            client_trial_id = trial.get("client_trial_id")
            if not isinstance(client_trial_id, str) or not client_trial_id.strip():
                raise InvalidAdmissionRequest(
                    f"trials[{ordinal}].client_trial_id must be a non-empty string"
                )
            if client_trial_id in client_trial_ids:
                raise InvalidAdmissionRequest(f"duplicate client_trial_id {client_trial_id!r}")
            if not isinstance(trial.get("payload"), dict):
                raise InvalidAdmissionRequest(f"trials[{ordinal}].payload must be a JSON object")
            client_trial_ids.add(client_trial_id)
            normalized_trials.append(dict(trial))
        return request_id, client_batch_id, normalized_trials

    def admit_batch(self, request: Mapping[str, Any]) -> AdmissionResult:
        request_id, client_batch_id, trials = self._validate_admission(request)
        request_digest = canonical_request_digest(request)
        created_at = _now()
        created_at_text = _format_time(created_at)
        expires_at = None
        if self.idempotency_ttl_seconds is not None:
            expires_at = _format_time(
                created_at + timedelta(seconds=self.idempotency_ttl_seconds)
            )

        batch_id = f"atb-{uuid4().hex}"
        trial_records = [
            (trial, f"te-{uuid4().hex}")
            for trial in trials
        ]
        response = {
            "batch_id": batch_id,
            "name": f"async_trial_batches/{batch_id}",
            "state": BatchState.QUEUED.value,
            "revision": 1,
            "requested_trials": len(trial_records),
            "trials": [
                {
                    "client_trial_id": trial["client_trial_id"],
                    "trial_execution_id": trial_execution_id,
                    "state": TrialState.QUEUED.value,
                }
                for trial, trial_execution_id in trial_records
            ],
        }
        response_json = _canonical_json(response)

        with self._write_transaction() as connection:
            existing = connection.execute(
                """
                SELECT request_digest, response_json
                FROM idempotency_records
                WHERE request_id = ?
                """,
                (request_id,),
            ).fetchone()
            if existing is not None:
                if existing["request_digest"] != request_digest:
                    raise IdempotencyConflict(
                        request_id,
                        str(existing["request_digest"]),
                        request_digest,
                    )
                return AdmissionResult(
                    response=json.loads(existing["response_json"]),
                    created=False,
                )

            connection.execute(
                """
                INSERT INTO async_trial_batches (
                    batch_id, request_id, client_batch_id, trainer_run_id,
                    batching_key_json, state, revision, requested_trials,
                    queued_trials, running_trials, succeeded_trials,
                    failed_trials, cancelled_trials, deadline_exceeded_trials,
                    stale_trials, result_manifest_uri, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, 0, 0, 0, 0, NULL, ?, ?)
                """,
                (
                    batch_id,
                    request_id,
                    client_batch_id,
                    request.get("trainer_run_id"),
                    _canonical_json(request.get("batching_key")),
                    BatchState.QUEUED.value,
                    1,
                    len(trial_records),
                    len(trial_records),
                    created_at_text,
                    created_at_text,
                ),
            )
            for ordinal, (trial, trial_execution_id) in enumerate(trial_records):
                metadata = {key: value for key, value in trial.items() if key != "payload"}
                connection.execute(
                    """
                    INSERT INTO trial_executions (
                        trial_execution_id, batch_id, ordinal, client_trial_id,
                        session_id, task_id, state, attempt_count, metadata_json,
                        result_uri, normalized_error_category, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, NULL, NULL, ?, ?)
                    """,
                    (
                        trial_execution_id,
                        batch_id,
                        ordinal,
                        trial["client_trial_id"],
                        trial.get("session_id"),
                        str(trial["task_id"]) if trial.get("task_id") is not None else None,
                        TrialState.QUEUED.value,
                        _canonical_json(metadata),
                        created_at_text,
                        created_at_text,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO enqueue_intents (
                        trial_execution_id, batch_id, payload_json,
                        materialized_at, created_at
                    ) VALUES (?, ?, ?, NULL, ?)
                    """,
                    (
                        trial_execution_id,
                        batch_id,
                        _canonical_json(trial["payload"]),
                        created_at_text,
                    ),
                )
            connection.execute(
                """
                INSERT INTO idempotency_records (
                    request_id, request_digest, batch_id, response_json,
                    created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    request_id,
                    request_digest,
                    batch_id,
                    response_json,
                    created_at_text,
                    expires_at,
                ),
            )
        return AdmissionResult(response=response, created=True)

    def get_admission(self, request_id: str) -> AdmissionResult | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT response_json FROM idempotency_records WHERE request_id = ?",
                (request_id,),
            ).fetchone()
        if row is None:
            return None
        return AdmissionResult(response=json.loads(row["response_json"]), created=False)

    def get_batch(self, batch_id: str) -> dict[str, Any]:
        with self._connection() as connection:
            return self._get_batch(connection, batch_id)

    def _get_batch(self, connection: sqlite3.Connection, batch_id: str) -> dict[str, Any]:
        batch = connection.execute(
            "SELECT * FROM async_trial_batches WHERE batch_id = ?",
            (batch_id,),
        ).fetchone()
        if batch is None:
            raise BatchNotFound(batch_id)
        trials = connection.execute(
            """
            SELECT trial_execution_id, ordinal, client_trial_id, session_id,
                   task_id, state, attempt_count, metadata_json, result_uri,
                   normalized_error_category, created_at, updated_at
            FROM trial_executions
            WHERE batch_id = ?
            ORDER BY ordinal
            """,
            (batch_id,),
        ).fetchall()
        result = dict(batch)
        result["batching_key"] = json.loads(result.pop("batching_key_json"))
        result["trials"] = [
            {
                **{key: value for key, value in dict(trial).items() if key != "metadata_json"},
                "metadata": json.loads(trial["metadata_json"]),
            }
            for trial in trials
        ]
        return result

    def list_enqueue_intents(self, batch_id: str) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT trial_execution_id, batch_id, payload_json,
                       materialized_at, created_at
                FROM enqueue_intents
                WHERE batch_id = ?
                ORDER BY rowid
                """,
                (batch_id,),
            ).fetchall()
        return [
            {
                "trial_execution_id": row["trial_execution_id"],
                "batch_id": row["batch_id"],
                "payload": json.loads(row["payload_json"]),
                "materialized_at": row["materialized_at"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def transition_trial(self, trial_execution_id: str, target: TrialState) -> dict[str, Any]:
        target = TrialState(target)
        updated_at = _format_time(_now())
        with self._write_transaction() as connection:
            trial = connection.execute(
                """
                SELECT batch_id, state
                FROM trial_executions
                WHERE trial_execution_id = ?
                """,
                (trial_execution_id,),
            ).fetchone()
            if trial is None:
                raise TrialNotFound(trial_execution_id)
            current = TrialState(trial["state"])
            if current == target:
                return self._get_batch(connection, str(trial["batch_id"]))
            validate_trial_transition(current, target)
            connection.execute(
                """
                UPDATE trial_executions
                SET state = ?, updated_at = ?
                WHERE trial_execution_id = ?
                """,
                (target.value, updated_at, trial_execution_id),
            )
            batch_id = str(trial["batch_id"])
            counters = self._trial_counters(connection, batch_id)
            batch = connection.execute(
                "SELECT state FROM async_trial_batches WHERE batch_id = ?",
                (batch_id,),
            ).fetchone()
            if batch is None:
                raise BatchNotFound(batch_id)
            current_batch_state = BatchState(batch["state"])
            target_batch_state = self._derive_batch_state(current_batch_state, counters)
            validate_batch_transition(current_batch_state, target_batch_state)
            connection.execute(
                """
                UPDATE async_trial_batches
                SET state = ?, revision = revision + 1,
                    queued_trials = ?, running_trials = ?,
                    succeeded_trials = ?, failed_trials = ?,
                    cancelled_trials = ?, deadline_exceeded_trials = ?,
                    stale_trials = ?, updated_at = ?
                WHERE batch_id = ?
                """,
                (
                    target_batch_state.value,
                    counters["queued_trials"],
                    counters["running_trials"],
                    counters["succeeded_trials"],
                    counters["failed_trials"],
                    counters["cancelled_trials"],
                    counters["deadline_exceeded_trials"],
                    counters["stale_trials"],
                    updated_at,
                    batch_id,
                ),
            )
            return self._get_batch(connection, batch_id)

    @staticmethod
    def _trial_counters(connection: sqlite3.Connection, batch_id: str) -> dict[str, int]:
        row = connection.execute(
            """
            SELECT
                COUNT(*) AS requested_trials,
                SUM(CASE WHEN state = 'QUEUED' THEN 1 ELSE 0 END) AS queued_trials,
                SUM(CASE WHEN state IN ('RUNNING', 'CANCEL_REQUESTED') THEN 1 ELSE 0 END)
                    AS running_trials,
                SUM(CASE WHEN state = 'SUCCEEDED' THEN 1 ELSE 0 END) AS succeeded_trials,
                SUM(CASE WHEN state = 'FAILED' THEN 1 ELSE 0 END) AS failed_trials,
                SUM(CASE WHEN state = 'CANCELLED' THEN 1 ELSE 0 END) AS cancelled_trials,
                SUM(CASE WHEN state = 'DEADLINE_EXCEEDED' THEN 1 ELSE 0 END)
                    AS deadline_exceeded_trials,
                SUM(CASE WHEN state IN ('STALE_PRE_EXECUTION', 'STALE_POST_EXECUTION')
                         THEN 1 ELSE 0 END) AS stale_trials
            FROM trial_executions
            WHERE batch_id = ?
            """,
            (batch_id,),
        ).fetchone()
        return {key: int(row[key] or 0) for key in row.keys()}

    @staticmethod
    def _derive_batch_state(current: BatchState, counters: Mapping[str, int]) -> BatchState:
        terminal_count = (
            counters["succeeded_trials"]
            + counters["failed_trials"]
            + counters["cancelled_trials"]
            + counters["deadline_exceeded_trials"]
            + counters["stale_trials"]
        )
        if terminal_count == counters["requested_trials"]:
            if current == BatchState.CANCEL_REQUESTED:
                return BatchState.CANCELLED
            return BatchState.COMPLETED
        if current == BatchState.CANCEL_REQUESTED:
            return BatchState.CANCEL_REQUESTED
        if counters["running_trials"] > 0 or current == BatchState.RUNNING:
            return BatchState.RUNNING
        return BatchState.QUEUED
