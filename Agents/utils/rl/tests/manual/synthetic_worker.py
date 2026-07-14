#!/usr/bin/env python3
"""Deterministic worker for the existing pending/active/results file queue."""

from __future__ import annotations

import argparse
import heapq
import json
import os
import signal
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4


STOP = False


def _request_stop(signum: int, frame: object) -> None:
    del signum, frame
    global STOP
    STOP = True


@dataclass(order=True)
class ActiveExecution:
    deadline: float
    sequence: int
    claim_id: str = field(compare=False)
    active_path: Path = field(compare=False)
    payload: dict[str, Any] = field(compare=False)
    claimed_at: float = field(compare=False)
    outcome: str = field(compare=False)
    duplicate_result: bool = field(compare=False)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--job-queue-root", type=Path, required=True)
    parser.add_argument("--delay-seconds", type=float)
    parser.add_argument("--delay-profile-json")
    parser.add_argument("--capacity", type=int, required=True)
    parser.add_argument("--audit-log", type=Path, required=True)
    parser.add_argument("--poll-seconds", type=float, default=0.01)
    parser.add_argument("--failure-fraction", type=float, default=0.0)
    parser.add_argument("--hang-fraction", type=float, default=0.0)
    parser.add_argument("--duplicate-result-fraction", type=float, default=0.0)
    return parser.parse_args()


def _delay_profile(args: argparse.Namespace) -> list[tuple[float, float]]:
    if args.delay_profile_json:
        raw_profile = json.loads(args.delay_profile_json)
        if not isinstance(raw_profile, list) or not raw_profile:
            raise ValueError("--delay-profile-json must be a non-empty array")
        profile: list[tuple[float, float]] = []
        cumulative = 0.0
        for item in raw_profile:
            if not isinstance(item, dict):
                raise ValueError("delay profile entries must be objects")
            fraction = float(item["fraction"])
            seconds = float(item["seconds"])
            if fraction <= 0 or seconds < 0:
                raise ValueError(
                    "delay profile fractions must be positive and seconds non-negative"
                )
            cumulative += fraction
            profile.append((cumulative, seconds))
        if abs(cumulative - 1.0) > 1e-6:
            raise ValueError(f"delay profile fractions must sum to 1.0, got {cumulative}")
        return profile
    if args.delay_seconds is None or args.delay_seconds < 0:
        raise ValueError("provide non-negative --delay-seconds or --delay-profile-json")
    return [(1.0, args.delay_seconds)]


def _delay_for_sequence(profile: list[tuple[float, float]], sequence: int) -> float:
    position = ((sequence * 2654435761) % 10000) / 10000.0
    for cumulative, seconds in profile:
        if position < cumulative:
            return seconds
    return profile[-1][1]


def _fraction_position(sequence: int, salt: int) -> float:
    return ((sequence * 2654435761 + salt) % 10000) / 10000.0


def _outcome_for_sequence(args: argparse.Namespace, sequence: int) -> tuple[str, bool]:
    position = _fraction_position(sequence, 7919)
    if position < args.hang_fraction:
        outcome = "hang"
    elif position < args.hang_fraction + args.failure_fraction:
        outcome = "failed"
    else:
        outcome = "succeeded"
    duplicate_result = (
        outcome != "hang"
        and _fraction_position(sequence, 104729) < args.duplicate_result_fraction
    )
    return outcome, duplicate_result


def _append_event(path: Path, event: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=True, sort_keys=True) + "\n")


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    temporary.replace(path)


