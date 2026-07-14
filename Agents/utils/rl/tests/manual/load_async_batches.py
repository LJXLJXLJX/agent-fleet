#!/usr/bin/env python3
"""Drive the complete async batch lifecycle through the public HTTP contract."""

from __future__ import annotations

import argparse
import json
import math
import socket
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

import run_case as harness


RETRYABLE_STATUSES = {502, 503, 504}


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", type=Path, required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    return parser.parse_args()


def _drop_response(method: str, url: str, payload: dict[str, Any] | None = None) -> None:
    parsed = urlparse(url)
    body = b"" if payload is None else json.dumps(payload, separators=(",", ":")).encode()
    target = parsed.path or "/"
    if parsed.query:
        target = f"{target}?{parsed.query}"
    headers = [
        f"{method} {target} HTTP/1.1",
        f"Host: {parsed.hostname}:{parsed.port}",
        "Connection: close",
    ]
    if body:
        headers.extend(
            [
                "Content-Type: application/json",
                f"Content-Length: {len(body)}",
            ]
        )
    request = ("\r\n".join(headers) + "\r\n\r\n").encode("ascii") + body
    with socket.create_connection((str(parsed.hostname), int(parsed.port)), timeout=5.0) as conn:
        conn.sendall(request)


def _admission_exists(database: Path, request_id: str) -> bool:
    if not database.exists():
        return False
    try:
        with sqlite3.connect(database, timeout=0.1) as connection:
            return (
                connection.execute(
                    "SELECT 1 FROM idempotency_records WHERE request_id = ?",
                    (request_id,),
                ).fetchone()
                is not None
            )
    except sqlite3.Error:
        return False


