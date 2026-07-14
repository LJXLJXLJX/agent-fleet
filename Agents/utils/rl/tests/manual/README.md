# Manual RL Control-Plane Validation

These explicit test entry points cover load, long-duration, restart, and
transport-fault scenarios that do not belong in default unittest discovery.

## Test Boundary

The synthetic cases keep these production boundaries real:

- `ThreadingHTTPServer` and the rollout request handlers;
- async request validation and idempotent admission;
- the SQLite WAL registry and startup reconciliation;
- the per-job `pending/active/results` file queue;
- status aggregation and result delivery through public HTTP APIs.

Only zellij startup and Harbor trial execution are replaced. The deterministic
synthetic worker claims the production file-queue schema and writes the result
artifact consumed by the production result path.

This means the harness can validate control-plane correctness, resource use,
and restart recovery. It does not measure real model inference, Harbor,
reward, trainer throughput, or a production Proxy unless those components are
explicitly included in the run.

## Async Lifecycle Smoke

From the Agent Fleet repository root:

```bash
bash Agents/utils/rl/tests/manual/run_async_lifecycle.sh \
  --case Agents/utils/rl/tests/manual/cases/lifecycle-smoke-32.json \
  --skip-metrics
```

The smoke uses real HTTP, SQLite, and file-queue transitions. It injects:

- a dropped submit response followed by an idempotent retry;
- concurrent duplicate submit attempts using the same `request_id`;
- dropped status responses near 10%, 30%, and 70% completion;
- dropped result responses;
- an Agent Fleet process kill and restart in mixed-terminal state;
- ordinary synthetic trial failures that must still produce terminal results.
- an oversized batch rejection followed by successful normal admissions.

Run the other restart boundaries using the same workload:

```bash
for phase in queued running mixed-terminal terminal; do
  bash Agents/utils/rl/tests/manual/run_async_lifecycle.sh \
    --case Agents/utils/rl/tests/manual/cases/lifecycle-smoke-32.json \
    --restart-phase "$phase" \
    --skip-metrics
done
```

macOS needs `--skip-metrics`; lifecycle correctness still runs, but Linux
`/proc` resource evidence is not collected.

## Target Linux Capacity Cases

Run these sequentially on a target Linux server:

```bash
for scale in 320 640 1280; do
  bash Agents/utils/rl/tests/manual/run_async_lifecycle.sh \
    --case "Agents/utils/rl/tests/manual/cases/lifecycle-${scale}.json" \
    --output-root ~/async-lifecycle-validation
done
```

The checked-in 320/640/1280 values are synthetic capacity levels, not claims
about typical production concurrency. They use execution capacity 60 so queue
handoff, multiple execution waves, bulk status polling, restart reconciliation,
and result delivery are all exercised.

Measure the batch-size tradeoff without editing the case:

```bash
for batch_size in 8 16 32 64; do
  bash Agents/utils/rl/tests/manual/run_async_lifecycle.sh \
    --case Agents/utils/rl/tests/manual/cases/lifecycle-320.json \
    --async-batch-size "$batch_size" \
    --output-root ~/async-lifecycle-validation
done
```

Compare status wire requests, QPS, request latency, and server resource samples.
Do not interpret the synthetic completion rate as Harbor or training throughput.

## Sync vs Async Resource Baseline

The original equal-workload A/B remains available:

```bash
bash Agents/utils/rl/tests/manual/run_case.sh \
  --case Agents/utils/rl/tests/manual/cases/smoke-32.json \
  --skip-metrics
```

For a Linux resource run:

```bash
bash Agents/utils/rl/tests/manual/run_case.sh \
  --case Agents/utils/rl/tests/manual/cases/public-reference-128.json \
  --async-batch-size 32 \
  --client-ramp-seconds 5 \
  --output-root ~/sync-async-control-plane-benchmark
```

`run_case.sh` compares long-lived `/run_trial` requests with async admission
under equal synthetic work. `run_retry_faults.sh` separately compares queue
claim amplification after response loss or duplicate submissions:

```bash
bash Agents/utils/rl/tests/manual/run_retry_faults.sh \
  --rounds 100 \
  --duplicate-clients 8
```

## Real Proxy Boundary

`run_async_lifecycle.sh` starts a direct-path server and therefore cannot claim
Proxy evidence. To validate a real deployment Proxy, first start the real Agent
Fleet server and synthetic or Harbor worker using the normal runbook, then aim
the lifecycle driver at the Proxy URL from a host that shares the configured
dataset and queue paths:

```bash
python3 Agents/utils/rl/tests/manual/load_async_batches.py \
  --case Agents/utils/rl/tests/manual/cases/incident-replay-320.json \
  --base-url "$ROLLOUT_PROXY_URL" \
  --work-dir "$ROLLOUT_TEST_WORK_DIR" \
  --manifest "$ROLLOUT_TEST_WORK_DIR/handles.json" \
  --output "$ROLLOUT_TEST_WORK_DIR/load-summary.json"
```

The Proxy run is valid only when its timeout, logs, 502/504 counts, and Agent
Fleet service configuration are preserved with the result. A 360-420 second
synthetic tail demonstrates that no trial-lifetime HTTP request crosses the
Proxy; it does not replace a later real Harbor end-to-end test.

## Outputs

Each lifecycle run creates:

```text
results/<timestamp>-<case>/
  case.json
  handles.json
  load-summary.json
  summary.json
  report.md
  load.log
  server-0.log
  server-1.log
  worker.log
  worker-audit.jsonl
  metrics-0.jsonl
  metrics-1.jsonl
  work/
    registry.sqlite3
    requests.jsonl
    queue/jobs/.../{pending,active,results}/
```

`summary.json` is the machine-readable evidence. `report.md` is a concise view
of unknown outcomes, lost results, duplicate execution, status request cost,
restart phase, and validation failures. Raw logs and the SQLite/file artifacts
must be retained when a result is used for a decision.

## Passing Criteria

A lifecycle case exits nonzero unless all of these hold:

- every logical submit resolves to one stable batch handle;
- accepted registry records match the configured trials and batches;
- injected response losses recover within the configured objective;
- terminal results remain queryable and no result is lost;
- no trial execution ID, client trial ID, or worker queue claim is duplicated;
- restart occurs at the requested queue boundary;
- status and result requests return no 502/504 on the measured path;
- all enqueue intents are materialized.

Resource leak trends, overload behavior, a four-hour soak, production Proxy
behavior, and real Harbor execution remain separate target-environment gates.
