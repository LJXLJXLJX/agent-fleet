#!/usr/bin/env python3
"""Run sequential sync and async control-plane benchmarks on one host."""

from __future__ import annotations

import argparse
import http.client
import json
import math
import os
import platform
import resource
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from collections import Counter
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse
from uuid import uuid4


SCRIPT_DIR = Path(__file__).resolve().parent
SERVER_SCRIPT = SCRIPT_DIR / "benchmark_server.py"
WORKER_SCRIPT = SCRIPT_DIR / "synthetic_queue_worker.py"
COLLECTOR_SCRIPT = SCRIPT_DIR / "collect_process_metrics.py"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, default=SCRIPT_DIR / "results")
    parser.add_argument("--async-batch-size", type=int)
    parser.add_argument("--skip-metrics", action="store_true")
    parser.add_argument("--modes", default="sync,async")
    return parser.parse_args()


def _load_case(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("case must be a JSON object")
    required = {"name", "trials", "async_batch_size", "execution_capacity"}
    missing = sorted(required - value.keys())
    if missing:
        raise ValueError(f"case is missing required fields: {missing}")
    trials = int(value["trials"])
    batch_size = int(value["async_batch_size"])
    capacity = int(value["execution_capacity"])
    if trials <= 0 or batch_size <= 0 or capacity <= 0:
        raise ValueError("trials, async_batch_size and execution_capacity must be positive")
    if "delay_seconds" not in value and "delay_profile" not in value:
        raise ValueError("case requires delay_seconds or delay_profile")
    if "delay_profile" in value:
        profile = value["delay_profile"]
        if not isinstance(profile, list) or not profile:
            raise ValueError("delay_profile must be a non-empty array")
        total_fraction = sum(float(item["fraction"]) for item in profile)
        if abs(total_fraction - 1.0) > 1e-6:
            raise ValueError("delay_profile fractions must sum to 1.0")
    return value


def _atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    temporary.replace(path)


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid4().hex}.tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _request_json(
    method: str,
    url: str,
    payload: dict[str, Any] | None,
    timeout: float,
) -> dict[str, Any]:
    parsed = urlparse(url)
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {} if body is None else {"Content-Type": "application/json"}
    started = time.monotonic()
    connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=timeout)
    try:
        connection.request(method, parsed.path or "/", body=body, headers=headers)
        response = connection.getresponse()
        raw_body = response.read()
        decoded: Any = {}
        if raw_body:
            try:
                decoded = json.loads(raw_body.decode("utf-8"))
            except json.JSONDecodeError:
                decoded = {"raw_body": raw_body.decode("utf-8", errors="replace")}
        return {
            "status": response.status,
            "latency_seconds": time.monotonic() - started,
            "body": decoded,
            "error": None,
        }
    except Exception as exc:
        return {
            "status": None,
            "latency_seconds": time.monotonic() - started,
            "body": None,
            "error": {"type": type(exc).__name__, "message": str(exc)},
        }
    finally:
        connection.close()


def _wait_for_server(base_url: str, process: subprocess.Popen[Any], timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"benchmark server exited with code {process.returncode}")
        result = _request_json("GET", f"{base_url}/health", None, 1.0)
        if result["status"] == 200:
            return
        time.sleep(0.05)
    raise TimeoutError(f"benchmark server did not become healthy: {base_url}")


