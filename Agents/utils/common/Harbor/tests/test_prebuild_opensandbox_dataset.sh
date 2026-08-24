#!/usr/bin/env bash
set -euo pipefail

HARBOR_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
tmp="$(mktemp -d)"
trap 'rm -rf -- "$tmp"' EXIT

mkdir -p "$tmp/bin" "$tmp/dataset/task/environment" "$tmp/home"
printf '[environment]\nbuild_timeout_sec = 60\n' > "$tmp/dataset/task/task.toml"
printf 'FROM ubuntu:22.04\nRUN apt-get update\n' \
  > "$tmp/dataset/task/environment/Dockerfile"
printf '{}\n' > "$tmp/docker-config.json"

cat > "$tmp/bin/docker" <<'SH'
#!/usr/bin/env bash
[[ "${1:-}" == buildx && "${2:-}" == version ]]
SH
chmod +x "$tmp/bin/docker"

cat > "$tmp/bin/curl" <<'SH'
#!/usr/bin/env bash
for argument in "$@"; do
  case "$argument" in
    *mirrors.tuna.tsinghua.edu.cn/ubuntu/dists/jammy/InRelease) exit 0 ;;
    */healthz) exit 22 ;;
  esac
done
exit 22
SH
chmod +x "$tmp/bin/curl"

cat > "$tmp/bin/fake-manager" <<'SH'
#!/usr/bin/env bash
while (($#)); do
  if [[ "$1" == --apt-mirror ]]; then
    printf '%s\n' "$2" >> "$TEST_APT_MIRROR_LOG"
    shift 2
    continue
  fi
  shift
done
printf 'registry.example/project/task@sha256:%064d\n' 0
SH
chmod +x "$tmp/bin/fake-manager"

cat > "$tmp/bin/runner" <<'SH'
#!/usr/bin/env bash
script="$1"
shift
exec "$script" "$@"
SH
chmod +x "$tmp/bin/runner"

output="$tmp/prebuild.out"
env -i \
  PATH="$tmp/bin:/usr/bin:/bin" \
  HOME="$tmp/home" \
  TEST_APT_MIRROR_LOG="$tmp/apt-mirrors.log" \
  YICLOUD_HARBOR_HOST=registry.example \
  YICLOUD_HARBOR_PROJECT=project \
  HARBOR_OPENSANDBOX_DOCKER_CONFIG="$tmp/docker-config.json" \
  HARBOR_OPENSANDBOX_IMAGE_CACHE_ROOT="$tmp/cache" \
  HARBOR_OPENSANDBOX_PREBUILD_ROOT="$tmp/runs" \
  HARBOR_OPENSANDBOX_IMAGE_MANAGER="$tmp/bin/fake-manager" \
  HARBOR_OPENSANDBOX_MANAGER_PYTHON="$tmp/bin/runner" \
  HARBOR_OPENSANDBOX_APT_MIRROR=http://127.0.0.1:8080/v1/cache \
  HARBOR_OPENSANDBOX_APT_MIRROR_FALLBACKS=https://mirrors.tuna.tsinghua.edu.cn \
  HARBOR_OPENSANDBOX_BUILD_USE_PROXY=0 \
  HARBOR_OPENSANDBOX_BUILD_NETWORK=host \
  HARBOR_OPENSANDBOX_PREBUILD_CONCURRENCY=1 \
  bash "$HARBOR_DIR/prebuild_opensandbox_dataset.sh" \
    "$tmp/dataset" test > "$output" 2>&1

grep -F '[WARN] APT Gateway unavailable; falling back to trusted domestic mirror' \
  "$output" >/dev/null
grep -Fx 'https://mirrors.tuna.tsinghua.edu.cn' "$tmp/apt-mirrors.log" >/dev/null
if grep -F '127.0.0.1:8080/v1/cache' "$tmp/apt-mirrors.log" >/dev/null; then
  echo 'unhealthy Gateway was passed to the OpenSandbox image manager' >&2
  exit 1
fi
grep -F '[prebuild][ready] task=task' "$output" >/dev/null

echo 'prebuild OpenSandbox APT Gateway fallback tests passed'