def _claim_one(
    pending_path: Path,
    delay_seconds: float,
    sequence: int,
    outcome: str,
    duplicate_result: bool,
) -> ActiveExecution | None:
    try:
        payload = json.loads(pending_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    active_path = pending_path.parent.parent / "active" / pending_path.name
    active_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        pending_path.replace(active_path)
    except FileNotFoundError:
        return None
    claimed_at = time.monotonic()
    return ActiveExecution(
        deadline=float("inf") if outcome == "hang" else claimed_at + delay_seconds,
        sequence=sequence,
        claim_id=f"claim-{uuid4().hex}",
        active_path=active_path,
        payload=payload,
        claimed_at=claimed_at,
        outcome=outcome,
        duplicate_result=duplicate_result,
    )


def _finish(execution: ActiveExecution, audit_log: Path) -> None:
    payload = execution.payload
    request_id = str(payload.get("request_id") or execution.active_path.stem)
    result_path = execution.active_path.parent.parent / "results" / f"{request_id}.json"
    result = {
        "ok": execution.outcome == "succeeded",
        "request_id": request_id,
        "task_id": payload.get("task_id"),
        "reward": 1.0 if execution.outcome == "succeeded" else 0.0,
        "synthetic": True,
        "exception_info": (
            None
            if execution.outcome == "succeeded"
            else {"exception_type": "SyntheticWorkerFailure"}
        ),
        "metadata": {
            "claim_id": execution.claim_id,
            "request_id": request_id,
            "batch_id": payload.get("batch_id"),
            "trial_execution_id": payload.get("trial_execution_id"),
            "client_trial_id": payload.get("client_trial_id"),
            "async_request_id": payload.get("async_request_id"),
        },
    }
    _atomic_write_json(result_path, result)
    if execution.duplicate_result:
        _atomic_write_json(result_path, result)
        _append_event(
            audit_log,
            {
                "event": "duplicate_result_write",
                "timestamp": time.time(),
                "request_id": request_id,
                "batch_id": payload.get("batch_id"),
                "trial_execution_id": payload.get("trial_execution_id"),
            },
        )
    execution.active_path.unlink(missing_ok=True)
    finished_at = time.monotonic()
    _append_event(
        audit_log,
        {
            "event": "finish",
            "timestamp": time.time(),
            "monotonic": finished_at,
            "claim_id": execution.claim_id,
            "request_id": request_id,
            "batch_id": payload.get("batch_id"),
            "trial_execution_id": payload.get("trial_execution_id"),
            "client_trial_id": payload.get("client_trial_id"),
            "duration_seconds": finished_at - execution.claimed_at,
            "outcome": execution.outcome,
            "duplicate_result": execution.duplicate_result,
        },
    )


def main() -> int:
    args = _parse_args()
    delay_profile = _delay_profile(args)
    if args.capacity <= 0:
        raise ValueError("--capacity must be positive")
    for name in ("failure_fraction", "hang_fraction", "duplicate_result_fraction"):
        value = float(getattr(args, name))
        if not 0.0 <= value <= 1.0:
            raise ValueError(f"--{name.replace('_', '-')} must be between 0 and 1")
    if args.failure_fraction + args.hang_fraction > 1.0:
        raise ValueError("failure and hang fractions must sum to at most 1")
    args.audit_log.parent.mkdir(parents=True, exist_ok=True)
    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)

    active: list[ActiveExecution] = []
    sequence = 0
    while not STOP:
        available = args.capacity - len(active)
        if available > 0:
            pending_files = sorted(args.job_queue_root.glob("*/pending/*.json"))
            for pending_path in pending_files[:available]:
                delay_seconds = _delay_for_sequence(delay_profile, sequence)
                outcome, duplicate_result = _outcome_for_sequence(args, sequence)
                execution = _claim_one(
                    pending_path,
                    delay_seconds,
                    sequence,
                    outcome,
                    duplicate_result,
                )
                if execution is None:
                    continue
                sequence += 1
                heapq.heappush(active, execution)
                payload = execution.payload
                _append_event(
                    args.audit_log,
                    {
                        "event": "claim",
                        "timestamp": time.time(),
                        "monotonic": execution.claimed_at,
                        "claim_id": execution.claim_id,
                        "request_id": payload.get("request_id"),
                        "batch_id": payload.get("batch_id"),
                        "trial_execution_id": payload.get("trial_execution_id"),
                        "client_trial_id": payload.get("client_trial_id"),
                        "outcome": execution.outcome,
                    },
                )

        now = time.monotonic()
        while active and active[0].deadline <= now:
            _finish(heapq.heappop(active), args.audit_log)
        time.sleep(args.poll_seconds)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
