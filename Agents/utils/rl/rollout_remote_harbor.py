#!/usr/bin/env python3
"""Miles/Polar-compatible HTTP front door for Harbor rollout mode.

The HTTP server only accepts RL requests, writes them to a local queue, and
waits for zellij workers to produce results.  Workers run the existing
harboropik.sh path so rollout mode keeps the same logs, local cache, Opik
hooks, and timeout finalization behavior as normal benchmark runs.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import threading
import time
from collections import Counter, deque
from datetime import datetime, timezone
from fcntl import LOCK_EX, LOCK_UN, flock
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

from async_trial_registry import (
    AdmissionResult,
    AsyncTrialRegistry,
    BatchNotFound,
    IdempotencyConflict,
    InvalidAdmissionRequest,
    TrialState,
    TrialStateObservation,
)


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_DATASET_NAME = os.environ.get("RL_DATASET_NAME", "seta")
DEFAULT_DATASET_ROOT = Path(os.environ.get("RL_DATASET_ROOT", "/workspace/seta-env/Harbor-Dataset"))
DEFAULT_MODEL_NAME = os.environ.get("RL_MODEL_NAME", "minimax2.7")
DEFAULT_API_BASE = os.environ.get("RL_API_BASE", "")
DEFAULT_API_KEY = os.environ.get("RL_API_KEY", "")
DEFAULT_API_KEY_MODE = os.environ.get("RL_API_KEY_MODE", "static").strip().lower()
DEFAULT_OPIK_PROJECT_NAME = os.environ.get("OPIK_PROJECT_NAME", "")
DEFAULT_DISABLED_TASK_IDS = os.environ.get("RL_DISABLED_TASK_IDS", "")
DEFAULT_TIMEOUT = float(os.environ.get("RL_REQUEST_TIMEOUT", "3600"))
TRACE_LOG = Path(os.environ.get("RL_TRACE_LOG", "/workspace/runs/rl-rollout-requests.jsonl"))
QUEUE_DIR = Path(os.environ.get("RL_QUEUE_DIR", "/workspace/runs/rl-rollout-queue"))
PENDING_DIR = QUEUE_DIR / "pending"
RESULTS_DIR = QUEUE_DIR / "results"
ACTIVE_DIR = Path(os.environ.get("RL_ACTIVE_DIR", str(QUEUE_DIR / "active")))
JOB_QUEUE_ROOT = Path(os.environ.get("RL_JOB_QUEUE_ROOT", str(QUEUE_DIR / "jobs")))
JOB_RUNTIME_ROOT = Path(os.environ.get("RL_JOB_RUNTIME_ROOT", str(TRACE_LOG.parent / "rl-jobs")))
ENABLE_DYNAMIC_JOB_ZELLIJ = os.environ.get("RL_DYNAMIC_JOB_ZELLIJ", "1").strip().lower() not in {"0", "false", "no", "off"}
ENABLE_ASYNC_TRIAL_BATCHES = os.environ.get(
    "RL_ASYNC_TRIAL_BATCHES_ENABLED", "0"
).strip().lower() in {"1", "true", "yes", "on"}
ASYNC_TRIAL_REGISTRY_PATH = Path(
    os.environ.get("RL_ASYNC_TRIAL_REGISTRY_PATH", str(QUEUE_DIR / "async-trial-registry.sqlite3"))
)
ASYNC_MAX_TRIALS_PER_BATCH = int(os.environ.get("RL_ASYNC_MAX_TRIALS_PER_BATCH", "256"))
ASYNC_MAX_REQUEST_BYTES = int(
    os.environ.get("RL_ASYNC_MAX_REQUEST_BYTES", str(32 * 1024 * 1024))
)
ASYNC_MAX_BULK_STATUS_IDS = int(os.environ.get("RL_ASYNC_MAX_BULK_STATUS_IDS", "128"))
JOB_ZELLIJ_LOCKS: dict[str, threading.Lock] = {}
JOB_ZELLIJ_READY: dict[str, str] = {}
JOB_ZELLIJ_LOCKS_GUARD = threading.Lock()
ASYNC_MATERIALIZATION_LOCKS = tuple(threading.Lock() for _ in range(256))
ASYNC_REGISTRY: AsyncTrialRegistry | None = None
ASYNC_REGISTRY_GUARD = threading.Lock()
ASYNC_BATCH_ID_PATTERN = re.compile(r"atb-[0-9a-f]{32}\Z")
TRACE_WARNING_GUARD = threading.Lock()
TRACE_WARNING_LAST_AT = 0.0


class AsyncControlPlaneMetrics:
    """Small process-local counters used to explain async control-plane health."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._started = time.monotonic()
        self._counters: Counter[str] = Counter()
        self._rejections: Counter[str] = Counter()
        self._status_requests: deque[float] = deque()

    def record(self, name: str, *, rejection_category: str | None = None) -> None:
        now = time.monotonic()
        with self._lock:
            self._counters[name] += 1
            if rejection_category is not None:
                self._rejections[rejection_category] += 1
            if name == "status_requests":
                self._status_requests.append(now)
                self._trim_status_requests(now)

    def snapshot(self) -> dict[str, Any]:
        now = time.monotonic()
        with self._lock:
            self._trim_status_requests(now)
            return {
                "uptime_seconds": round(now - self._started, 3),
                "status_qps_60s": round(len(self._status_requests) / 60.0, 3),
                "counters": dict(sorted(self._counters.items())),
                "rejection_categories": dict(sorted(self._rejections.items())),
            }

    def _trim_status_requests(self, now: float) -> None:
        cutoff = now - 60.0
        while self._status_requests and self._status_requests[0] < cutoff:
            self._status_requests.popleft()


ASYNC_METRICS = AsyncControlPlaneMetrics()


