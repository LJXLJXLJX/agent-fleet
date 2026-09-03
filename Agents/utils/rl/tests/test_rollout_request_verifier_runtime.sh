#!/usr/bin/env bash
set -euo pipefail

RL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
worker="$RL_DIR/run_rl_rollout_worker.sh"
tmp="$(mktemp -d)"
trap 'rm -rf -- "$tmp"' EXIT

eval "$(sed -n '/^prepare_request_verifier_runtime_bundle()/,/^}/p' "$worker")"

select_verifier_runtime_bundle() {
  printf '%s\n' "test-bundle"
}

resolve_verifier_runtime_bundle() {
  printf '%s\n' "$1" >> "$tmp/resolved"
}

harbor_prepare_verifier_runtime_bundle() {
  printf '%s\n' "prepared" >> "$tmp/prepared"
}

WORKER_STARTUP_ENVIRONMENT_TYPE=docker
HARBOR_ENVIRONMENT_TYPE=opensandbox
prepare_request_verifier_runtime_bundle
grep -Fx -- 'test-bundle' "$tmp/resolved" >/dev/null
grep -Fx -- 'prepared' "$tmp/prepared" >/dev/null

: > "$tmp/resolved"
: > "$tmp/prepared"
WORKER_STARTUP_ENVIRONMENT_TYPE=opensandbox
HARBOR_ENVIRONMENT_TYPE=opensandbox
prepare_request_verifier_runtime_bundle
[[ ! -s "$tmp/resolved" ]]
[[ ! -s "$tmp/prepared" ]]

WORKER_STARTUP_ENVIRONMENT_TYPE=docker
HARBOR_ENVIRONMENT_TYPE=docker
prepare_request_verifier_runtime_bundle
[[ ! -s "$tmp/resolved" ]]
[[ ! -s "$tmp/prepared" ]]

echo 'rollout request verifier runtime test passed'
