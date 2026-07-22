#!/usr/bin/env bash
set -euo pipefail

HARBOR_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MANAGER_PYTHON="${HARBOR_OPIK_PYTHON:-$(command -v python3)}"
tmp="$(mktemp -d)"
trap 'rm -rf -- "$tmp"' EXIT

mkdir -p "$tmp/bin" "$tmp/dataset/0/environment" "$tmp/home"
printf '#!/usr/bin/env bash\nexit 0\n' > "$tmp/bin/uv"
chmod +x "$tmp/bin/uv"
printf '[environment]\nbuild_timeout_sec = 60\n' > "$tmp/dataset/0/task.toml"
printf 'FROM ubuntu:24.04\n' > "$tmp/dataset/0/environment/Dockerfile"

run_dry() {
  local image_ref="$1"
  local manager="$2"
  local build_args_json="${3:-}"
  local dataset_name="${4:-auto}"
  if [[ -z "$build_args_json" ]]; then
    build_args_json='{}'
  fi
  env -i \
    PATH="$tmp/bin:/usr/bin:/bin" \
    HOME="$tmp/home" \
    AGENT=oracle \
    DATASET_NAME="$dataset_name" \
    DATASET_PATH="$tmp/dataset" \
    INCLUDE_TASKS=0 \
    OUTPUT_PATH="$tmp/output" \
    TB_DRY_RUN=1 \
    TB_N_CONCURRENT=1 \
    TB_MAX_RETRIES=0 \
    TB_ENVIRONMENT_TYPE=opensandbox \
    HARBOR_OPENSANDBOX_IMAGE_REF="$image_ref" \
    HARBOR_OPENSANDBOX_IMAGE_REPOSITORY=fdj-infra/test-repository \
    HARBOR_OPENSANDBOX_IMAGE_MANAGER="$manager" \
    HARBOR_OPIK_PYTHON="$MANAGER_PYTHON" \
    HARBOR_OPENSANDBOX_BUILD_ARGS_JSON="$build_args_json" \
    YICLOUD_PUBLIC_KEY=fake-public \
    YICLOUD_SECRET_KEY=fake-secret \
    YICLOUD_PROJECT_NAME=fdj-infra \
    YICLOUD_SANDBOX_ENVIRONMENT_ID=env-test \
    YICLOUD_SANDBOX_ENVIRONMENT_NAME=test-environment \
    bash "$HARBOR_DIR/harboropik.sh" 2>&1
}

automatic="$(run_dry '' "$HARBOR_DIR/opensandbox_image_manager.py" '{}')"
grep -F -- '--env yicloud_opensandbox:YiCloudOpenSandboxEnvironment' <<< "$automatic" >/dev/null
grep -E -- '--ek image_ref=fdj-infra/test-repository:harbor-0-[0-9a-f]{20}' \
  <<< "$automatic" >/dev/null
grep -F -- '--ek lifecycle_minutes=120' <<< "$automatic" >/dev/null
if grep -F -- '--extra-docker-compose' <<< "$automatic" >/dev/null; then
  echo 'OpenSandbox command unexpectedly contains a Docker compose overlay' >&2
  exit 1
fi

automatic_seta="$(run_dry '' "$HARBOR_DIR/opensandbox_image_manager.py" '{}' seta)"
grep -E -- '--ek image_ref=fdj-infra/test-repository:harbor-0-[0-9a-f]{20}' \
  <<< "$automatic_seta" >/dev/null
grep -F -- "--path $tmp/dataset" <<< "$automatic_seta" >/dev/null
if grep -F -- '--dataset seta-env' <<< "$automatic_seta" >/dev/null; then
  echo 'OpenSandbox local SETA path unexpectedly resolved through Harbor Registry' >&2
  exit 1
fi

manual="$(run_dry 'fdj-infra/manual:immutable' "$tmp/does-not-exist.py")"
grep -F -- '--ek image_ref=fdj-infra/manual:immutable' <<< "$manual" >/dev/null
if grep -F -- '[INFO] preparing OpenSandbox image' <<< "$manual" >/dev/null; then
  echo 'manual image override unexpectedly invoked the image manager' >&2
  exit 1
fi