def _wait_until(predicate: Any, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def _request_with_retry(
    method: str,
    url: str,
    payload: dict[str, Any] | None,
    *,
    accepted_status: int,
    request_timeout: float,
    recovery_timeout: float,
) -> tuple[dict[str, Any], list[dict[str, Any]], float | None]:
    deadline = time.monotonic() + recovery_timeout
    attempts: list[dict[str, Any]] = []
    first_failure_at: float | None = None
    while True:
        result = harness._request_json(method, url, payload, request_timeout)
        attempts.append(result)
        if result["status"] == accepted_status:
            recovery_seconds = (
                None
                if first_failure_at is None
                else time.monotonic() - first_failure_at
            )
            return result, attempts, recovery_seconds
        retryable = result["status"] is None or result["status"] in RETRYABLE_STATUSES
        if not retryable or time.monotonic() >= deadline:
            return result, attempts, None
        if first_failure_at is None:
            first_failure_at = time.monotonic()
        time.sleep(0.05)


def _submit_one(
    batch_index: int,
    payload: dict[str, Any],
    case: dict[str, Any],
    base_url: str,
    registry_path: Path,
) -> dict[str, Any]:
    endpoint = f"{base_url}/async_trial_batches"
    recovery_timeout = float(case.get("recovery_timeout_seconds", 30.0))
    request_timeout = float(case.get("request_timeout_seconds", 5.0))
    drop_indexes = {int(index) for index in case.get("drop_submit_response_batches", [])}
    dropped = batch_index in drop_indexes
    admission_confirmed_after_drop = False
    drop_started: float | None = None
    if dropped:
        drop_started = time.monotonic()
        _drop_response("POST", endpoint, payload)
        admission_confirmed_after_drop = _wait_until(
            lambda: _admission_exists(registry_path, str(payload["request_id"])),
            recovery_timeout,
        )

    duplicate_clients = int(case.get("duplicate_submit_clients", 1))
    if duplicate_clients <= 0:
        raise ValueError("duplicate_submit_clients must be positive")

    def submit() -> tuple[dict[str, Any], list[dict[str, Any]], float | None]:
        return _request_with_retry(
            "POST",
            endpoint,
            payload,
            accepted_status=202,
            request_timeout=request_timeout,
            recovery_timeout=recovery_timeout,
        )

    if duplicate_clients == 1:
        submissions = [submit()]
    else:
        with ThreadPoolExecutor(max_workers=duplicate_clients) as executor:
            submissions = list(executor.map(lambda _: submit(), range(duplicate_clients)))

    responses = [item[0] for item in submissions]
    accepted = [item for item in responses if item["status"] == 202]
    batch_ids = {
        str(item["body"].get("batch_id"))
        for item in accepted
        if isinstance(item.get("body"), dict) and item["body"].get("batch_id")
    }
    recovered_after_drop_seconds = (
        None
        if drop_started is None or not batch_ids
        else time.monotonic() - drop_started
    )
    return {
        "batch_index": batch_index,
        "request_id": payload["request_id"],
        "dropped_submit_response": dropped,
        "admission_confirmed_after_drop": admission_confirmed_after_drop,
        "recovered_after_drop_seconds": recovered_after_drop_seconds,
        "client_submissions": duplicate_clients + int(dropped),
        "accepted_responses": len(accepted),
        "transport_errors": sum(
            attempt["status"] is None
            for _, attempts, _ in submissions
            for attempt in attempts
        ),
        "batch_ids": sorted(batch_ids),
        "responses": responses,
    }


def _chunks(values: list[str], size: int) -> list[list[str]]:
    return [values[index : index + size] for index in range(0, len(values), size)]


def run(case: dict[str, Any], base_url: str, work_dir: Path, manifest: Path) -> dict[str, Any]:
    trials = int(case["trials"])
    batch_size = int(case["async_batch_size"])
    batches = math.ceil(trials / batch_size)
    run_token = f"lifecycle-{uuid4().hex[:10]}"
    rejection_probe: dict[str, Any] | None = None
    rejection_probe_trials = int(case.get("admission_rejection_probe_trials", 0))
    if rejection_probe_trials < 0:
        raise ValueError("admission_rejection_probe_trials must be non-negative")
    if rejection_probe_trials:
        probe_payload = harness._async_payload(
            batches,
            trials,
            rejection_probe_trials,
            f"{run_token}-rejection-probe",
            work_dir,
        )
        probe_result = harness._request_json(
            "POST",
            f"{base_url}/async_trial_batches",
            probe_payload,
            float(case.get("request_timeout_seconds", 5.0)),
        )
        rejection_probe = {
            "trials": rejection_probe_trials,
            "status": probe_result["status"],
            "latency_seconds": probe_result["latency_seconds"],
            "body": probe_result["body"],
            "error": probe_result["error"],
        }
    payloads = []
    for batch_index in range(batches):
        first_trial = batch_index * batch_size
        trial_count = min(batch_size, trials - first_trial)
        payloads.append(
            harness._async_payload(
                batch_index,
                first_trial,
                trial_count,
                run_token,
                work_dir,
            )
        )

    submit_started = time.monotonic()
    registry_path = work_dir / "registry.sqlite3"
    submit_workers = min(batches, int(case.get("submit_concurrency", 16)))
    with ThreadPoolExecutor(max_workers=max(1, submit_workers)) as executor:
        admissions = list(
            executor.map(
                lambda item: _submit_one(item[0], item[1], case, base_url, registry_path),
                enumerate(payloads),
            )
        )
    submit_seconds = time.monotonic() - submit_started

    handles: list[dict[str, str]] = []
    for admission in admissions:
        if len(admission["batch_ids"]) == 1:
            handles.append(
                {
                    "request_id": str(admission["request_id"]),
                    "batch_id": str(admission["batch_ids"][0]),
                }
            )
    harness._atomic_write_json(
        manifest,
        {
            "schema_version": 1,
            "run_token": run_token,
            "logical_batches": batches,
            "handles": handles,
        },
    )

    batch_ids = [handle["batch_id"] for handle in handles]
    expected_terminal = int(case.get("expected_terminal_trials", trials))
    status_chunk_size = int(case.get("status_chunk_size", 128))
    poll_interval = float(case.get("poll_interval_seconds", 0.2))
    lifecycle_timeout = float(case.get("lifecycle_timeout_seconds", 120.0))
    request_timeout = float(case.get("request_timeout_seconds", 5.0))
    recovery_timeout = float(case.get("recovery_timeout_seconds", 30.0))
    drop_status_every = int(case.get("drop_status_response_every", 0))
    progress_drop_targets = sorted(
        float(value) for value in case.get("drop_status_at_progress", [])
    )
    if any(not 0.0 <= value <= 1.0 for value in progress_drop_targets):
        raise ValueError("drop_status_at_progress values must be between 0 and 1")
    progress_drop_index = 0
    status_requests = 0
    status_transport_errors = 0
    status_502_504 = 0
    status_recoveries: list[float] = []
    status_latencies: list[float] = []
    dropped_status_responses = 0
    recovered_status_responses = 0
    snapshots: dict[str, dict[str, Any]] = {}
    polling_started = time.monotonic()
    deadline = polling_started + lifecycle_timeout
    while batch_ids and time.monotonic() < deadline:
        terminal_before_poll = sum(
            int(snapshot.get("succeeded_trials", 0))
            + int(snapshot.get("failed_trials", 0))
            for snapshot in snapshots.values()
        )
        for chunk in _chunks(batch_ids, status_chunk_size):
            ids = ",".join(chunk)
            url = f"{base_url}/async_trial_batches?ids={ids}"
            drops_before_request = 0
            drop_started_at: list[float] = []
            if drop_status_every > 0 and (status_requests + 1) % drop_status_every == 0:
                drop_started_at.append(time.monotonic())
                _drop_response("GET", url)
                dropped_status_responses += 1
                drops_before_request += 1
            progress = (
                0.0
                if expected_terminal == 0
                else terminal_before_poll / expected_terminal
            )
            while (
                progress_drop_index < len(progress_drop_targets)
                and progress >= progress_drop_targets[progress_drop_index]
            ):
                drop_started_at.append(time.monotonic())
                _drop_response("GET", url)
                dropped_status_responses += 1
                drops_before_request += 1
                progress_drop_index += 1
            result, attempts, recovery = _request_with_retry(
                "GET",
                url,
                None,
                accepted_status=200,
                request_timeout=request_timeout,
                recovery_timeout=recovery_timeout,
            )
            status_requests += len(attempts)
            status_latencies.extend(float(item["latency_seconds"]) for item in attempts)
            status_transport_errors += sum(item["status"] is None for item in attempts)
            status_502_504 += sum(item["status"] in {502, 504} for item in attempts)
            if recovery is not None:
                status_recoveries.append(recovery)
            if result["status"] == 200 and isinstance(result["body"], dict):
                recovered_status_responses += drops_before_request
                status_recoveries.extend(
                    time.monotonic() - started_at for started_at in drop_started_at
                )
                for snapshot in result["body"].get("batches", []):
                    if isinstance(snapshot, dict) and snapshot.get("batch_id"):
                        snapshots[str(snapshot["batch_id"])] = snapshot
        terminal = sum(
            int(snapshot.get("succeeded_trials", 0))
            + int(snapshot.get("failed_trials", 0))
            for snapshot in snapshots.values()
        )
        if terminal >= expected_terminal:
            break
        time.sleep(poll_interval)
    polling_seconds = time.monotonic() - polling_started

    result_fetch_delay = float(case.get("result_fetch_delay_seconds", 0.0))
    if result_fetch_delay < 0:
        raise ValueError("result_fetch_delay_seconds must be non-negative")
    if result_fetch_delay:
        time.sleep(result_fetch_delay)

    drop_results_every = int(case.get("drop_results_response_every", 0))
    result_requests = 0
    result_transport_errors = 0
    result_502_504 = 0
    result_recoveries: list[float] = []
    result_latencies: list[float] = []
    dropped_result_responses = 0
    recovered_result_responses = 0
    manifests: list[dict[str, Any]] = []
    for index, batch_id in enumerate(batch_ids, start=1):
        url = f"{base_url}/async_trial_batches/{batch_id}/results"
        result_drop_started_at: float | None = None
        if drop_results_every > 0 and index % drop_results_every == 0:
            result_drop_started_at = time.monotonic()
            _drop_response("GET", url)
            dropped_result_responses += 1
        result, attempts, recovery = _request_with_retry(
            "GET",
            url,
            None,
            accepted_status=200,
            request_timeout=request_timeout,
            recovery_timeout=recovery_timeout,
        )
        result_requests += len(attempts)
        result_latencies.extend(float(item["latency_seconds"]) for item in attempts)
        result_transport_errors += sum(item["status"] is None for item in attempts)
        result_502_504 += sum(item["status"] in {502, 504} for item in attempts)
        if recovery is not None:
            result_recoveries.append(recovery)
        if result["status"] == 200 and isinstance(result["body"], dict):
            if drop_results_every > 0 and index % drop_results_every == 0:
                recovered_result_responses += 1
                if result_drop_started_at is not None:
                    result_recoveries.append(time.monotonic() - result_drop_started_at)
            manifests.append(result["body"])

    delivered_results = [
        result
        for result_manifest in manifests
        for result in result_manifest.get("results", [])
        if isinstance(result, dict)
    ]
    trial_execution_ids = [
        str(result.get("trial_execution_id"))
        for result in delivered_results
        if result.get("trial_execution_id")
    ]
    client_trial_ids = [
        str(result.get("client_trial_id"))
        for result in delivered_results
        if result.get("client_trial_id")
    ]
    available_results = sum(result.get("result") is not None for result in delivered_results)
    terminal_trials = sum(
        int(snapshot.get("succeeded_trials", 0)) + int(snapshot.get("failed_trials", 0))
        for snapshot in snapshots.values()
    )
    dropped_admissions = [item for item in admissions if item["dropped_submit_response"]]
    recovered_dropped_admissions = sum(
        len(item["batch_ids"]) == 1 for item in dropped_admissions
    )
    all_recoveries = [
        value
        for value in (
            [item["recovered_after_drop_seconds"] for item in dropped_admissions]
            + status_recoveries
            + result_recoveries
        )
        if value is not None
    ]
    injected_response_losses = (
        len(dropped_admissions)
        + dropped_status_responses
        + dropped_result_responses
    )
    recovered_response_losses = (
        recovered_dropped_admissions
        + recovered_status_responses
        + recovered_result_responses
    )
    unknown_outcome_count = injected_response_losses - recovered_response_losses
    return {
        "schema_version": 1,
        "case_name": case["name"],
        "run_token": run_token,
        "trials": trials,
        "logical_batches": batches,
        "admission_rejection_probe": rejection_probe,
        "recovered_batch_handles": len(handles),
        "unique_batch_handles": len(set(batch_ids)),
        "submit_seconds": submit_seconds,
        "admissions": admissions,
        "dropped_submit_responses": len(dropped_admissions),
        "recovered_dropped_submit_responses": recovered_dropped_admissions,
        "injected_response_losses": injected_response_losses,
        "recovered_response_losses": recovered_response_losses,
        "unknown_outcome_count": unknown_outcome_count,
        "unknown_outcome_rate": (
            0.0
            if not injected_response_losses
            else unknown_outcome_count / injected_response_losses
        ),
        "terminal_trials": terminal_trials,
        "expected_terminal_trials": expected_terminal,
        "delivered_results": len(delivered_results),
        "available_results": available_results,
        "lost_result_count": max(0, expected_terminal - available_results),
        "lost_result_rate": (
            0.0
            if expected_terminal == 0
            else max(0, expected_terminal - available_results) / expected_terminal
        ),
        "duplicate_trial_execution_ids": len(trial_execution_ids)
        - len(set(trial_execution_ids)),
        "duplicate_client_trial_ids": len(client_trial_ids) - len(set(client_trial_ids)),
        "status": {
            "response_observed_requests": status_requests,
            "wire_requests": status_requests + dropped_status_responses,
            "transport_errors": status_transport_errors,
            "http_502_504": status_502_504,
            "dropped_responses": dropped_status_responses,
            "recovered_dropped_responses": recovered_status_responses,
            "progress_drop_targets": progress_drop_targets,
            "progress_drops_completed": progress_drop_index,
            "polling_seconds": polling_seconds,
            "qps": (
                0.0
                if polling_seconds == 0
                else (status_requests + dropped_status_responses) / polling_seconds
            ),
            "latency_seconds": {
                "p50": harness._percentile(status_latencies, 0.50),
                "p95": harness._percentile(status_latencies, 0.95),
                "p99": harness._percentile(status_latencies, 0.99),
                "max": max(status_latencies) if status_latencies else None,
            },
            "requests_per_logical_batch": (
                0.0
                if batches == 0
                else (status_requests + dropped_status_responses) / batches
            ),
            "requests_per_trial": (
                0.0
                if trials == 0
                else (status_requests + dropped_status_responses) / trials
            ),
        },
        "results": {
            "requests": result_requests,
            "wire_requests": result_requests + dropped_result_responses,
            "transport_errors": result_transport_errors,
            "http_502_504": result_502_504,
            "dropped_responses": dropped_result_responses,
            "recovered_dropped_responses": recovered_result_responses,
            "latency_seconds": {
                "p50": harness._percentile(result_latencies, 0.50),
                "p95": harness._percentile(result_latencies, 0.95),
                "p99": harness._percentile(result_latencies, 0.99),
                "max": max(result_latencies) if result_latencies else None,
            },
        },
        "recovery_seconds": {
            "count": len(all_recoveries),
            "p50": harness._percentile(all_recoveries, 0.50),
            "p95": harness._percentile(all_recoveries, 0.95),
            "p99": harness._percentile(all_recoveries, 0.99),
            "max": max(all_recoveries) if all_recoveries else None,
        },
        "final_snapshots": snapshots,
    }


def main() -> int:
    args = _parse_args()
    case = harness._load_case(args.case.resolve())
    summary = run(case, args.base_url.rstrip("/"), args.work_dir.resolve(), args.manifest)
    harness._atomic_write_json(args.output, summary)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"async load failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
