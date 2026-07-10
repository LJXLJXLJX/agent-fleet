# REMOVE BEFORE PR: Sync vs Async Benchmark

This directory is a temporary decision harness. It intentionally lives inside
the repository while the async control-plane design is being evaluated, but it
is not intended to ship in the Agent Fleet pull request.

Delete the whole directory before preparing the PR:

```text
Agents/utils/rl/tests/REMOVE_BEFORE_PR__SYNC_ASYNC_BENCHMARK/
```

## What It Measures

The harness compares two paths on one machine:

```text
sync:  one POST /run_trial connection per logical trial
async: POST /async_trial_batches with configurable control-plane batch size
```

Both paths use:

- the real `ThreadingHTTPServer` and request handlers;
- the real SQLite registry for the async path;
- the real per-job `pending/active/results` file queue;
- the same deterministic synthetic execution capacity and delay.

Only zellij and real Harbor execution are replaced. The synthetic worker claims
the existing file queue and writes the same result-file boundary used by the
rollout server.

## One-Server Scope

A single target Linux server is sufficient for this decision benchmark. The
load generator, Agent Fleet server, synthetic worker, and collector run as
separate processes on that host. Metrics are collected for the server process,
so client threads are not counted as handler threads, although all processes do
compete for the same host CPU and memory. Sync and async runs are executed
sequentially under the same conditions.

For publishable resource numbers, run on the target Agent Fleet Linux host or a
machine with the same CPU, memory, cgroup, `ulimit`, and local filesystem. A
developer laptop run only validates the harness.

## Quick Start

From the Agent Fleet repository root:

```bash
bash Agents/utils/rl/tests/REMOVE_BEFORE_PR__SYNC_ASYNC_BENCHMARK/run_case.sh \
  --case Agents/utils/rl/tests/REMOVE_BEFORE_PR__SYNC_ASYNC_BENCHMARK/cases/smoke-32.json
```

The Linux decision cases require `/proc`. On macOS, add `--skip-metrics` to
validate request, registry, queue, and duplicate accounting without claiming
resource results.

Run the idempotency comparison separately. Use a small round count to validate
the harness, then the decision gate requires 100:

```bash
bash Agents/utils/rl/tests/REMOVE_BEFORE_PR__SYNC_ASYNC_BENCHMARK/run_retry_faults.sh \
  --rounds 100 \
  --duplicate-clients 8
```

Override the case's async control-plane batch size without editing the checked-in
case. Run separate invocations for 16, 32, and 64 so each result directory is
self-contained:

```bash
bash Agents/utils/rl/tests/REMOVE_BEFORE_PR__SYNC_ASYNC_BENCHMARK/run_case.sh \
  --case Agents/utils/rl/tests/REMOVE_BEFORE_PR__SYNC_ASYNC_BENCHMARK/cases/public-reference-256.json \
  --async-batch-size 64
```

Cases use a simultaneous client burst by default. If that intentionally saturates
the production-equivalent server listen backlog, rerun the same case with a
bounded ramp to measure steady-state long-connection cost separately:

```bash
bash Agents/utils/rl/tests/REMOVE_BEFORE_PR__SYNC_ASYNC_BENCHMARK/run_case.sh \
  --case Agents/utils/rl/tests/REMOVE_BEFORE_PR__SYNC_ASYNC_BENCHMARK/cases/public-reference-128.json \
  --async-batch-size 32 \
  --client-ramp-seconds 5
```

The burst and ramped runs answer different questions and must be reported
separately. The runner preserves the real `ThreadingHTTPServer` listen backlog;
it does not enlarge the benchmark server backlog to force the sync case to pass.

For the target-server decision matrix, let the harness run cases and repetitions
sequentially. This avoids overlapping sync and async measurements:

```bash
bash Agents/utils/rl/tests/REMOVE_BEFORE_PR__SYNC_ASYNC_BENCHMARK/run_matrix.sh \
  --case Agents/utils/rl/tests/REMOVE_BEFORE_PR__SYNC_ASYNC_BENCHMARK/cases/public-reference-128.json \
  --case Agents/utils/rl/tests/REMOVE_BEFORE_PR__SYNC_ASYNC_BENCHMARK/cases/public-reference-256.json \
  --case Agents/utils/rl/tests/REMOVE_BEFORE_PR__SYNC_ASYNC_BENCHMARK/cases/public-reference-512.json \
  --batch-sizes 16,32,64 \
  --repetitions 3
```

The matrix writes `matrix.json` after every run and stops on the first failure,
so an interrupted or failed multi-hour run retains its completed-run manifest.

Reference cases:

```text
cases/smoke-32.json
cases/public-reference-128.json
cases/public-reference-256.json
cases/public-reference-512.json
cases/incident-replay-320.json
cases/capacity-envelope-1024.json
```

Before running 512 or 1024 simultaneous sync connections, check the host limit:

```bash
ulimit -n
```

Use at least 4096 open files for the 1024-trial structural case. Do not silently
raise limits between sync and async runs.

## Output

Each invocation creates:

```text
results/<timestamp>-<case>/
  case.json
  summary.json
  report.md
  sync/
    server.log
    worker.log
    worker-audit.jsonl
    metrics.jsonl
    summary.json
  async/
    ...
```

A matrix invocation creates one outer `results/<timestamp>-matrix-*/` directory,
with `matrix.json` plus one normal result directory per matrix cell.

`summary.json` contains request latency, accepted/error counts, server thread,
FD, process-owned TCP socket, established TCP, RSS and CPU measurements,
established connection-seconds, queue/result counts, registry counts, synthetic
throughput, and duplicate execution counts. Keep the raw JSONL files when
reporting a conclusion.

`report.md` is the concise human-readable view: configuration, sync/async table,
resource ratios, integrity checks, and interpretation limits. `summary.json`
remains the machine-readable evidence and includes individual request records.
If one mode accepts only part of the workload, the runner records a failed
partial report, prints progress, and continues with the other mode so the
failure still has an A/B result.

## Interpretation Limits

- Public framework batch values are sample-count references, not proven HTTP
  concurrency norms.
- The 320 case only replays the shape of the observed incident.
- This step validates admission and execution handoff, not status, result
  routing, bounded wait, or automatic restart recovery.
- A response-loss/retry result is only meaningful when the audit log is used to
  count actual claims, not merely final result filenames.
- Case files named `public-reference-*` use a fixed delay and are structural
  sample-count references. Replace `real-profile-template.json` with measured
  concurrency and duration data before describing a run as production-shaped.

This temporary harness currently measures only the early continuation gate:

```text
connection/FD/thread/RSS cost
submit latency and synthetic completion throughput
duplicate admission or queue claims caused by client retry
```

It does **not** measure final `unknown_outcome_rate`, `lost_result_rate`, rolling
deploy survival, or trainer `failure_recovery_seconds`. Those metrics require
the later status, result-manifest, restart-recovery, and Miles/Polar coordinator
steps. In particular, the response-drop probe proves durable admission and
deduplicated retry only; it does not prove that a restarted client can query and
recover the final outcome. Its `duplicate_queue_claim_rate` and
`queue_claim_amplification` count synthetic execution starts caused by client
retry; they are not worker-recovery attempt metrics.
