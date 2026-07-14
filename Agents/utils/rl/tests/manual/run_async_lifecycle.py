#!/usr/bin/env python3
"""Orchestrate async lifecycle load and restart fault injection."""

from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

import run_case as harness


SCRIPT_DIR = Path(__file__).resolve().parent
SERVER_SCRIPT = SCRIPT_DIR / "benchmark_server.py"
LOAD_SCRIPT = SCRIPT_DIR / "load_async_batches.py"
COLLECTOR_SCRIPT = SCRIPT_DIR / "collect_metrics.py"
RESTART_PHASES = {"none", "queued", "running", "mixed-terminal", "terminal"}


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, default=SCRIPT_DIR / "results")
    parser.add_argument("--async-batch-size", type=int)
    parser.add_argument("--restart-phase", choices=sorted(RESTART_PHASES))
    parser.add_argument("--skip-metrics", action="store_true")
    return parser.parse_args()


def _slug(value: str) -> str:
    return "".join(
        character if character.isalnum() or character in "-_" else "-"
        for character in value
    )


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _queue_counts(work_dir: Path) -> dict[str, int]:
    root = work_dir / "queue" / "jobs"
    return {
        state: len(list(root.glob(f"*/{state}/*.json")))
        for state in ("pending", "active", "results")
    }


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _wait_for(
    description: str,
    predicate: Callable[[], bool],
    timeout: float,
    watched: list[subprocess.Popen[Any]],
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for process in watched:
            if process.poll() is not None:
                raise RuntimeError(
                    f"process {process.args!r} exited with code {process.returncode} "
                    f"while waiting for {description}"
                )
        if predicate():
            return
        time.sleep(0.02)
    raise TimeoutError(f"timed out waiting for {description}")


def _start_server(
    case: dict[str, Any],
    work_dir: Path,
    run_dir: Path,
    port: int,
    generation: int,
    skip_metrics: bool,
) -> tuple[subprocess.Popen[Any], Any, subprocess.Popen[Any] | None, Any | None, Path]:
    maximum_batch = int(
        case.get(
            "server_max_trials_per_batch",
            max(int(case["async_batch_size"]), 2048),
        )
    )
    if int(case["async_batch_size"]) > maximum_batch:
        raise ValueError("async_batch_size exceeds server_max_trials_per_batch")
    server, server_handle = harness._start_process(
        [
            sys.executable,
            str(SERVER_SCRIPT),
            "--port",
            str(port),
            "--work-dir",
            str(work_dir),
            "--max-trials-per-batch",
            str(maximum_batch),
        ],
        run_dir / f"server-{generation}.log",
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        harness._wait_for_server(base_url, server)
    except Exception:
        harness._terminate(server)
        server_handle.close()
        raise
    if skip_metrics:
        return server, server_handle, None, None, run_dir / f"stop-metrics-{generation}"

    stop_file = run_dir / f"stop-metrics-{generation}"
    collector, collector_handle = harness._start_process(
        [
            sys.executable,
            str(COLLECTOR_SCRIPT),
            "--pid",
            str(server.pid),
            "--output",
            str(run_dir / f"metrics-{generation}.jsonl"),
            "--stop-file",
            str(stop_file),
            "--interval-seconds",
            str(case.get("metric_interval_seconds", 0.05)),
        ],
        run_dir / f"collector-{generation}.log",
    )
    return server, server_handle, collector, collector_handle, stop_file


def _stop_collector(process: subprocess.Popen[Any] | None, stop_file: Path) -> None:
    if process is None or process.poll() is not None:
        return
    stop_file.write_text("stop\n", encoding="utf-8")
    try:
        process.wait(timeout=5.0)
    except subprocess.TimeoutExpired:
        harness._terminate(process)


def _stop_server(process: subprocess.Popen[Any], restart_style: str) -> None:
    if process.poll() is not None:
        return
    if restart_style == "kill":
        process.kill()
        process.wait(timeout=5.0)
        return
    harness._terminate(process)


def _event_counts(path: Path) -> dict[str, int]:
    events = harness._load_jsonl(path)
    return dict(sorted(Counter(str(event.get("event")) for event in events).items()))


def _metric_generations(run_dir: Path) -> list[dict[str, Any]]:
    summaries = []
    for path in sorted(run_dir.glob("metrics-*.jsonl")):
        samples = harness._load_jsonl(path)
        summary = harness._metrics_summary(samples)
        if summary is not None:
            summary["source"] = path.name
            summary["rss_start_to_end_kib"] = int(samples[-1]["rss_kib"]) - int(
                samples[0]["rss_kib"]
            )
            summary["fd_start_to_end"] = int(samples[-1]["open_fds"]) - int(
                samples[0]["open_fds"]
            )
            summaries.append(summary)
    return summaries


def _validate(
    case: dict[str, Any],
    load: dict[str, Any],
    registry: dict[str, int] | None,
    audit: dict[str, Any],
    restart: dict[str, Any],
) -> list[str]:
    trials = int(case["trials"])
    batches = math.ceil(trials / int(case["async_batch_size"]))
    expected_terminal = int(case.get("expected_terminal_trials", trials))
    failures = []
    checks = (
        (
            not case.get("admission_rejection_probe_trials")
            or load.get("admission_rejection_probe", {}).get("status") == 400,
            "oversized admission probe was not rejected with HTTP 400",
        ),
        (load.get("recovered_batch_handles") == batches, "batch handle count mismatch"),
        (load.get("unique_batch_handles") == batches, "logical batch was duplicated"),
        (load.get("terminal_trials") == expected_terminal, "terminal trial count mismatch"),
        (load.get("available_results") == expected_terminal, "authoritative result count mismatch"),
        (load.get("unknown_outcome_count") == 0, "unknown outcomes remain after recovery"),
        (load.get("lost_result_count") == 0, "terminal results were lost"),
        (load.get("duplicate_trial_execution_ids") == 0, "duplicate trial execution IDs returned"),
        (load.get("duplicate_client_trial_ids") == 0, "duplicate client trial IDs returned"),
        (load.get("status", {}).get("http_502_504") == 0, "status path returned 502/504"),
        (load.get("results", {}).get("http_502_504") == 0, "results path returned 502/504"),
        (audit.get("duplicate_claims") == 0, "worker claimed a logical trial more than once"),
        (audit.get("duplicate_result_writes") == 0, "worker wrote a result more than once"),
        (
            load.get("status", {}).get("progress_drops_completed")
            == len(load.get("status", {}).get("progress_drop_targets", [])),
            "not all configured progress disconnects were injected",
        ),
    )
    failures.extend(message for passed, message in checks if not passed)

    if registry is None:
        failures.append("registry database is unavailable")
    else:
        expected_counts = {
            "async_trial_batches": batches,
            "trial_executions": trials,
            "enqueue_intents": trials,
            "idempotency_records": batches,
            "unmaterialized_intents": 0,
        }
        for name, expected in expected_counts.items():
            if registry.get(name) != expected:
                failures.append(
                    f"registry {name} mismatch: expected={expected} actual={registry.get(name)}"
                )

    if restart["phase"] != "none" and not restart["performed"]:
        failures.append("configured server restart was not performed")
    return failures


def _report(summary: dict[str, Any]) -> str:
    case = summary["case"]
    load = summary["load"]
    restart = summary["restart"]
    status = load["status"]
    results = load["results"]
    rejection_probe = load.get("admission_rejection_probe") or {}
    verdict = "PASS" if not summary["validation_failures"] else "FAIL"
    lines = [
        f"# Async Lifecycle Validation: {case['name']}",
        "",
        f"Verdict: **{verdict}**",
        "",
        "## Boundary",
        "",
        "The HTTP handler, SQLite registry, idempotent admission, startup reconciliation,",
        "and pending/active/results file queue are production code. Harbor execution is",
        "replaced by a deterministic synthetic worker.",
        "",
        "## Configuration",
        "",
        f"- Trials: {case['trials']}",
        f"- Batch size: {case['async_batch_size']}",
        f"- Execution capacity: {case['execution_capacity']}",
        f"- Restart phase: {restart['phase']}",
        f"- Classification: {case.get('classification', 'unspecified')}",
        "",
        "## Results",
        "",
        "| Metric | Value |",
        "| --- | ---: |",
        f"| Recovered batch handles | {load['recovered_batch_handles']} |",
        f"| Oversized admission status | {harness._display(rejection_probe.get('status'))} |",
        f"| Terminal trials | {load['terminal_trials']} |",
        f"| Available results | {load['available_results']} |",
        f"| Injected response losses | {load['injected_response_losses']} |",
        f"| Unknown outcomes | {load['unknown_outcome_count']} |",
        f"| Lost results | {load['lost_result_count']} |",
        f"| Duplicate execution IDs | {load['duplicate_trial_execution_ids']} |",
        f"| Duplicate queue claims | {summary['worker_audit']['duplicate_claims']} |",
        f"| Duplicate result writes | {summary['worker_audit']['duplicate_result_writes']} |",
        f"| Status wire requests | {status['wire_requests']} |",
        f"| Status QPS | {status['qps']:.3f} |",
        f"| Status latency p95 (s) | {harness._display(status['latency_seconds']['p95'])} |",
        f"| Result wire requests | {results['wire_requests']} |",
        f"| Recovery time p95 (s) | {harness._display(load['recovery_seconds']['p95'])} |",
        f"| HTTP 502/504 | {status['http_502_504'] + results['http_502_504']} |",
        "",
    ]
    if summary["validation_failures"]:
        lines.extend(
            ["## Validation Failures", ""]
            + [f"- {failure}" for failure in summary["validation_failures"]]
            + [""]
        )
    lines.extend(
        [
            "## Interpretation",
            "",
            "This run validates the Agent Fleet control-plane lifecycle and configured fault",
            "boundaries. It does not measure real Harbor, model inference, reward, trainer",
            "throughput, or a production Proxy unless an external Proxy is explicitly used.",
            "",
        ]
    )
    return "\n".join(lines)


def run(args: argparse.Namespace) -> tuple[Path, dict[str, Any]]:
    case = harness._load_case(args.case.resolve())
    if args.async_batch_size is not None:
        if args.async_batch_size <= 0:
            raise ValueError("--async-batch-size must be positive")
        case["async_batch_size"] = args.async_batch_size
        case["name"] = f"{case['name']}-batch-{args.async_batch_size}"
    restart_phase = args.restart_phase or str(case.get("restart_phase", "none"))
    if restart_phase not in RESTART_PHASES:
        raise ValueError(f"unsupported restart_phase: {restart_phase}")
    restart_style = str(case.get("restart_style", "kill"))
    if restart_style not in {"kill", "terminate"}:
        raise ValueError("restart_style must be kill or terminate")

    run_dir = args.output_root.expanduser().resolve() / (
        f"{_timestamp()}-{_slug(str(case['name']))}-{uuid4().hex[:6]}"
    )
    work_dir = run_dir / "work"
    run_dir.mkdir(parents=True)
    harness._atomic_write_json(run_dir / "case.json", case)

    port = harness._free_port()
    base_url = f"http://127.0.0.1:{port}"
    manifest_path = run_dir / "handles.json"
    load_summary_path = run_dir / "load-summary.json"
    all_handles: list[Any] = []
    collectors: list[tuple[subprocess.Popen[Any] | None, Path]] = []
    server: subprocess.Popen[Any] | None = None
    worker: subprocess.Popen[Any] | None = None
    load_process: subprocess.Popen[Any] | None = None
    generation = 0
    restart_record: dict[str, Any] = {
        "phase": restart_phase,
        "style": restart_style,
        "performed": False,
        "observed_queue_counts": None,
        "downtime_seconds": None,
    }
    started = time.monotonic()
    try:
        server, handle, collector, collector_handle, stop_file = _start_server(
            case, work_dir, run_dir, port, generation, args.skip_metrics
        )
        all_handles.append(handle)
        if collector_handle is not None:
            all_handles.append(collector_handle)
        collectors.append((collector, stop_file))

        if restart_phase != "queued":
            worker, worker_handle = harness._start_process(
                harness._worker_command(case, work_dir, run_dir / "worker-audit.jsonl"),
                run_dir / "worker.log",
            )
            all_handles.append(worker_handle)

        load_process, load_handle = harness._start_process(
            [
                sys.executable,
                str(LOAD_SCRIPT),
                "--case",
                str(run_dir / "case.json"),
                "--base-url",
                base_url,
                "--work-dir",
                str(work_dir),
                "--output",
                str(load_summary_path),
                "--manifest",
                str(manifest_path),
            ],
            run_dir / "load.log",
        )
        all_handles.append(load_handle)

        if restart_phase != "none":
            expected_batches = math.ceil(
                int(case["trials"]) / int(case["async_batch_size"])
            )
            phase_timeout = float(case.get("lifecycle_timeout_seconds", 120.0))
            _wait_for(
                "all durable batch handles",
                lambda: len((_read_json(manifest_path) or {}).get("handles", []))
                == expected_batches,
                phase_timeout,
                [server, load_process],
            )

            def phase_reached() -> bool:
                counts = _queue_counts(work_dir)
                if restart_phase == "queued":
                    return counts["pending"] == int(case["trials"])
                if restart_phase == "running":
                    return counts["active"] > 0
                if restart_phase == "mixed-terminal":
                    return counts["active"] > 0 and counts["results"] > 0
                expected = int(case.get("expected_terminal_trials", case["trials"]))
                return counts["results"] >= expected

            watched = [server, load_process]
            if worker is not None:
                watched.append(worker)
            _wait_for(f"restart phase {restart_phase}", phase_reached, phase_timeout, watched)
            restart_record["observed_queue_counts"] = _queue_counts(work_dir)

            collector_process, collector_stop = collectors[-1]
            _stop_collector(collector_process, collector_stop)
            _stop_server(server, restart_style)
            downtime_started = time.monotonic()
            time.sleep(float(case.get("restart_downtime_seconds", 0.25)))
            generation += 1
            server, handle, collector, collector_handle, stop_file = _start_server(
                case, work_dir, run_dir, port, generation, args.skip_metrics
            )
            all_handles.append(handle)
            if collector_handle is not None:
                all_handles.append(collector_handle)
            collectors.append((collector, stop_file))
            restart_record["performed"] = True
            restart_record["downtime_seconds"] = time.monotonic() - downtime_started

            if restart_phase == "queued":
                worker, worker_handle = harness._start_process(
                    harness._worker_command(case, work_dir, run_dir / "worker-audit.jsonl"),
                    run_dir / "worker.log",
                )
                all_handles.append(worker_handle)

        driver_timeout = float(case.get("lifecycle_timeout_seconds", 120.0)) + float(
            case.get("recovery_timeout_seconds", 30.0)
        ) + float(case.get("result_fetch_delay_seconds", 0.0)) + 30.0
        load_process.wait(timeout=driver_timeout)
        if load_process.returncode != 0:
            raise RuntimeError(f"load driver exited with code {load_process.returncode}")
        load_summary = _read_json(load_summary_path)
        if load_summary is None:
            raise RuntimeError("load driver did not write a valid summary")

        if worker is not None:
            harness._terminate(worker)
        for process, stop_file in collectors:
            _stop_collector(process, stop_file)
        if server is not None:
            harness._terminate(server)

        audit_events = harness._load_jsonl(run_dir / "worker-audit.jsonl")
        audit_summary = harness._audit_summary(audit_events)
        registry_counts = harness._registry_counts(work_dir / "registry.sqlite3")
        server_metrics = _metric_generations(run_dir)
        summary = {
            "schema_version": 1,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "wall_seconds": time.monotonic() - started,
            "case": case,
            "boundary": {
                "production": [
                    "HTTP handler",
                    "SQLite registry",
                    "idempotent admission",
                    "startup reconciliation",
                    "pending/active/results file queue",
                ],
                "substituted": ["Harbor trial execution", "model inference", "reward"],
            },
            "restart": restart_record,
            "load": load_summary,
            "queue_counts": _queue_counts(work_dir),
            "registry_counts": registry_counts,
            "worker_audit": audit_summary,
            "worker_event_counts": dict(
                sorted(Counter(str(event.get("event")) for event in audit_events).items())
            ),
            "server_event_counts": _event_counts(work_dir / "requests.jsonl"),
            "server_metrics": server_metrics,
            "metrics_skipped": args.skip_metrics,
        }
        summary["validation_failures"] = _validate(
            case, load_summary, registry_counts, audit_summary, restart_record
        )
        expected_metric_generations = 2 if restart_record["performed"] else 1
        if not args.skip_metrics and len(server_metrics) != expected_metric_generations:
            summary["validation_failures"].append(
                "Linux resource metrics are incomplete: "
                f"expected_generations={expected_metric_generations} "
                f"observed_generations={len(server_metrics)}"
            )
        harness._atomic_write_json(run_dir / "summary.json", summary)
        harness._atomic_write_text(run_dir / "report.md", _report(summary))
        return run_dir, summary
    finally:
        if load_process is not None:
            harness._terminate(load_process)
        if worker is not None:
            harness._terminate(worker)
        for process, stop_file in collectors:
            _stop_collector(process, stop_file)
        if server is not None:
            harness._terminate(server)
        for handle in all_handles:
            handle.close()


def main() -> int:
    args = _parse_args()
    run_dir, summary = run(args)
    print(
        json.dumps(
            {
                "result_dir": str(run_dir),
                "report": str(run_dir / "report.md"),
                "validation_failures": summary["validation_failures"],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 1 if summary["validation_failures"] else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (
        OSError,
        ValueError,
        json.JSONDecodeError,
        RuntimeError,
        TimeoutError,
        subprocess.TimeoutExpired,
    ) as exc:
        print(f"async lifecycle validation failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
