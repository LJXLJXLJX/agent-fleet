#!/usr/bin/env python3
"""Summarize one async batch from the production control-plane JSONL trace."""

from __future__ import annotations

import argparse
import json
import math
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any


TERMINAL_STATES = {"SUCCEEDED", "FAILED"}


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trace-log", type=Path, required=True)
    parser.add_argument("--batch-id", required=True)
    parser.add_argument("--expected-trials", type=int)
    return parser.parse_args()


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, math.ceil(percentile * len(ordered)) - 1)
    return round(ordered[index], 3)


def _latency_summary(values: list[float]) -> dict[str, Any]:
    return {
        "count": len(values),
        "p50": _percentile(values, 0.50),
        "p95": _percentile(values, 0.95),
        "max": round(max(values), 3) if values else None,
    }


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def summarize(trace_log: Path, batch_id: str) -> dict[str, Any]:
    events = [
        json.loads(line)
        for line in trace_log.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    batch_events = [event for event in events if event.get("batch_id") == batch_id]
    event_counts = Counter(str(event.get("event")) for event in batch_events)
    queue_latencies = [
        float(event["queue_latency_ms"])
        for event in batch_events
        if event.get("event") == "async_trial_queued"
        and isinstance(event.get("queue_latency_ms"), (int, float))
    ]
    worker_run_latencies = [
        float(event["duration_ms"])
        for event in batch_events
        if event.get("event") == "async_trial_result_committed"
        and isinstance(event.get("duration_ms"), (int, float))
    ]

    running_observed_at: dict[str, datetime] = {}
    observed_run_latencies: list[float] = []
    transition_counts: Counter[str] = Counter()
    for event in batch_events:
        if event.get("event") != "async_trial_state_observed":
            continue
        trial_execution_id = str(event.get("trial_execution_id") or "")
        target = str(event.get("to_state") or "")
        transition_counts[target] += 1
        observed_at = _timestamp(event.get("timestamp"))
        if target == "RUNNING" and observed_at is not None:
            running_observed_at[trial_execution_id] = observed_at
        elif target in TERMINAL_STATES and observed_at is not None:
            started_at = running_observed_at.get(trial_execution_id)
            if started_at is not None:
                observed_run_latencies.append(
                    round((observed_at - started_at).total_seconds() * 1000, 3)
                )

    delivered = [
        event
        for event in batch_events
        if event.get("event") == "async_submit_response_delivered"
    ]
    submit_latency = delivered[-1].get("response_latency_ms") if delivered else None
    identities = {
        (
            event.get("trial_execution_id"),
            event.get("client_trial_id"),
        )
        for event in batch_events
        if event.get("trial_execution_id") and event.get("client_trial_id")
    }
    run_latencies = worker_run_latencies or observed_run_latencies
    return {
        "batch_id": batch_id,
        "events": len(batch_events),
        "event_counts": dict(sorted(event_counts.items())),
        "correlated_trials": len(identities),
        "submit_response_latency_ms": submit_latency,
        "queue_latency_ms": _latency_summary(queue_latencies),
        "run_latency_ms": {
            "source": "worker_committed" if worker_run_latencies else "control_plane_observed",
            **_latency_summary(run_latencies),
        },
        "state_transition_counts": dict(sorted(transition_counts.items())),
    }


def main() -> int:
    args = _parse_args()
    summary = summarize(args.trace_log, args.batch_id)
    print(json.dumps(summary, indent=2, sort_keys=True))
    if args.expected_trials is not None:
        if summary["correlated_trials"] != args.expected_trials:
            return 1
        if summary["queue_latency_ms"]["count"] != args.expected_trials:
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