def _run_simultaneously(
    operations: list[Callable[[], dict[str, Any]]],
    timeout: float,
) -> list[dict[str, Any]]:
    if not operations:
        return []
    barrier = threading.Barrier(len(operations) + 1)

    def invoke(operation: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        try:
            barrier.wait(timeout=60.0)
        except threading.BrokenBarrierError as exc:
            raise RuntimeError("load generator could not start all client threads") from exc
        return operation()

    with ThreadPoolExecutor(max_workers=len(operations), thread_name_prefix="benchmark-client") as pool:
        futures: list[Future[dict[str, Any]]] = [
            pool.submit(invoke, operation) for operation in operations
        ]
        try:
            barrier.wait(timeout=60.0)
        except threading.BrokenBarrierError as exc:
            raise RuntimeError("load generator thread barrier failed") from exc
        return [future.result(timeout=timeout) for future in futures]


def _sync_payload(index: int, run_token: str, work_dir: Path, timeout: float) -> dict[str, Any]:
    return {
        "request_id": f"sync-{run_token}-{index:05d}",
        "session_id": f"sync-session-{run_token}-{index:05d}",
        "task_id": "1",
        "dataset_name": "benchmark",
        "dataset_root": str((work_dir / "dataset").resolve()),
        "ray_job_id": f"benchmark-sync-{run_token}",
        "request_timeout": timeout,
    }


def _async_payload(
    batch_index: int,
    first_trial: int,
    trial_count: int,
    run_token: str,
    work_dir: Path,
) -> dict[str, Any]:
    request_id = f"async-{run_token}-batch-{batch_index:04d}"
    return {
        "request_id": request_id,
        "client_batch_id": f"client-{request_id}",
        "trainer_run_id": f"benchmark-{run_token}",
        "batching_key": {
            "dataset_name": "benchmark",
            "dataset_root": str((work_dir / "dataset").resolve()),
            "ray_job_id": f"benchmark-async-{run_token}",
            "policy_version": "benchmark-policy-0",
        },
        "trials": [
            {
                "client_trial_id": f"async-client-trial-{run_token}-{index:05d}",
                "session_id": f"async-session-{run_token}-{index:05d}",
                "task_id": "1",
                "group_id": index,
                "rollout_step": 0,
                "policy_version": "benchmark-policy-0",
                "payload": {},
            }
            for index in range(first_trial, first_trial + trial_count)
        ],
    }


def _maximum_delay(case: dict[str, Any]) -> float:
    if "delay_profile" in case:
        return max(float(item["seconds"]) for item in case["delay_profile"])
    return float(case["delay_seconds"])


def _execution_timeout(case: dict[str, Any]) -> float:
    waves = math.ceil(int(case["trials"]) / int(case["execution_capacity"]))
    return max(120.0, waves * _maximum_delay(case) + 120.0)


def _result_files(job_queue_root: Path) -> list[Path]:
    return list(job_queue_root.glob("*/results/*.json"))


def _wait_for_results(job_queue_root: Path, expected: int, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if len(_result_files(job_queue_root)) >= expected:
            return
        time.sleep(0.02)
    raise TimeoutError(
        f"timed out waiting for synthetic results: expected={expected}, "
        f"actual={len(_result_files(job_queue_root))}"
    )


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            value = json.loads(line)
            if isinstance(value, dict):
                records.append(value)
    return records


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = max(0, math.ceil(percentile * len(ordered)) - 1)
    return ordered[rank]


def _request_summary(requests: list[dict[str, Any]], accepted_status: int) -> dict[str, Any]:
    latencies = [float(item["latency_seconds"]) for item in requests]
    statuses = Counter(str(item["status"]) for item in requests)
    return {
        "requests": len(requests),
        "accepted": sum(item["status"] == accepted_status for item in requests),
        "transport_errors": sum(item["status"] is None for item in requests),
        "status_counts": dict(sorted(statuses.items())),
        "latency_seconds": {
            "p50": _percentile(latencies, 0.50),
            "p95": _percentile(latencies, 0.95),
            "p99": _percentile(latencies, 0.99),
            "max": max(latencies) if latencies else None,
        },
    }


def _metrics_summary(samples: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not samples:
        return None
    baseline_threads = min(int(item["threads"]) for item in samples)
    baseline_fds = min(int(item["open_fds"]) for item in samples)
    baseline_sockets = min(int(item["socket_fds"]) for item in samples)
    baseline_established_tcp = min(int(item["established_tcp"]) for item in samples)
    baseline_rss = min(int(item["rss_kib"]) for item in samples)
    connection_seconds = 0.0
    for previous, current in zip(samples, samples[1:]):
        elapsed = max(0.0, float(current["monotonic"]) - float(previous["monotonic"]))
        active_connections = max(
            0,
            int(previous["established_tcp"]) - baseline_established_tcp,
        )
        connection_seconds += active_connections * elapsed
    return {
        "samples": len(samples),
        "baseline": {
            "threads": baseline_threads,
            "open_fds": baseline_fds,
            "socket_fds": baseline_sockets,
            "established_tcp": baseline_established_tcp,
            "rss_kib": baseline_rss,
        },
        "peak": {
            "threads": max(int(item["threads"]) for item in samples),
            "handler_thread_delta": max(int(item["threads"]) for item in samples)
            - baseline_threads,
            "open_fds": max(int(item["open_fds"]) for item in samples),
            "open_fd_delta": max(int(item["open_fds"]) for item in samples) - baseline_fds,
            "socket_fds": max(int(item["socket_fds"]) for item in samples),
            "active_socket_delta": max(int(item["socket_fds"]) for item in samples)
            - baseline_sockets,
            "tcp_sockets": max(int(item["tcp_sockets"]) for item in samples),
            "established_tcp": max(int(item["established_tcp"]) for item in samples),
            "established_tcp_delta": max(int(item["established_tcp"]) for item in samples)
            - baseline_established_tcp,
            "rss_kib": max(int(item["rss_kib"]) for item in samples),
            "rss_delta_kib": max(int(item["rss_kib"]) for item in samples) - baseline_rss,
            "cpu_percent": max(float(item["cpu_percent"]) for item in samples),
        },
        "cpu_seconds_delta": float(samples[-1]["cpu_seconds"])
        - float(samples[0]["cpu_seconds"]),
        "connection_seconds": connection_seconds,
    }


def _registry_counts(path: Path) -> dict[str, int] | None:
    if not path.exists():
        return None
    with sqlite3.connect(path) as connection:
        tables = (
            "async_trial_batches",
            "trial_executions",
            "enqueue_intents",
            "idempotency_records",
        )
        counts = {
            table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in tables
        }
        counts["unmaterialized_intents"] = int(
            connection.execute(
                "SELECT COUNT(*) FROM enqueue_intents WHERE materialized_at IS NULL"
            ).fetchone()[0]
        )
        return counts


def _audit_summary(events: list[dict[str, Any]]) -> dict[str, Any]:
    claims = [event for event in events if event.get("event") == "claim"]
    finishes = [event for event in events if event.get("event") == "finish"]
    claim_counts = Counter(str(event.get("request_id")) for event in claims)
    duplicate_claims = sum(count - 1 for count in claim_counts.values() if count > 1)
    throughput = None
    if claims and finishes:
        started = min(float(event["monotonic"]) for event in claims)
        finished = max(float(event["monotonic"]) for event in finishes)
        if finished > started:
            throughput = len(finishes) / (finished - started)
    return {
        "claims": len(claims),
        "finishes": len(finishes),
        "unique_request_ids": len(claim_counts),
        "duplicate_claims": duplicate_claims,
        "completion_throughput_per_second": throughput,
    }


def _terminate(process: subprocess.Popen[Any] | None, timeout: float = 5.0) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=timeout)


def _start_process(command: list[str], log_path: Path) -> tuple[subprocess.Popen[Any], Any]:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handle = log_path.open("w", encoding="utf-8")
    process = subprocess.Popen(
        command,
        stdout=handle,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    return process, handle


def _worker_command(case: dict[str, Any], work_dir: Path, audit_log: Path) -> list[str]:
    command = [
        sys.executable,
        str(WORKER_SCRIPT),
        "--job-queue-root",
        str(work_dir / "queue" / "jobs"),
        "--capacity",
        str(case["execution_capacity"]),
        "--audit-log",
        str(audit_log),
    ]
    if "delay_profile" in case:
        command.extend(
            ["--delay-profile-json", json.dumps(case["delay_profile"], separators=(",", ":"))]
        )
    else:
        command.extend(["--delay-seconds", str(case["delay_seconds"])])
    return command


def _run_mode(
    mode: str,
    case: dict[str, Any],
    run_dir: Path,
    *,
    skip_metrics: bool,
) -> dict[str, Any]:
    mode_dir = run_dir / mode
    work_dir = mode_dir / "work"
    mode_dir.mkdir(parents=True, exist_ok=True)
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    timeout = _execution_timeout(case)
    run_token = uuid4().hex[:10]
    server: subprocess.Popen[Any] | None = None
    worker: subprocess.Popen[Any] | None = None
    collector: subprocess.Popen[Any] | None = None
    handles: list[Any] = []
    stop_file = mode_dir / "stop-metrics"
    requests: list[dict[str, Any]] = []
    started = time.monotonic()
    try:
        server, server_handle = _start_process(
            [
                sys.executable,
                str(SERVER_SCRIPT),
                "--port",
                str(port),
                "--work-dir",
                str(work_dir),
                "--max-trials-per-batch",
                str(max(int(case["async_batch_size"]), 2048)),
            ],
            mode_dir / "server.log",
        )
        handles.append(server_handle)
        _wait_for_server(base_url, server)

        worker, worker_handle = _start_process(
            _worker_command(case, work_dir, mode_dir / "worker-audit.jsonl"),
            mode_dir / "worker.log",
        )
        handles.append(worker_handle)

        if not skip_metrics:
            collector, collector_handle = _start_process(
                [
                    sys.executable,
                    str(COLLECTOR_SCRIPT),
                    "--pid",
                    str(server.pid),
                    "--output",
                    str(mode_dir / "metrics.jsonl"),
                    "--stop-file",
                    str(stop_file),
                    "--interval-seconds",
                    str(case.get("metric_interval_seconds", 0.05)),
                ],
                mode_dir / "collector.log",
            )
            handles.append(collector_handle)
        time.sleep(0.25)

        trials = int(case["trials"])
        if mode == "sync":
            operations = [
                (
                    lambda index=index: _request_json(
                        "POST",
                        f"{base_url}/run_trial",
                        _sync_payload(index, run_token, work_dir, timeout),
                        timeout,
                    )
                )
                for index in range(trials)
            ]
            requests = _run_simultaneously(operations, timeout + 30.0)
            accepted_status = 200
            returned_mappings = None
        else:
            batch_size = int(case["async_batch_size"])
            batches = math.ceil(trials / batch_size)
            payloads = []
            for batch_index in range(batches):
                first_trial = batch_index * batch_size
                count = min(batch_size, trials - first_trial)
                payloads.append(
                    _async_payload(batch_index, first_trial, count, run_token, work_dir)
                )
            operations = [
                (
                    lambda payload=payload: _request_json(
                        "POST",
                        f"{base_url}/async_trial_batches",
                        payload,
                        30.0,
                    )
                )
                for payload in payloads
            ]
            requests = _run_simultaneously(operations, 60.0)
            accepted_status = 202
            returned_mappings = sum(
                len(item["body"].get("trials", []))
                for item in requests
                if item["status"] == 202 and isinstance(item["body"], dict)
            )

        _wait_for_results(work_dir / "queue" / "jobs", int(case["trials"]), timeout)
        wall_seconds = time.monotonic() - started
        if collector is not None:
            stop_file.write_text("stop\n", encoding="utf-8")
            collector.wait(timeout=10.0)

        metrics = _load_jsonl(mode_dir / "metrics.jsonl")
        audit = _load_jsonl(mode_dir / "worker-audit.jsonl")
        job_queue_root = work_dir / "queue" / "jobs"
        queue_counts = {
            state: len(list(job_queue_root.glob(f"*/{state}/*.json")))
            for state in ("pending", "active", "results")
        }
        summary = {
            "mode": mode,
            "wall_seconds": wall_seconds,
            "request_summary": _request_summary(requests, accepted_status),
            "returned_trial_mappings": returned_mappings,
            "queue_counts": queue_counts,
            "registry_counts": _registry_counts(work_dir / "registry.sqlite3"),
            "worker_audit": _audit_summary(audit),
            "server_metrics": _metrics_summary(metrics),
            "raw_requests": requests,
        }
        _atomic_write_json(mode_dir / "summary.json", summary)
        return summary
    finally:
        if collector is not None and collector.poll() is None:
            stop_file.write_text("stop\n", encoding="utf-8")
            try:
                collector.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                _terminate(collector)
        _terminate(worker)
        _terminate(server)
        for handle in handles:
            handle.close()


def _ratio(numerator: float | int | None, denominator: float | int | None) -> float | None:
    if numerator is None or denominator in {None, 0}:
        return None
    return float(numerator) / float(denominator)


def _comparison(mode_summaries: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    sync = mode_summaries.get("sync")
    async_summary = mode_summaries.get("async")
    if sync is None or async_summary is None:
        return None
    sync_metrics = sync.get("server_metrics") or {}
    async_metrics = async_summary.get("server_metrics") or {}
    sync_peak = sync_metrics.get("peak") or {}
    async_peak = async_metrics.get("peak") or {}
    return {
        "sync_to_async_handler_thread_ratio": _ratio(
            sync_peak.get("handler_thread_delta"), async_peak.get("handler_thread_delta")
        ),
        "sync_to_async_established_tcp_ratio": _ratio(
            sync_peak.get("established_tcp_delta"), async_peak.get("established_tcp_delta")
        ),
        "sync_to_async_connection_seconds_ratio": _ratio(
            sync_metrics.get("connection_seconds"), async_metrics.get("connection_seconds")
        ),
        "async_to_sync_completion_throughput_ratio": _ratio(
            async_summary["worker_audit"].get("completion_throughput_per_second"),
            sync["worker_audit"].get("completion_throughput_per_second"),
        ),
    }


def _validation_failures(
    case: dict[str, Any], mode_summaries: dict[str, dict[str, Any]]
) -> list[str]:
    failures = []
    expected_trials = int(case["trials"])
    for mode, summary in mode_summaries.items():
        expected_requests = expected_trials if mode == "sync" else math.ceil(
            expected_trials / int(case["async_batch_size"])
        )
        if summary["request_summary"]["accepted"] != expected_requests:
            failures.append(f"{mode}: accepted request count mismatch")
        if summary["queue_counts"]["results"] != expected_trials:
            failures.append(f"{mode}: result artifact count mismatch")
        if summary["worker_audit"]["finishes"] != expected_trials:
            failures.append(f"{mode}: worker finish count mismatch")
        if mode == "async" and summary["returned_trial_mappings"] != expected_trials:
            failures.append("async: returned trial mapping count mismatch")
    return failures


def _display(value: Any, digits: int = 3) -> str:
    if value is None:
        return "not collected"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    if isinstance(value, dict):
        return ", ".join(f"{key}={item}" for key, item in value.items())
    return str(value)


def _mode_report_rows(mode: str, summary: dict[str, Any]) -> list[tuple[str, str, str]]:
    requests = summary["request_summary"]
    latency = requests["latency_seconds"]
    audit = summary["worker_audit"]
    rows = [
        ("HTTP requests", mode, _display(requests["requests"])),
        ("Accepted requests", mode, _display(requests["accepted"])),
        ("HTTP statuses", mode, _display(requests["status_counts"])),
        ("Transport errors", mode, _display(requests["transport_errors"])),
        ("Request latency p50 (s)", mode, _display(latency["p50"])),
        ("Request latency p95 (s)", mode, _display(latency["p95"])),
        ("Request latency p99 (s)", mode, _display(latency["p99"])),
        ("Request latency max (s)", mode, _display(latency["max"])),
        ("Mode wall time (s)", mode, _display(summary["wall_seconds"])),
        ("Worker claims", mode, _display(audit["claims"])),
        ("Worker finishes", mode, _display(audit["finishes"])),
        ("Duplicate queue claims", mode, _display(audit["duplicate_claims"])),
        (
            "Completion throughput (trials/s)",
            mode,
            _display(audit["completion_throughput_per_second"]),
        ),
        ("Result artifacts", mode, _display(summary["queue_counts"]["results"])),
    ]
    if mode == "async":
        rows.append(("Returned trial mappings", mode, _display(summary["returned_trial_mappings"])))
    return rows


def _render_report(final_summary: dict[str, Any]) -> str:
    case = final_summary["case"]
    environment = final_summary["environment"]
    modes = final_summary["modes"]
    comparison = final_summary.get("comparison") or {}
    validation = final_summary["validation"]
    mode_names = [mode for mode in ("sync", "async") if mode in modes]

    values: dict[str, dict[str, str]] = {}
    row_order: list[str] = []
    for mode in mode_names:
        for metric, row_mode, value in _mode_report_rows(mode, modes[mode]):
            if metric not in values:
                values[metric] = {}
                row_order.append(metric)
            values[metric][row_mode] = value

    lines = [
        "# Sync vs Async Benchmark Report",
        "",
        f"- Result: **{validation['status']}**",
        f"- Case: `{case['name']}`",
        f"- Classification: `{case.get('classification', 'unspecified')}`",
        f"- Created: `{final_summary['created_at']}`",
        f"- Host: `{environment['hostname']}`",
        f"- Platform: `{environment['platform']}`",
        f"- Python / SQLite: `{environment['python_version']}` / `{environment['sqlite_version']}`",
        "",
        "## Configuration",
        "",
        "| Logical trials | Async batch size | Execution capacity | Delay |",
        "| ---: | ---: | ---: | --- |",
        (
            f"| {case['trials']} | {case['async_batch_size']} | {case['execution_capacity']} | "
            f"{_display(case.get('delay_seconds', case.get('delay_profile')))} |"
        ),
        "",
        "## Results",
        "",
        "| Metric | " + " | ".join(name.title() for name in mode_names) + " |",
        "| --- | " + " | ".join("---:" for _ in mode_names) + " |",
    ]
    for metric in row_order:
        lines.append(
            f"| {metric} | "
            + " | ".join(values[metric].get(mode, "n/a") for mode in mode_names)
            + " |"
        )

    lines.extend(["", "## Comparison", ""])
    if comparison:
        lines.extend(
            [
                "| Ratio | Value |",
                "| --- | ---: |",
                f"| Sync / async handler threads | {_display(comparison.get('sync_to_async_handler_thread_ratio'))} |",
                f"| Sync / async established TCP | {_display(comparison.get('sync_to_async_established_tcp_ratio'))} |",
                f"| Sync / async connection-seconds | {_display(comparison.get('sync_to_async_connection_seconds_ratio'))} |",
                f"| Async / sync completion throughput | {_display(comparison.get('async_to_sync_completion_throughput_ratio'))} |",
            ]
        )
    else:
        lines.append("A comparison requires both sync and async modes.")

    lines.extend(["", "## Resource Metrics", ""])
    if environment["metrics_skipped"]:
        lines.append(
            "Not collected. This run used `--skip-metrics`; macOS smoke results are not resource evidence."
        )
    else:
        lines.extend(
            [
                "| Metric | " + " | ".join(name.title() for name in mode_names) + " |",
                "| --- | " + " | ".join("---:" for _ in mode_names) + " |",
            ]
        )
        resource_metrics = (
            ("Peak handler thread delta", "handler_thread_delta"),
            ("Peak established TCP delta", "established_tcp_delta"),
            ("Peak open FD delta", "open_fd_delta"),
            ("Peak RSS delta (KiB)", "rss_delta_kib"),
        )
        for label, key in resource_metrics:
            lines.append(
                f"| {label} | "
                + " | ".join(
                    _display(((modes[mode].get("server_metrics") or {}).get("peak") or {}).get(key))
                    for mode in mode_names
                )
                + " |"
            )
        lines.append(
            "| Connection-seconds | "
            + " | ".join(
                _display((modes[mode].get("server_metrics") or {}).get("connection_seconds"))
                for mode in mode_names
            )
            + " |"
        )

    lines.extend(["", "## Integrity", ""])
    if validation["failures"]:
        lines.extend(f"- FAIL: {failure}" for failure in validation["failures"])
    else:
        lines.append("- All expected requests were accepted.")
        lines.append("- Every logical trial produced one worker finish and one result artifact.")
        if "async" in modes:
            registry = modes["async"].get("registry_counts") or {}
            lines.append(
                "- Async registry: "
                f"{registry.get('async_trial_batches', 0)} batches, "
                f"{registry.get('trial_executions', 0)} trials, "
                f"{registry.get('enqueue_intents', 0)} intents, "
                f"{registry.get('unmaterialized_intents', 0)} unmaterialized intents."
            )

    lines.extend(
        [
            "",
            "## Interpretation Limits",
            "",
            "- This is a synthetic admission and execution-handoff test, not a real Harbor or training result.",
            "- It does not measure final unknown-outcome, lost-result, rolling-deploy, or trainer-stall metrics.",
            "- Public-reference case sizes are structural sample-count references, not proven concurrency norms.",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    args = _parse_args()
    case = _load_case(args.case.resolve())
    if args.async_batch_size is not None:
        if args.async_batch_size <= 0:
            raise ValueError("--async-batch-size must be positive")
        case = dict(case)
        case["async_batch_size"] = args.async_batch_size
        case["name"] = f"{case['name']}-batch-{args.async_batch_size}"
    modes = [item.strip() for item in args.modes.split(",") if item.strip()]
    if not modes or any(mode not in {"sync", "async"} for mode in modes):
        raise ValueError("--modes must contain sync and/or async")
    if not args.skip_metrics and not sys.platform.startswith("linux"):
        raise RuntimeError("decision metrics require Linux; use --skip-metrics only for harness smoke")

    try:
        threading.stack_size(512 * 1024)
    except (RuntimeError, ValueError):
        pass

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = args.output_root.resolve() / f"{timestamp}-{case['name']}-{uuid4().hex[:6]}"
    run_dir.mkdir(parents=True)
    _atomic_write_json(run_dir / "case.json", case)

    summaries: dict[str, dict[str, Any]] = {}
    for mode in modes:
        print(f"running mode={mode} case={case['name']} trials={case['trials']}", flush=True)
        summaries[mode] = _run_mode(mode, case, run_dir, skip_metrics=args.skip_metrics)

    nofile_soft, nofile_hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    final_summary = {
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "case": case,
        "environment": {
            "hostname": socket.gethostname(),
            "platform": platform.platform(),
            "python_version": platform.python_version(),
            "sqlite_version": sqlite3.sqlite_version,
            "cpu_count": os.cpu_count(),
            "nofile_soft": nofile_soft,
            "nofile_hard": nofile_hard,
            "metrics_skipped": args.skip_metrics,
        },
        "modes": summaries,
        "comparison": _comparison(summaries),
    }
    failures = _validation_failures(case, summaries)
    final_summary["validation"] = {
        "status": "PASS" if not failures else "FAIL",
        "failures": failures,
    }
    _atomic_write_json(run_dir / "summary.json", final_summary)
    report_path = run_dir / "report.md"
    _atomic_write_text(report_path, _render_report(final_summary))
    print(
        json.dumps(
            {
                "result_dir": str(run_dir),
                "report": str(report_path),
                "comparison": final_summary["comparison"],
            },
            indent=2,
        )
    )
    if failures:
        print("benchmark validation failed: " + "; ".join(failures), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