class AsyncRequestBodyTooLarge(ValueError):
    """Raised before reading an oversized async admission request body."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _redact_sensitive(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _redact_sensitive(item)
            for key, item in value.items()
            if key.lower() not in {"api_key", "authorization", "proxy-authorization"}
        }
    if isinstance(value, list):
        return [_redact_sensitive(item) for item in value]
    if isinstance(value, str) and DEFAULT_API_KEY and DEFAULT_API_KEY in value:
        return value.replace(DEFAULT_API_KEY, "<redacted>")
    return value


def _append_trace(event: dict[str, Any]) -> bool:
    """Best-effort append; telemetry failures must never change accepted work."""

    global TRACE_WARNING_LAST_AT
    try:
        TRACE_LOG.parent.mkdir(parents=True, exist_ok=True)
        safe_event = _redact_sensitive(event)
        with TRACE_LOG.open("a", encoding="utf-8") as handle:
            flock(handle.fileno(), LOCK_EX)
            try:
                handle.write(json.dumps(safe_event, ensure_ascii=True, sort_keys=True) + "\n")
                handle.flush()
            finally:
                flock(handle.fileno(), LOCK_UN)
        return True
    except Exception as exc:
        ASYNC_METRICS.record("trace_write_failures")
        with TRACE_WARNING_GUARD:
            now = time.monotonic()
            if now - TRACE_WARNING_LAST_AT >= 60.0:
                print(
                    f"warning: failed to append rollout trace: {type(exc).__name__}",
                    flush=True,
                )
                TRACE_WARNING_LAST_AT = now
        return False


def _record_async_event(event: str, **fields: Any) -> bool:
    return _append_trace(
        {
            "event": event,
            "event_schema_version": 1,
            "component": "async_trial_control_plane",
            "timestamp": _now(),
            **fields,
        }
    )


def _async_registry() -> AsyncTrialRegistry:
    global ASYNC_REGISTRY
    with ASYNC_REGISTRY_GUARD:
        if ASYNC_REGISTRY is None or ASYNC_REGISTRY.path != ASYNC_TRIAL_REGISTRY_PATH:
            ASYNC_REGISTRY = AsyncTrialRegistry(ASYNC_TRIAL_REGISTRY_PATH)
        return ASYNC_REGISTRY


def _async_materialization_lock(trial_execution_id: str) -> threading.Lock:
    return ASYNC_MATERIALIZATION_LOCKS[hash(trial_execution_id) % len(ASYNC_MATERIALIZATION_LOCKS)]


def _metadata(request: dict[str, Any]) -> dict[str, Any]:
    value = request.get("metadata")
    return value if isinstance(value, dict) else {}


def _trial_config(request: dict[str, Any]) -> dict[str, Any]:
    value = request.get("trial_config")
    return value if isinstance(value, dict) else {}


def _first_nonempty(*values: Any) -> str:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _safe_slug(value: str, *, fallback: str = "default") -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip("-")
    return slug or fallback


def _short_suffix(value: str, width: int = 6) -> str:
    value = str(value or "").strip()
    return value[-width:] if value else ""


def _extract_ray_submission_id(request: dict[str, Any]) -> str:
    ray_submission_id = _first_nonempty(request.get("ray_submission_id"))
    if not ray_submission_id:
        raise ValueError("top-level ray_submission_id is required in rollout mode")
    return ray_submission_id


def _extract_opik_project_name(request: dict[str, Any], ray_submission_id: str) -> str:
    return _first_nonempty(
        request.get("opik_project_name"),
        ray_submission_id,
        DEFAULT_OPIK_PROJECT_NAME,
    )


def _extract_polar_task_id(request: dict[str, Any], session_id: str) -> str:
    meta = _metadata(request)
    trial = _trial_config(request)
    return _first_nonempty(
        request.get("polar_task_id"),
        request.get("polar_task"),
        request.get("rl_task_id"),
        meta.get("polar_task_id"),
        meta.get("polar_task"),
        meta.get("rl_task_id"),
        trial.get("polar_task_id"),
        trial.get("polar_task"),
        trial.get("rl_task_id"),
        request.get("session_id"),
        session_id,
    )


def _display_name(task_name: str, polar_task_id: str, session_id: str) -> str:
    suffix = _short_suffix(polar_task_id or session_id)
    return f"{task_name}-{suffix}" if suffix else task_name


def _queue_for_submission(ray_submission_id: str) -> Path:
    if not ray_submission_id:
        return QUEUE_DIR
    return JOB_QUEUE_ROOT / _safe_slug(ray_submission_id)


def _elapsed_ms_since(timestamp: str | None) -> float | None:
    if not timestamp:
        return None
    try:
        started = datetime.fromisoformat(timestamp)
    except ValueError:
        return None
    return round((datetime.now(timezone.utc) - started).total_seconds() * 1000, 3)


def _async_queue_depth() -> dict[str, int]:
    # Results are durable history rather than outstanding queue depth and may
    # grow for the full retention window, so health probes do not scan them.
    queue_roots = {QUEUE_DIR}
    if JOB_QUEUE_ROOT.is_dir():
        queue_roots.update(path for path in JOB_QUEUE_ROOT.iterdir() if path.is_dir())
    return {
        state: sum(len(list((root / state).glob("*.json"))) for root in queue_roots)
        for state in ("pending", "active")
    }


def _async_health_snapshot() -> dict[str, Any]:
    if not ENABLE_ASYNC_TRIAL_BATCHES:
        return {
            "ready": False,
            "reason": "disabled",
            "metrics": ASYNC_METRICS.snapshot(),
        }
    try:
        registry = _async_registry().health_snapshot()
        return {
            "ready": bool(registry["ready"]),
            "registry": registry,
            "queue_depth": _async_queue_depth(),
            "metrics": ASYNC_METRICS.snapshot(),
        }
    except Exception as exc:
        ASYNC_METRICS.record("health_failures")
        return {
            "ready": False,
            "error_category": "REGISTRY_UNAVAILABLE",
            "exception_type": type(exc).__name__,
            "metrics": ASYNC_METRICS.snapshot(),
        }


def _submission_session_name(ray_submission_id: str, dataset_name: str) -> str:
    agent_slug = _safe_slug(os.environ.get("RL_AGENT", "claude-code"))
    dataset_slug = _safe_slug(dataset_name)
    submission_slug = _safe_slug(ray_submission_id)
    return f"harbor-rollout-{agent_slug}-{dataset_slug}-{submission_slug}"


def _job_lock(job_slug: str) -> threading.Lock:
    with JOB_ZELLIJ_LOCKS_GUARD:
        lock = JOB_ZELLIJ_LOCKS.get(job_slug)
        if lock is None:
            lock = threading.Lock()
            JOB_ZELLIJ_LOCKS[job_slug] = lock
        return lock


def _run_helper(cmd: list[str], *, cwd: str, env: dict[str, str], timeout: float) -> tuple[int, str, str]:
    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        # Kill the whole process group; otherwise a timed-out helper can leave
        # child flock processes behind and block every following RL request.
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        stdout, stderr = proc.communicate()
        raise TimeoutError(
            f"{cmd!r} timed out after {timeout:.1f}s; "
            f"stdout={stdout.strip()!r}; stderr={stderr.strip()!r}"
        ) from exc
    return proc.returncode, stdout, stderr


def _zellij_session_exists(session_name: str) -> bool:
    try:
        result = subprocess.run(
            ["zellij", "list-sessions", "--short"],
            cwd=str(SCRIPT_DIR),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if result.returncode != 0:
        return False
    return session_name in {line.strip() for line in result.stdout.splitlines() if line.strip()}


def _cached_job_session(job_slug: str) -> str:
    with JOB_ZELLIJ_LOCKS_GUARD:
        return JOB_ZELLIJ_READY.get(job_slug, "")


def _clear_cached_job_session(job_slug: str, session_name: str) -> None:
    with JOB_ZELLIJ_LOCKS_GUARD:
        if JOB_ZELLIJ_READY.get(job_slug) == session_name:
            JOB_ZELLIJ_READY.pop(job_slug, None)


def _ensure_submission_zellij(
    ray_submission_id: str,
    dataset_name: str,
    queue_dir: Path,
    model_name: str,
    opik_project_name: str,
) -> str:
    if not ray_submission_id:
        raise ValueError("ray_submission_id is required in rollout mode so a worker zellij session can be started")
    if not ENABLE_DYNAMIC_JOB_ZELLIJ:
        raise RuntimeError("RL_DYNAMIC_JOB_ZELLIJ=0 is unsupported without a prestarted worker pool")
    submission_slug = _safe_slug(ray_submission_id)
    expected_session = _submission_session_name(ray_submission_id, dataset_name)
    ready_session = _cached_job_session(submission_slug)
    if ready_session:
        if _zellij_session_exists(ready_session):
            return ready_session
        _clear_cached_job_session(submission_slug, ready_session)

    lock = _job_lock(submission_slug)
    with lock:
        ready_session = _cached_job_session(submission_slug)
        if ready_session:
            if _zellij_session_exists(ready_session):
                return ready_session
            _clear_cached_job_session(submission_slug, ready_session)

        script = SCRIPT_DIR / "ensure_rl_job_zellij.sh"
        if not script.exists():
            raise FileNotFoundError(f"job zellij helper not found: {script}")
        env = os.environ.copy()
        env.update({
            "RL_ZELLIJ_SUBMISSION_ID": ray_submission_id,
            "RL_ZELLIJ_JOB_QUEUE_DIR": str(queue_dir),
            "RL_JOB_RUNTIME_ROOT": str(JOB_RUNTIME_ROOT),
            "RL_MODEL_NAME": model_name,
            "OPIK_PROJECT_NAME": opik_project_name,
        })
        returncode, stdout, stderr = _run_helper(
            [str(script), ray_submission_id, dataset_name, str(queue_dir)],
            cwd=str(SCRIPT_DIR),
            env=env,
            timeout=float(os.environ.get("RL_JOB_ZELLIJ_START_TIMEOUT", "45")),
        )
        if returncode != 0:
            raise RuntimeError(
                "failed to ensure submission zellij session "
                f"for ray_submission_id={ray_submission_id!r}: {stderr or stdout}"
            )
        session_name = stdout.strip().splitlines()[-1] if stdout.strip() else expected_session
        with JOB_ZELLIJ_LOCKS_GUARD:
            JOB_ZELLIJ_READY[submission_slug] = session_name
        return session_name


def _parse_task_ids(value: str | None) -> set[str]:
    return {item.strip() for item in (value or "").replace(";", ",").split(",") if item.strip()}


def _disabled_task_ids() -> set[str]:
    return _parse_task_ids(DEFAULT_DISABLED_TASK_IDS)


def _dataset_roots() -> dict[str, Path]:
    roots = {DEFAULT_DATASET_NAME: DEFAULT_DATASET_ROOT}
    raw_roots = os.environ.get("RL_DATASET_ROOTS", "")
    for item in raw_roots.split(","):
        item = item.strip()
        if not item:
            continue
        if "=" in item:
            name, path = item.split("=", 1)
            roots[name.strip()] = Path(path.strip())
        else:
            roots[Path(item).name] = Path(item)
    return roots


def _task_sort_key(path: Path) -> tuple[int, int | str]:
    return (0, int(path.name)) if path.name.isdigit() else (1, path.name)


def _dataset_root(dataset_name: str | None = None, dataset_root: str | None = None) -> Path:
    roots = _dataset_roots()
    root = Path(dataset_root) if dataset_root else roots.get(dataset_name or DEFAULT_DATASET_NAME)
    if root is None:
        raise ValueError(f"unknown dataset_name={dataset_name!r}; known={sorted(roots)}")
    root = root.resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"dataset root does not exist: {root}")
    return root


def list_dataset_tasks(
    dataset_name: str | None = None,
    dataset_root: str | None = None,
    *,
    include_disabled: bool = False,
) -> list[str]:
    root = _dataset_root(dataset_name, dataset_root)
    disabled = set() if include_disabled else _disabled_task_ids()
    return [
        path.name
        for path in sorted((item for item in root.iterdir() if item.is_dir()), key=_task_sort_key)
        if path.name not in disabled
    ]


def resolve_task_path(request: dict[str, Any]) -> Path:
    dataset_root = _dataset_root(request.get("dataset_name"), request.get("dataset_root"))
    raw_task = request.get("task_path") or request.get("task_id")
    if not raw_task:
        raise ValueError("task_id or task_path is required")
    task_path = Path(raw_task)
    if not task_path.is_absolute():
        task_path = dataset_root / task_path
    task_path = task_path.resolve()
    try:
        task_path.relative_to(dataset_root)
    except ValueError as exc:
        raise ValueError(f"task path {task_path} is outside dataset root {dataset_root}") from exc
    if not task_path.is_dir():
        raise FileNotFoundError(f"task path does not exist: {task_path}")
    if task_path.name in _disabled_task_ids():
        raise ValueError(f"task id {task_path.name} is disabled for dataset {request.get('dataset_name') or DEFAULT_DATASET_NAME}")
    return task_path


def _without_client_api_keys(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _without_client_api_keys(item)
            for key, item in value.items()
            if key.lower() != "api_key"
        }
    if isinstance(value, list):
        return [_without_client_api_keys(item) for item in value]
    return value


# Validate the batch envelope and every trial before any durable state is written.
# This also removes client-supplied API keys, applies batch-level defaults, merges
# envelope metadata into each legacy run_trial payload, rejects conflicting or
# duplicate identifiers, and verifies task routing inputs. The resulting canonical
# request is the value used for idempotency checks, persistence, and queue handoff.
def _validate_and_normalize_async_batch_request(
    request: dict[str, Any],
) -> dict[str, Any]:
    request_id = request.get("request_id")
    client_batch_id = request.get("client_batch_id")
    trials = request.get("trials")
    if not isinstance(request_id, str) or not request_id.strip():
        raise ValueError("request_id must be a non-empty string")
    if not isinstance(client_batch_id, str) or not client_batch_id.strip():
        raise ValueError("client_batch_id must be a non-empty string")
    if not isinstance(trials, list) or not trials:
        raise ValueError("trials must be a non-empty array")
    if len(trials) > ASYNC_MAX_TRIALS_PER_BATCH:
        raise ValueError(
            f"trials exceeds RL_ASYNC_MAX_TRIALS_PER_BATCH={ASYNC_MAX_TRIALS_PER_BATCH}"
        )

    batching_key = request.get("batching_key")
    batching_defaults = batching_key if isinstance(batching_key, dict) else {}
    normalized_trials: list[dict[str, Any]] = []
    client_trial_ids: set[str] = set()
    for ordinal, trial in enumerate(trials):
        if not isinstance(trial, dict):
            raise ValueError(f"trials[{ordinal}] must be a JSON object")
        client_trial_id = trial.get("client_trial_id")
        if not isinstance(client_trial_id, str) or not client_trial_id.strip():
            raise ValueError(f"trials[{ordinal}].client_trial_id must be a non-empty string")
        if client_trial_id in client_trial_ids:
            raise ValueError(f"duplicate client_trial_id {client_trial_id!r}")
        client_trial_ids.add(client_trial_id)

        raw_payload = trial.get("payload")
        if not isinstance(raw_payload, dict):
            raise ValueError(f"trials[{ordinal}].payload must be a JSON object")
        payload = _without_client_api_keys(raw_payload)
        for key in ("dataset_name", "dataset_root", "ray_submission_id"):
            if payload.get(key) in (None, "") and batching_defaults.get(key) not in (None, ""):
                payload[key] = batching_defaults[key]
        for key in ("session_id", "task_id"):
            envelope_value = trial.get(key)
            payload_value = payload.get(key)
            if envelope_value not in (None, "") and payload_value not in (None, ""):
                if str(envelope_value) != str(payload_value):
                    raise ValueError(
                        f"trials[{ordinal}].{key} conflicts with trials[{ordinal}].payload.{key}"
                    )
            elif envelope_value not in (None, ""):
                payload[key] = envelope_value
        if payload.get("session_id") in (None, ""):
            payload["session_id"] = client_trial_id

        metadata = dict(_metadata(payload))
        for key in ("group_id", "rollout_step", "policy_version"):
            if key in trial and key not in metadata:
                metadata[key] = trial[key]
        if metadata:
            payload["metadata"] = metadata

        resolve_task_path(payload)
        _dataset_root(payload.get("dataset_name"), payload.get("dataset_root"))
        try:
            _extract_ray_submission_id(payload)
        except ValueError as exc:
            raise ValueError(
                f"trials[{ordinal}].payload requires top-level ray_submission_id in rollout mode"
            ) from exc

        normalized_trial = dict(trial)
        normalized_trial["session_id"] = str(payload["session_id"])
        normalized_trial["task_id"] = str(payload.get("task_id") or payload.get("task_path"))
        normalized_trial["payload"] = payload
        normalized_trials.append(normalized_trial)

    normalized_request = dict(request)
    normalized_request["trials"] = normalized_trials
    return normalized_request


def _queue_artifact_exists(queue_dir: Path, trial_execution_id: str) -> bool:
    return any(
        (queue_dir / state / f"{trial_execution_id}.json").exists()
        for state in ("pending", "active", "results")
    )


def _async_execution_api_key(request: dict[str, Any]) -> str:
    session_id = str(request.get("session_id") or "")
    if DEFAULT_API_KEY_MODE == "session":
        return session_id
    return DEFAULT_API_KEY or session_id


def _materialize_enqueue_intent(
    registry: AsyncTrialRegistry,
    intent: dict[str, Any],
    trial_mapping: dict[str, Any],
    zellij_sessions: dict[tuple[str, str], str],
    *,
    async_request_id: str,
) -> None:
    trial_execution_id = str(intent["trial_execution_id"])
    with _async_materialization_lock(trial_execution_id):
        if TrialState(str(trial_mapping["state"])) in {
            TrialState.SUCCEEDED,
            TrialState.FAILED,
        }:
            materialized_at = registry.mark_enqueue_intent_materialized(trial_execution_id)
            _record_async_event(
                "async_enqueue_intent_closed",
                request_id=async_request_id,
                batch_id=intent["batch_id"],
                trial_execution_id=trial_execution_id,
                client_trial_id=trial_mapping["client_trial_id"],
                state=trial_mapping["state"],
                materialized_at=materialized_at,
            )
            return
        request = dict(intent["payload"])
        request["api_key"] = _async_execution_api_key(request)
        ray_submission_id = _extract_ray_submission_id(request)
        queue_dir = _queue_for_submission(ray_submission_id)
        queue_artifact_created = not _queue_artifact_exists(queue_dir, trial_execution_id)
        if queue_artifact_created:
            dataset_name = str(request.get("dataset_name") or DEFAULT_DATASET_NAME)
            model_name = str(request.get("model_name") or DEFAULT_MODEL_NAME)
            opik_project_name = _extract_opik_project_name(request, ray_submission_id)
            zellij_key = (ray_submission_id, dataset_name)
            zellij_session = zellij_sessions.get(zellij_key)
            if zellij_session is None:
                zellij_session = _ensure_submission_zellij(
                    ray_submission_id,
                    dataset_name,
                    queue_dir,
                    model_name,
                    opik_project_name,
                )
                zellij_sessions[zellij_key] = zellij_session
            request.update(
                {
                    "request_id": trial_execution_id,
                    "batch_id": intent["batch_id"],
                    "trial_execution_id": trial_execution_id,
                    "client_trial_id": trial_mapping["client_trial_id"],
                    "async_request_id": async_request_id,
                    "async_admitted_at": intent["created_at"],
                }
            )
            _enqueue_request(request, zellij_session=zellij_session)
        materialized_at = registry.mark_enqueue_intent_materialized(trial_execution_id)
        _record_async_event(
            "async_trial_queued",
            request_id=async_request_id,
            batch_id=intent["batch_id"],
            trial_execution_id=trial_execution_id,
            client_trial_id=trial_mapping["client_trial_id"],
            queue_artifact_created=queue_artifact_created,
            queue_latency_ms=_elapsed_ms_since(str(intent["created_at"])),
            materialized_at=materialized_at,
        )


def reconcile_async_batch(batch_id: str) -> int:
    """Materialize all durable intents that do not yet have a queue artifact."""

    registry = _async_registry()
    batch = registry.get_batch(batch_id)
    trial_mappings = {
        trial["trial_execution_id"]: trial
        for trial in batch["trials"]
    }
    zellij_sessions: dict[tuple[str, str], str] = {}
    materialized = 0
    for intent in registry.list_enqueue_intents(batch_id, unmaterialized_only=True):
        trial_execution_id = str(intent["trial_execution_id"])
        _materialize_enqueue_intent(
            registry,
            intent,
            trial_mappings[trial_execution_id],
            zellij_sessions,
            async_request_id=str(batch["request_id"]),
        )
        materialized += 1
    return materialized


def _validate_async_batch_id(batch_id: str) -> str:
    if not ASYNC_BATCH_ID_PATTERN.fullmatch(batch_id):
        raise ValueError(f"malformed async trial batch id: {batch_id!r}")
    return batch_id


def _load_worker_result(result_path: Path) -> dict[str, Any]:
    result = json.loads(result_path.read_text(encoding="utf-8"))
    if not isinstance(result, dict):
        raise ValueError("worker result must be a JSON object")
    return result


def _observed_result_state(result_path: Path) -> tuple[TrialState, str | None]:
    try:
        result = _load_worker_result(result_path)
    except (OSError, ValueError):
        return TrialState.FAILED, "MALFORMED_WORKER_RESULT"
    if result.get("ok") is True:
        return TrialState.SUCCEEDED, None
    return TrialState.FAILED, "WORKER_RESULT_FAILED"


def reconcile_async_batch_status(
    batch_id: str,
    *,
    fail_missing_materialized: bool = False,
) -> dict[str, Any]:
    """Reconcile durable trial state from the existing file queue.

    Stable snapshots remain read-only: SQLite takes a write transaction only
    when a queue artifact reveals a state or terminal metadata change.
    Startup recovery may additionally classify a previously materialized trial
    with no remaining queue artifact as failed instead of executing it again.
    """

    batch_id = _validate_async_batch_id(batch_id)
    registry = _async_registry()
    batch = registry.get_batch(batch_id)
    observations: dict[str, TrialStateObservation] = {}
    transition_events: list[dict[str, Any]] = []
    for record in registry.list_trial_reconciliation_records(batch_id):
        trial_execution_id = str(record["trial_execution_id"])
        payload = record["payload"]
        queue_dir = _queue_for_submission(_extract_ray_submission_id(payload))
        pending_path = queue_dir / "pending" / f"{trial_execution_id}.json"
        active_path = queue_dir / "active" / f"{trial_execution_id}.json"
        result_path = queue_dir / "results" / f"{trial_execution_id}.json"

        observation: TrialStateObservation | None = None
        if result_path.exists():
            state, error_category = _observed_result_state(result_path)
            observation = TrialStateObservation(
                state=state,
                result_uri=str(result_path),
                normalized_error_category=error_category,
            )
        elif active_path.exists():
            observation = TrialStateObservation(TrialState.RUNNING)
        elif pending_path.exists():
            observation = TrialStateObservation(TrialState.QUEUED)

        current = TrialState(record["state"])
        if observation is None:
            if (
                fail_missing_materialized
                and record["materialized_at"] is not None
                and current not in {TrialState.SUCCEEDED, TrialState.FAILED}
            ):
                observation = TrialStateObservation(
                    TrialState.FAILED,
                    normalized_error_category="QUEUE_ARTIFACT_MISSING",
                )
            else:
                continue
        if (
            current in {TrialState.SUCCEEDED, TrialState.FAILED}
            and current != observation.state
        ):
            continue
        state_changed = current != observation.state and not (
            current == TrialState.RUNNING and observation.state == TrialState.QUEUED
        )
        result_uri_missing = observation.result_uri is not None and record["result_uri"] is None
        error_missing = (
            observation.normalized_error_category is not None
            and record["normalized_error_category"] is None
        )
        if state_changed or result_uri_missing or error_missing:
            observations[trial_execution_id] = observation
            transition_events.append(
                {
                    "request_id": batch["request_id"],
                    "batch_id": batch_id,
                    "trial_execution_id": trial_execution_id,
                    "client_trial_id": record["client_trial_id"],
                    "from_state": current.value,
                    "to_state": observation.state.value,
                    "error_category": observation.normalized_error_category,
                    "state_observation_latency_ms": _elapsed_ms_since(record["updated_at"]),
                }
            )

    if observations:
        snapshot = registry.reconcile_batch_trial_states(batch_id, observations)
        for event in transition_events:
            _record_async_event("async_trial_state_observed", **event)
        return snapshot
    return registry.get_batch_snapshot(batch_id)


def reconcile_async_state_on_startup() -> dict[str, int]:
    """Recover committed async work before the HTTP listener accepts traffic."""

    started = time.monotonic()
    registry = _async_registry()
    batch_ids = registry.list_recoverable_batch_ids()
    summary = {
        "batches_scanned": len(batch_ids),
        "intents_materialized": 0,
        "completed_batches": 0,
    }
    for batch_id in batch_ids:
        summary["intents_materialized"] += reconcile_async_batch(batch_id)
        snapshot = reconcile_async_batch_status(
            batch_id,
            fail_missing_materialized=True,
        )
        if snapshot["state"] == "COMPLETED":
            summary["completed_batches"] += 1
    _record_async_event(
        "async_startup_reconciliation_completed",
        **summary,
        duration_ms=round((time.monotonic() - started) * 1000, 3),
    )
    ASYNC_METRICS.record("startup_reconciliations")
    return summary


def _parse_async_batch_ids(query: dict[str, list[str]]) -> list[str]:
    batch_ids = [
        item.strip()
        for value in query.get("ids", [])
        for item in value.split(",")
        if item.strip()
    ]
    if not batch_ids:
        raise ValueError("ids must contain at least one async trial batch id")
    if len(batch_ids) > ASYNC_MAX_BULK_STATUS_IDS:
        raise ValueError(
            "ids exceeds "
            f"RL_ASYNC_MAX_BULK_STATUS_IDS={ASYNC_MAX_BULK_STATUS_IDS}"
        )
    return list(dict.fromkeys(_validate_async_batch_id(batch_id) for batch_id in batch_ids))


def get_async_batch_snapshots(batch_ids: list[str]) -> dict[str, Any]:
    registry = _async_registry()
    existing, missing_ids = registry.get_batch_snapshots(batch_ids)
    snapshots = [
        reconcile_async_batch_status(str(snapshot["batch_id"]))
        for snapshot in existing
    ]
    return {
        "batches": snapshots,
        "missing_ids": missing_ids,
    }


def get_async_batch_results(batch_id: str) -> dict[str, Any]:
    """Return terminal worker results using the existing /run_trial payload shape."""

    batch_id = _validate_async_batch_id(batch_id)
    reconcile_async_batch_status(batch_id)
    manifest = _async_registry().get_batch_result_records(batch_id)
    records = manifest.pop("results")
    delivered: list[dict[str, Any]] = []
    unavailable = 0

    for record in records:
        entry = {
            "client_trial_id": record["client_trial_id"],
            "trial_execution_id": record["trial_execution_id"],
            "state": record["state"],
        }
        error_category = record["error_category"]
        result_uri = record["result_uri"]
        try:
            if not isinstance(result_uri, str) or not result_uri:
                raise FileNotFoundError("terminal trial has no result artifact")
            entry["result"] = _redact_sensitive(_load_worker_result(Path(result_uri)))
            if error_category is not None:
                entry["error_category"] = error_category
        except (OSError, ValueError):
            unavailable += 1
            entry["result"] = None
            entry["error"] = {
                "category": error_category or "RESULT_ARTIFACT_UNAVAILABLE",
            }
        delivered.append(entry)

    manifest["available_results"] = len(delivered) - unavailable
    manifest["unavailable_results"] = unavailable
    manifest["results"] = delivered
    return manifest


def _submit_async_trial_batch(request: dict[str, Any]) -> AdmissionResult:
    started = time.monotonic()
    normalized_request = _validate_and_normalize_async_batch_request(request)
    registry = _async_registry()
    admission = registry.admit_batch(normalized_request)
    admission_latency_ms = round((time.monotonic() - started) * 1000, 3)
    outcome = "created" if admission.created else "recovered"
    ASYNC_METRICS.record(f"admissions_{outcome}")
    _record_async_event(
        "async_batch_admitted",
        request_id=normalized_request["request_id"],
        batch_id=admission.batch_id,
        client_batch_id=normalized_request["client_batch_id"],
        trainer_run_id=normalized_request.get("trainer_run_id"),
        requested_trials=len(normalized_request["trials"]),
        admission_outcome=outcome,
        admission_latency_ms=admission_latency_ms,
    )
    materialization_started = time.monotonic()
    materialized = reconcile_async_batch(admission.batch_id)
    _record_async_event(
        "async_batch_materialized",
        request_id=normalized_request["request_id"],
        batch_id=admission.batch_id,
        requested_trials=len(normalized_request["trials"]),
        intents_materialized=materialized,
        materialization_latency_ms=round(
            (time.monotonic() - materialization_started) * 1000,
            3,
        ),
    )
    return admission


def _enqueue_request(
    request: dict[str, Any],
    *,
    zellij_session: str | None = None,
) -> tuple[str, Path]:
    request_id = request.get("request_id") or uuid4().hex[:12]
    session_id = request.get("session_id") or uuid4().hex
    task_path = resolve_task_path(request)
    dataset_root = _dataset_root(request.get("dataset_name"), request.get("dataset_root"))
    dataset_name = request.get("dataset_name") or DEFAULT_DATASET_NAME
    model_name = request.get("model_name") or DEFAULT_MODEL_NAME
    ray_submission_id = _extract_ray_submission_id(request)
    opik_project_name = _extract_opik_project_name(request, ray_submission_id)
    polar_task_id = _extract_polar_task_id(request, session_id)
    display_name = _display_name(task_path.name, polar_task_id, session_id)
    queue_dir = _queue_for_submission(ray_submission_id)
    pending_dir = queue_dir / "pending"
    results_dir = queue_dir / "results"
    active_dir = queue_dir / "active"
    zellij_session = zellij_session or _ensure_submission_zellij(
        ray_submission_id,
        dataset_name,
        queue_dir,
        model_name,
        opik_project_name,
    )
    payload = {
        **request,
        "request_id": request_id,
        "session_id": session_id,
        "ray_submission_id": ray_submission_id,
        "polar_task_id": polar_task_id,
        "display_name": display_name,
        "task_id": task_path.name,
        "task_path": str(task_path),
        "dataset_name": dataset_name,
        "dataset_root": str(dataset_root),
        "model_name": model_name,
        "opik_project_name": opik_project_name,
        "api_base": request.get("api_base") or os.environ.get("RL_API_BASE", ""),
        "api_key": request.get("api_key") or DEFAULT_API_KEY,
        "api_key_mode": DEFAULT_API_KEY_MODE,
        "queue_dir": str(queue_dir),
        "zellij_session": zellij_session,
        "created_at": _now(),
    }
    pending_dir.mkdir(parents=True, exist_ok=True)
    results_dir.mkdir(parents=True, exist_ok=True)
    active_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = pending_dir / f"{request_id}.json.tmp"
    final_path = pending_dir / f"{request_id}.json"
    tmp_path.write_text(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True), encoding="utf-8")
    tmp_path.replace(final_path)
    _append_trace({
        "event": "queued",
        "timestamp": _now(),
        "request_id": request_id,
        "task_id": task_path.name,
        "display_name": display_name,
        "session_id": session_id,
        "ray_submission_id": ray_submission_id,
        "polar_task_id": polar_task_id,
        "model_name": model_name,
        "opik_project_name": opik_project_name,
        "batch_id": request.get("batch_id"),
        "trial_execution_id": request.get("trial_execution_id"),
        "client_trial_id": request.get("client_trial_id"),
        "async_request_id": request.get("async_request_id"),
        "queue_latency_ms": _elapsed_ms_since(request.get("async_admitted_at")),
        "dataset_name": payload["dataset_name"],
        "queue_dir": str(queue_dir),
        "zellij_session": zellij_session,
    })
    return request_id, results_dir / f"{request_id}.json"


def _wait_result(result_path: Path, timeout: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if result_path.exists():
            return json.loads(result_path.read_text(encoding="utf-8"))
        time.sleep(0.5)
    raise TimeoutError(f"timed out waiting for rollout worker result: {result_path}")


class Handler(BaseHTTPRequestHandler):
    server_version = "sii-agent-fleet-rl-rollout/0.2"

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self, *, max_bytes: int | None = None) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or "0")
        if length <= 0:
            return {}
        if max_bytes is not None and length > max_bytes:
            raise AsyncRequestBodyTooLarge(
                f"request body exceeds RL_ASYNC_MAX_REQUEST_BYTES={max_bytes}"
            )
        data = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("request body must be a JSON object")
        return data

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"{self.address_string()} - {fmt % args}", flush=True)

    def _send_async_json(
        self,
        status: HTTPStatus,
        payload: dict[str, Any],
        *,
        operation: str,
        request_id: str | None = None,
        batch_id: str | None = None,
    ) -> bool:
        try:
            self._send_json(status, payload)
            return True
        except OSError as exc:
            ASYNC_METRICS.record("response_delivery_failures")
            _record_async_event(
                "async_response_delivery_failed",
                operation=operation,
                request_id=request_id,
                batch_id=batch_id,
                http_status=int(status),
                error_category="CLIENT_CONNECTION_LOST",
                exception_type=type(exc).__name__,
            )
            return False

    def _send_async_error(
        self,
        status: HTTPStatus,
        exc: Exception,
        *,
        operation: str,
        category: str,
        request_id: str | None = None,
        batch_id: str | None = None,
    ) -> None:
        public_message: Any = str(exc) if int(status) < 500 else "internal server error"
        public_message = _redact_sensitive(public_message)
        detail = {
            "category": category,
            "retryable": int(status) >= 500,
            "exception_type": type(exc).__name__,
            "exception_message": public_message,
        }
        if isinstance(exc, IdempotencyConflict):
            detail["request_id"] = exc.request_id
        ASYNC_METRICS.record("request_rejections", rejection_category=category)
        _record_async_event(
            "async_request_rejected",
            operation=operation,
            request_id=request_id,
            batch_id=batch_id,
            http_status=int(status),
            rejection_category=category,
            exception_type=type(exc).__name__,
        )
        self._send_async_json(
            status,
            {"detail": detail},
            operation=operation,
            request_id=request_id,
            batch_id=batch_id,
        )

    def _handle_async_trial_batch_submit(self) -> None:
        started = time.monotonic()
        request: dict[str, Any] = {}
        try:
            request = self._read_json(max_bytes=ASYNC_MAX_REQUEST_BYTES)
            admission = _submit_async_trial_batch(request)
            delivered = self._send_async_json(
                HTTPStatus.ACCEPTED,
                admission.response,
                operation="submit",
                request_id=str(request.get("request_id") or "") or None,
                batch_id=admission.batch_id,
            )
            if delivered:
                ASYNC_METRICS.record("submit_responses_delivered")
                _record_async_event(
                    "async_submit_response_delivered",
                    request_id=request.get("request_id"),
                    batch_id=admission.batch_id,
                    admission_outcome="created" if admission.created else "recovered",
                    http_status=int(HTTPStatus.ACCEPTED),
                    response_latency_ms=round((time.monotonic() - started) * 1000, 3),
                )
        except AsyncRequestBodyTooLarge as exc:
            self._send_async_error(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                exc,
                operation="submit",
                category="REQUEST_BODY_TOO_LARGE",
            )
        except IdempotencyConflict as exc:
            self._send_async_error(
                HTTPStatus.CONFLICT,
                exc,
                operation="submit",
                category="IDEMPOTENCY_CONFLICT",
                request_id=exc.request_id,
            )
        except (ValueError, FileNotFoundError, InvalidAdmissionRequest) as exc:
            self._send_async_error(
                HTTPStatus.BAD_REQUEST,
                exc,
                operation="submit",
                category="INVALID_ADMISSION_REQUEST",
                request_id=str(request.get("request_id") or "") or None,
            )
        except Exception as exc:
            self._send_async_error(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                exc,
                operation="submit",
                category="ADMISSION_INTERNAL_ERROR",
                request_id=str(request.get("request_id") or "") or None,
            )

    def _handle_async_trial_batch_get(self, batch_id: str) -> None:
        started = time.monotonic()
        ASYNC_METRICS.record("status_requests")
        try:
            snapshot = reconcile_async_batch_status(batch_id)
            delivered = self._send_async_json(
                HTTPStatus.OK,
                snapshot,
                operation="status",
                batch_id=batch_id,
            )
            if delivered:
                _record_async_event(
                    "async_status_response_delivered",
                    batch_id=batch_id,
                    state=snapshot["state"],
                    revision=snapshot["revision"],
                    queued_trials=snapshot["queued_trials"],
                    running_trials=snapshot["running_trials"],
                    succeeded_trials=snapshot["succeeded_trials"],
                    failed_trials=snapshot["failed_trials"],
                    status_latency_ms=round((time.monotonic() - started) * 1000, 3),
                )
        except ValueError as exc:
            self._send_async_error(
                HTTPStatus.BAD_REQUEST,
                exc,
                operation="status",
                category="MALFORMED_BATCH_ID",
                batch_id=batch_id,
            )
        except BatchNotFound as exc:
            self._send_async_error(
                HTTPStatus.NOT_FOUND,
                exc,
                operation="status",
                category="BATCH_NOT_FOUND",
                batch_id=batch_id,
            )
        except Exception as exc:
            self._send_async_error(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                exc,
                operation="status",
                category="STATUS_INTERNAL_ERROR",
                batch_id=batch_id,
            )

    def _handle_async_trial_batches_get(self, query: dict[str, list[str]]) -> None:
        started = time.monotonic()
        ASYNC_METRICS.record("status_requests")
        try:
            batch_ids = _parse_async_batch_ids(query)
            response = get_async_batch_snapshots(batch_ids)
            delivered = self._send_async_json(
                HTTPStatus.OK,
                response,
                operation="bulk_status",
            )
            if delivered:
                _record_async_event(
                    "async_bulk_status_response_delivered",
                    requested_batch_ids=len(batch_ids),
                    returned_batches=len(response["batches"]),
                    missing_batch_ids=len(response["missing_ids"]),
                    status_latency_ms=round((time.monotonic() - started) * 1000, 3),
                )
        except ValueError as exc:
            self._send_async_error(
                HTTPStatus.BAD_REQUEST,
                exc,
                operation="bulk_status",
                category="INVALID_BULK_STATUS_REQUEST",
            )
        except Exception as exc:
            self._send_async_error(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                exc,
                operation="bulk_status",
                category="STATUS_INTERNAL_ERROR",
            )

    def _handle_async_trial_batch_results_get(self, batch_id: str) -> None:
        started = time.monotonic()
        ASYNC_METRICS.record("result_requests")
        try:
            results = get_async_batch_results(batch_id)
            delivered = self._send_async_json(
                HTTPStatus.OK,
                results,
                operation="results",
                batch_id=batch_id,
            )
            if delivered:
                ASYNC_METRICS.record("result_responses_delivered")
                _record_async_event(
                    "async_results_response_delivered",
                    batch_id=batch_id,
                    state=results["state"],
                    terminal_trials=results["terminal_trials"],
                    available_results=results["available_results"],
                    unavailable_results=results["unavailable_results"],
                    result_delivery_latency_ms=round(
                        (time.monotonic() - started) * 1000,
                        3,
                    ),
                )
        except ValueError as exc:
            self._send_async_error(
                HTTPStatus.BAD_REQUEST,
                exc,
                operation="results",
                category="MALFORMED_BATCH_ID",
                batch_id=batch_id,
            )
        except BatchNotFound as exc:
            self._send_async_error(
                HTTPStatus.NOT_FOUND,
                exc,
                operation="results",
                category="BATCH_NOT_FOUND",
                batch_id=batch_id,
            )
        except Exception as exc:
            self._send_async_error(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                exc,
                operation="results",
                category="RESULT_DELIVERY_INTERNAL_ERROR",
                batch_id=batch_id,
            )

    def _send_run_trial_error(
        self,
        status: HTTPStatus,
        exc: Exception,
        *,
        started: float,
        request: dict[str, Any],
        request_id: str,
    ) -> None:
        detail = {
            "exception_type": type(exc).__name__,
            "exception_message": str(exc),
        }
        _append_trace({
            "event": "error",
            "timestamp": _now(),
            "request_id": request_id,
            "task_id": request.get("task_id") or request.get("task_path") or "<unknown>",
            "duration_sec": round(time.monotonic() - started, 3),
            "exception_info": detail,
        })
        self._send_json(status, {"detail": detail})

    def _handle_run_trial(self) -> None:
        started = time.monotonic()
        request: dict[str, Any] = {}
        request_id = ""
        try:
            request = self._read_json()
            request_id, result_path = _enqueue_request(request)
            wait_timeout = float(
                request.get("request_timeout")
                or request.get("timeout")
                or DEFAULT_TIMEOUT
            )
            result = _wait_result(result_path, wait_timeout)
            _append_trace({
                "event": "returned",
                "timestamp": _now(),
                "request_id": request_id,
                "task_id": result.get("task_id"),
                "status": "completed" if result.get("ok") else "failed",
                "duration_sec": round(time.monotonic() - started, 3),
            })
            self._send_json(HTTPStatus.OK, result)
        except ValueError as exc:
            self._send_run_trial_error(
                HTTPStatus.BAD_REQUEST,
                exc,
                started=started,
                request=request,
                request_id=request_id,
            )
        except Exception as exc:
            self._send_run_trial_error(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                exc,
                started=started,
                request=request,
                request_id=request_id,
            )

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        try:
            if parsed.path == "/async_trial_batches":
                if ENABLE_ASYNC_TRIAL_BATCHES:
                    self._handle_async_trial_batches_get(query)
                else:
                    self._send_json(HTTPStatus.NOT_FOUND, {"detail": "not found"})
                return
            async_batch_prefix = "/async_trial_batches/"
            if parsed.path.startswith(async_batch_prefix):
                suffix = parsed.path[len(async_batch_prefix):]
                parts = suffix.split("/")
                if not ENABLE_ASYNC_TRIAL_BATCHES:
                    self._send_json(HTTPStatus.NOT_FOUND, {"detail": "not found"})
                # GET /async_trial_batches/{batch_id}
                elif len(parts) == 1 and parts[0]:
                    self._handle_async_trial_batch_get(parts[0])
                # GET /async_trial_batches/{batch_id}/results
                elif len(parts) == 2 and parts[0] and parts[1] == "results":
                    self._handle_async_trial_batch_results_get(parts[0])
                else:
                    self._send_json(HTTPStatus.NOT_FOUND, {"detail": "not found"})
                return
            if parsed.path == "/health":
                async_health = _async_health_snapshot()
                self._send_json(HTTPStatus.OK, {
                    "status": "ok" if not ENABLE_ASYNC_TRIAL_BATCHES or async_health["ready"] else "degraded",
                    "mode": "rollout",
                    "dataset_roots": {name: str(path) for name, path in _dataset_roots().items()},
                    "disabled_task_ids": sorted(_disabled_task_ids()),
                    "default_dataset": DEFAULT_DATASET_NAME,
                    "default_agent": os.environ.get("RL_AGENT", "claude-code"),
                    "default_model_name": DEFAULT_MODEL_NAME,
                    "default_api_base_set": bool(DEFAULT_API_BASE),
                    "api_key_mode": DEFAULT_API_KEY_MODE,
                    "queue_dir": str(QUEUE_DIR),
                    "job_queue_root": str(JOB_QUEUE_ROOT),
                    "dynamic_job_zellij": ENABLE_DYNAMIC_JOB_ZELLIJ,
                    "async_trial_batches_enabled": ENABLE_ASYNC_TRIAL_BATCHES,
                    "async_trial_registry_path": str(ASYNC_TRIAL_REGISTRY_PATH),
                    "async_max_trials_per_batch": ASYNC_MAX_TRIALS_PER_BATCH,
                    "async_max_request_bytes": ASYNC_MAX_REQUEST_BYTES,
                    "async_max_bulk_status_ids": ASYNC_MAX_BULK_STATUS_IDS,
                    "async_control_plane": async_health,
                    "trace_log": str(TRACE_LOG),
                })
                return
            if parsed.path == "/datasets":
                datasets = []
                for name, root in sorted(_dataset_roots().items()):
                    tasks = list_dataset_tasks(name)
                    datasets.append({"name": name, "root": str(root), "task_count": len(tasks), "disabled_task_ids": sorted(_disabled_task_ids())})
                self._send_json(HTTPStatus.OK, {"datasets": datasets})
                return
            prefix = "/datasets/"
            suffix = "/tasks"
            if parsed.path.startswith(prefix) and parsed.path.endswith(suffix):
                dataset_name = parsed.path[len(prefix):-len(suffix)].strip("/")
                dataset_root = (query.get("dataset_root") or [None])[0]
                include_disabled = (query.get("include_disabled") or ["false"])[0].lower() in {"1", "true", "yes"}
                tasks = list_dataset_tasks(dataset_name, dataset_root, include_disabled=include_disabled)
                self._send_json(HTTPStatus.OK, {"dataset_name": dataset_name, "task_count": len(tasks), "task_ids": tasks, "disabled_task_ids": sorted(_disabled_task_ids())})
                return
            self._send_json(HTTPStatus.NOT_FOUND, {"detail": "not found"})
        except Exception as exc:
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"detail": {"exception_type": type(exc).__name__, "exception_message": str(exc)}})

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/async_trial_batches":
            if ENABLE_ASYNC_TRIAL_BATCHES:
                self._handle_async_trial_batch_submit()
            else:
                self._send_json(HTTPStatus.NOT_FOUND, {"detail": "not found"})
            return
        if path == "/run_trial":
            self._handle_run_trial()
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"detail": "not found"})


def main() -> int:
    host = os.environ.get("RL_HOST", "0.0.0.0")
    port = int(os.environ.get("RL_PORT", "19001"))
    for path in (TRACE_LOG.parent, PENDING_DIR, ACTIVE_DIR, RESULTS_DIR):
        path.mkdir(parents=True, exist_ok=True)
    if ENABLE_ASYNC_TRIAL_BATCHES:
        recovery = reconcile_async_state_on_startup()
        print(
            "RL async startup recovery "
            f"batches={recovery['batches_scanned']} "
            f"materialized={recovery['intents_materialized']} "
            f"completed={recovery['completed_batches']}",
            flush=True,
        )
    print(f"RL rollout Harbor service listening on {host}:{port}", flush=True)
    ThreadingHTTPServer((host, port), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
