#!/usr/bin/env bash
set -euo pipefail

RL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
worker="$RL_DIR/run_rl_rollout_worker.sh"
tmp="$(mktemp -d)"
trap 'rm -rf -- "$tmp"' EXIT

eval "$(sed -n '/^prepare_request_verifier_runtime_bundle()/,/^}/p' "$worker")"

select_verifier_runtime_bundle() {
  case "$HARBOR_VERIFIER_BENCHMARK" in
    agent-fleet-swe-rebench-v2) printf '%s\n' "test-bundle" ;;
    *) printf '%s\n' "none" ;;
  esac
}

resolve_verifier_runtime_bundle() {
  printf '%s\n' "$1" >> "$tmp/resolved"
}

harbor_prepare_verifier_runtime_bundle() {
  printf '%s\n' "prepared" >> "$tmp/prepared"
}

HARBOR_ENVIRONMENT_TYPE=opensandbox
HARBOR_OPENSANDBOX_BENCHMARK=prebuilt-registry-alias
prepare_request_verifier_runtime_bundle agent-fleet-swe-rebench-v2
grep -Fx -- 'test-bundle' "$tmp/resolved" >/dev/null
grep -Fx -- 'prepared' "$tmp/prepared" >/dev/null
[[ "$HARBOR_VERIFIER_BENCHMARK" == "agent-fleet-swe-rebench-v2" ]]
[[ "$HARBOR_OPENSANDBOX_BENCHMARK" == "prebuilt-registry-alias" ]]

: > "$tmp/resolved"
: > "$tmp/prepared"
HARBOR_ENVIRONMENT_TYPE=opensandbox
prepare_request_verifier_runtime_bundle other-dataset
grep -Fx -- 'none' "$tmp/resolved" >/dev/null
grep -Fx -- 'prepared' "$tmp/prepared" >/dev/null
[[ "$HARBOR_VERIFIER_BENCHMARK" == "other-dataset" ]]
[[ "$HARBOR_OPENSANDBOX_BENCHMARK" == "prebuilt-registry-alias" ]]

: > "$tmp/resolved"
: > "$tmp/prepared"
HARBOR_ENVIRONMENT_TYPE=docker
prepare_request_verifier_runtime_bundle agent-fleet-swe-rebench-v2
grep -Fx -- 'test-bundle' "$tmp/resolved" >/dev/null
grep -Fx -- 'prepared' "$tmp/prepared" >/dev/null

grep -F 'dataset_name="$(json_get "$request_file" dataset_name)"' "$worker" >/dev/null
grep -F 'prepare_request_verifier_runtime_bundle "$dataset_name"' "$worker" >/dev/null

echo 'rollout request verifier runtime test passed'
