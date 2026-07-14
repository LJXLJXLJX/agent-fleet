#!/usr/bin/env python3
"""Compare sync and async duplicate/replayed-request behavior."""

from __future__ import annotations

import argparse
import json
import socket
import sqlite3
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

import run_case as benchmark


SCRIPT_DIR = Path(__file__).resolve().parent


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rounds", type=int, default=100)
    parser.add_argument("--duplicate-clients", type=int, default=8)
    parser.add_argument("--output-root", type=Path, default=SCRIPT_DIR / "results")
    return parser.parse_args()


def _drop_response(url: str, payload: dict[str, Any]) -> None:
    parsed = urlparse(url)
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    request = (
        f"POST {parsed.path} HTTP/1.1\r\n"
        f"Host: {parsed.hostname}:{parsed.port}\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Connection: close\r\n\r\n"
    ).encode("ascii") + body
    with socket.create_connection((str(parsed.hostname), int(parsed.port)), timeout=5.0) as connection:
        connection.sendall(request)


def _wait_until(predicate: Any, description: str, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise TimeoutError(f"timed out waiting for {description}")


def _admission_exists(database: Path, request_id: str) -> bool:
    if not database.exists():
        return False
    try:
        with sqlite3.connect(database, timeout=0.1) as connection:
            row = connection.execute(
                "SELECT 1 FROM idempotency_records WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            return row is not None
    except sqlite3.Error:
        return False


def _queue_artifact_exists(job_queue_root: Path, request_id: str) -> bool:
    return any(
        next(job_queue_root.glob(f"*/{state}/{request_id}.json"), None) is not None
        for state in ("pending", "active", "results")
    )


def _scenario_payload(
    mode: str,
    scenario: str,
    round_number: int,
    run_token: str,
    work_dir: Path,
) -> dict[str, Any]:
    if mode == "sync":
        return benchmark._sync_payload(round_number, f"{scenario}-{run_token}", work_dir, 30.0)
    return benchmark._async_payload(
        round_number,
        round_number,
        1,
        f"{scenario}-{run_token}",
        work_dir,
    )


def _run_scenario(
    mode: str,
    scenario: str,
    rounds: int,
    duplicate_clients: int,
    output_dir: Path,
) -> dict[str, Any]:
    scenario_dir = output_dir / f"{mode}-{scenario}"
    work_dir = scenario_dir / "work"
    scenario_dir.mkdir(parents=True)
    port = benchmark._free_port()
    base_url = f"http://127.0.0.1:{port}"
    endpoint = "/run_trial" if mode == "sync" else "/async_trial_batches"
    accepted_status = 200 if mode == "sync" else 202
    run_token = uuid4().hex[:8]
    server = None
    worker = None
    handles: list[Any] = []
    responses: list[dict[str, Any]] = []
    returned_batch_ids: list[str] = []
    try:
        server, server_handle = benchmark._start_process(
            [
                sys.executable,
                str(benchmark.SERVER_SCRIPT),
                "--port",
                str(port),
                "--work-dir",
                str(work_dir),
                "--max-trials-per-batch",
                "2048",
            ],
            scenario_dir / "server.log",
        )
        handles.append(server_handle)
        benchmark._wait_for_server(base_url, server)
        worker_case = {
            "execution_capacity": max(32, duplicate_clients),
            "delay_seconds": 0.05,
        }
        worker, worker_handle = benchmark._start_process(
            benchmark._worker_command(worker_case, work_dir, scenario_dir / "worker-audit.jsonl"),
            scenario_dir / "worker.log",
        )
        handles.append(worker_handle)

        for round_number in range(rounds):
            payload = _scenario_payload(mode, scenario, round_number, run_token, work_dir)
            if scenario == "concurrent-duplicate":
                operations = [
                    (
                        lambda payload=payload: benchmark._request_json(
                            "POST",
                            f"{base_url}{endpoint}",
                            payload,
                            30.0,
                        )
                    )
                    for _ in range(duplicate_clients)
                ]
                round_responses = benchmark._run_simultaneously(operations, 40.0)
                responses.extend(round_responses)
                returned_batch_ids.extend(
                    str(item["body"].get("batch_id"))
                    for item in round_responses
                    if item["status"] == 202 and isinstance(item["body"], dict)
                )
            elif scenario == "response-drop-retry":
                request_id = str(payload["request_id"])
                _drop_response(f"{base_url}{endpoint}", payload)
                if mode == "async":
                    _wait_until(
                        lambda: _admission_exists(work_dir / "registry.sqlite3", request_id),
                        f"durable async admission {request_id}",
                    )
                else:
                    _wait_until(
                        lambda: _queue_artifact_exists(
                            work_dir / "queue" / "jobs",
                            request_id,
                        ),
                        f"sync queue artifact {request_id}",
                    )
                retry = benchmark._request_json(
                    "POST",
                    f"{base_url}{endpoint}",
                    payload,
                    30.0,
                )
                responses.append(retry)
                if retry["status"] == 202 and isinstance(retry["body"], dict):
                    returned_batch_ids.append(str(retry["body"].get("batch_id")))
            else:
                raise ValueError(f"unknown scenario: {scenario}")

            benchmark._wait_for_results(work_dir / "queue" / "jobs", round_number + 1, 30.0)

        time.sleep(0.5)
        events = benchmark._load_jsonl(scenario_dir / "worker-audit.jsonl")
        claims = [event for event in events if event.get("event") == "claim"]
        claims_per_request = Counter(str(event.get("request_id")) for event in claims)
        duplicate_claims = sum(count - 1 for count in claims_per_request.values() if count > 1)
        requests_with_duplicate_claims = sum(
            count > 1 for count in claims_per_request.values()
        )
        job_queue_root = work_dir / "queue" / "jobs"
        summary = {
            "mode": mode,
            "scenario": scenario,
            "rounds": rounds,
            "duplicate_clients": duplicate_clients if scenario == "concurrent-duplicate" else None,
            "responses": benchmark._request_summary(responses, accepted_status),
            "worker_audit": benchmark._audit_summary(events),
            "duplicate_claims": duplicate_claims,
            "requests_with_duplicate_claims": requests_with_duplicate_claims,
            "duplicate_queue_claim_rate": requests_with_duplicate_claims / rounds,
            "queue_claim_amplification": len(claims) / rounds,
            "unique_returned_batch_ids": len(set(returned_batch_ids)),
            "queue_counts": {
                state: len(list(job_queue_root.glob(f"*/{state}/*.json")))
                for state in ("pending", "active", "results")
            },
            "registry_counts": benchmark._registry_counts(work_dir / "registry.sqlite3"),
        }
        benchmark._atomic_write_json(scenario_dir / "summary.json", summary)
        return summary
    finally:
        benchmark._terminate(worker)
        benchmark._terminate(server)
        for handle in handles:
            handle.close()


def main() -> int:
    args = _parse_args()
    if args.rounds <= 0 or args.duplicate_clients <= 1:
        raise ValueError("--rounds must be positive and --duplicate-clients must exceed one")
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_dir = args.output_root.resolve() / f"{timestamp}-retry-faults-{uuid4().hex[:6]}"
    output_dir.mkdir(parents=True)

    summaries = {}
    for mode in ("sync", "async"):
        for scenario in ("concurrent-duplicate", "response-drop-retry"):
            key = f"{mode}-{scenario}"
            print(f"running {key} rounds={args.rounds}", flush=True)
            summaries[key] = _run_scenario(
                mode,
                scenario,
                args.rounds,
                args.duplicate_clients,
                output_dir,
            )
    final = {
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "rounds": args.rounds,
        "duplicate_clients": args.duplicate_clients,
        "summaries": summaries,
    }
    benchmark._atomic_write_json(output_dir / "summary.json", final)
    print(json.dumps({"result_dir": str(output_dir)}, indent=2))

    async_duplicate = summaries["async-concurrent-duplicate"]
    async_drop = summaries["async-response-drop-retry"]
    failures = []
    if async_duplicate["duplicate_claims"] != 0:
        failures.append("async concurrent duplicate generated duplicate claims")
    if async_drop["duplicate_claims"] != 0:
        failures.append("async response-drop retry generated duplicate claims")
    if async_duplicate["responses"]["accepted"] != args.rounds * args.duplicate_clients:
        failures.append("not all async duplicate submissions returned 202")
    if async_drop["responses"]["accepted"] != args.rounds:
        failures.append("not all async response-drop retries returned 202")
    if failures:
        print("fault probe failed: " + "; ".join(failures), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
