#!/usr/bin/env python3
"""Submit one synthetic async trial batch and verify its queue artifacts."""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from uuid import uuid4


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--harbor-url", required=True)
    parser.add_argument("--dataset-name", required=True)
    parser.add_argument("--ray-submission-id", required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--trial-count", type=int, default=32)
    parser.add_argument("--dataset-root")
    parser.add_argument("--queue-root", type=Path)
    parser.add_argument("--queue-wait-seconds", type=float, default=10.0)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--request-id")
    return parser.parse_args()


def _safe_slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip("-") or "default"


def _request_payload(args: argparse.Namespace) -> dict[str, Any]:
    request_id = args.request_id or f"smoke-{uuid4().hex}"
    batching_key: dict[str, Any] = {
        "dataset_name": args.dataset_name,
        "ray_submission_id": args.ray_submission_id,
        "policy_version": "synthetic-smoke",
    }
    if args.dataset_root:
        batching_key["dataset_root"] = args.dataset_root
    return {
        "request_id": request_id,
        "client_batch_id": f"client-{request_id}",
        "trainer_run_id": "agent-fleet-async-smoke",
        "batching_key": batching_key,
        "trials": [
            {
                "client_trial_id": f"{request_id}-trial-{index:03d}",
                "session_id": f"{request_id}-session-{index:03d}",
                "task_id": args.task_id,
                "group_id": index,
                "rollout_step": 0,
                "policy_version": "synthetic-smoke",
                "payload": {},
            }
            for index in range(args.trial_count)
        ],
    }


def _submit(url: str, payload: dict[str, Any], timeout: float) -> tuple[int, dict[str, Any]]:
    request = urllib.request.Request(
        f"{url.rstrip('/')}/async_trial_batches",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
            return response.status, body
    except urllib.error.HTTPError as exc:
        raw_body = exc.read().decode("utf-8", errors="replace")
        try:
            body = json.loads(raw_body)
        except json.JSONDecodeError:
            body = {"raw_body": raw_body}
        return exc.code, body


def _get_snapshot(url: str, batch_id: str, timeout: float) -> tuple[int, dict[str, Any]]:
    request = urllib.request.Request(
        f"{url.rstrip('/')}/async_trial_batches/{batch_id}",
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
            return response.status, body
    except urllib.error.HTTPError as exc:
        raw_body = exc.read().decode("utf-8", errors="replace")
        try:
            body = json.loads(raw_body)
        except json.JSONDecodeError:
            body = {"raw_body": raw_body}
        return exc.code, body


def _wait_for_queue_artifacts(
    queue_root: Path,
    ray_submission_id: str,
    trial_execution_ids: set[str],
    timeout: float,
) -> dict[str, str]:
    job_dir = queue_root / _safe_slug(ray_submission_id)
    deadline = time.monotonic() + timeout
    found: dict[str, str] = {}
    while time.monotonic() < deadline:
        for trial_execution_id in trial_execution_ids - found.keys():
            for state in ("pending", "active", "results"):
                if (job_dir / state / f"{trial_execution_id}.json").exists():
                    found[trial_execution_id] = state
                    break
        if len(found) == len(trial_execution_ids):
            return found
        time.sleep(0.05)
    return found


def main() -> int:
    args = _parse_args()
    if args.trial_count <= 0:
        raise ValueError("--trial-count must be positive")
    payload = _request_payload(args)

    started = time.monotonic()
    status, response = _submit(args.harbor_url, payload, args.timeout)
    submit_ms = round((time.monotonic() - started) * 1000, 3)
    summary: dict[str, Any] = {
        "request_id": payload["request_id"],
        "http_status": status,
        "submit_ms": submit_ms,
        "requested_trials": args.trial_count,
        "batch_id": response.get("batch_id"),
    }
    if status != 202:
        summary["response"] = response
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 1

    trial_execution_ids = {
        str(trial["trial_execution_id"])
        for trial in response.get("trials", [])
        if isinstance(trial, dict) and trial.get("trial_execution_id")
    }
    summary["returned_trial_mappings"] = len(trial_execution_ids)
    if len(trial_execution_ids) != args.trial_count:
        summary["error"] = "response did not contain one trial mapping per requested trial"
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 1

    if args.queue_root is not None:
        found = _wait_for_queue_artifacts(
            args.queue_root,
            args.ray_submission_id,
            trial_execution_ids,
            args.queue_wait_seconds,
        )
        summary["queue_artifacts"] = len(found)
        summary["queue_states"] = {
            state: sum(found_state == state for found_state in found.values())
            for state in ("pending", "active", "results")
        }
        if len(found) != args.trial_count:
            summary["missing_queue_artifacts"] = sorted(trial_execution_ids - found.keys())
            print(json.dumps(summary, indent=2, sort_keys=True))
            return 1

    recovery_started = time.monotonic()
    snapshot_status, snapshot = _get_snapshot(
        args.harbor_url,
        str(response["batch_id"]),
        args.timeout,
    )
    summary["state_recovery_ms"] = round(
        (time.monotonic() - recovery_started) * 1000,
        3,
    )
    summary["snapshot_http_status"] = snapshot_status
    summary["snapshot_state"] = snapshot.get("state")
    summary["snapshot_requested_trials"] = snapshot.get("requested_trials")
    if snapshot_status != 200:
        summary["snapshot_response"] = snapshot
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 1

    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"smoke failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
