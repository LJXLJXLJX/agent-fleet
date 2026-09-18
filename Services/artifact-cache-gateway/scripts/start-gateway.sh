#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
LOCAL_ENV="$REPO_ROOT/config.local.env"
BUILD=0

usage() {
  cat <<'EOF'
Usage: scripts/start-gateway.sh [--build]

Start Artifact Cache Gateway from the repository's config.local.env.

By default the script reuses artifact-cache-gateway:local. It builds only when
that image is missing. Pass --build after changing source code or Dockerfile.
The container is recreated on every invocation so config.local.env changes are
applied without deleting the S3 cache or artifact-cache-work volume.
EOF
}

while (($#)); do
  case "$1" in
    --build)
      BUILD=1
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "ERROR: unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
  shift
done

if [[ ! -f "$LOCAL_ENV" ]]; then
  echo "ERROR: missing $LOCAL_ENV" >&2
  exit 1
fi
if [[ ! -f "$REPO_ROOT/config/sources.json" ]]; then
  echo "ERROR: missing $REPO_ROOT/config/sources.json" >&2
  exit 1
fi
if ! command -v docker >/dev/null 2>&1; then
  echo "ERROR: docker is not installed or not on PATH" >&2
  exit 1
fi
if ! command -v curl >/dev/null 2>&1; then
  echo "ERROR: curl is not installed or not on PATH" >&2
  exit 1
fi

# config.local.env is a shell-format, Git-ignored file and may contain export
# statements, so Docker's --env-file parser cannot consume it directly.
set -a
# shellcheck disable=SC1090
source "$LOCAL_ENV"
set +a

required_vars=(
  ARTIFACT_CACHE_S3_ENDPOINT
  ARTIFACT_CACHE_S3_REGION
  ARTIFACT_CACHE_S3_BUCKET
  ARTIFACT_CACHE_S3_PREFIX
  ARTIFACT_CACHE_S3_ACCESS_KEY_ID
  ARTIFACT_CACHE_S3_SECRET_ACCESS_KEY
)
for name in "${required_vars[@]}"; do
  if [[ -z "${!name:-}" ]]; then
    echo "ERROR: config.local.env is missing $name" >&2
    exit 1
  fi
done

IMAGE="${ARTIFACT_CACHE_GATEWAY_IMAGE:-artifact-cache-gateway:local}"
CONTAINER="${ARTIFACT_CACHE_GATEWAY_CONTAINER:-artifact-cache-gateway}"
BASE_IMAGE="${ARTIFACT_CACHE_GATEWAY_BASE_IMAGE:-m.daocloud.io/docker.io/library/python:3.10-slim}"
if [[ ! -v ARTIFACT_CACHE_UPSTREAM_PROXY ]]; then
  export ARTIFACT_CACHE_UPSTREAM_PROXY="http://127.0.0.1:7890"
fi
export ARTIFACT_CACHE_CACHE_MODE="${ARTIFACT_CACHE_CACHE_MODE:-proxy-only}"
if [[ "$ARTIFACT_CACHE_CACHE_MODE" != "proxy-only" && "$ARTIFACT_CACHE_CACHE_MODE" != "all" ]]; then
  echo "ERROR: ARTIFACT_CACHE_CACHE_MODE must be proxy-only or all" >&2
  exit 1
fi

if ((BUILD)) || ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  if ((BUILD)); then
    echo "[1/3] Rebuilding $IMAGE from the domestic base-image mirror..."
  else
    echo "[1/3] $IMAGE is missing; building it once..."
  fi
  docker build \
    --build-arg "BASE_IMAGE=$BASE_IMAGE" \
    --tag "$IMAGE" \
    "$REPO_ROOT"
else
  echo "[1/3] Reusing existing image $IMAGE (no build)."
fi

echo "[2/3] Recreating $CONTAINER to reload config.local.env..."
if docker container inspect "$CONTAINER" >/dev/null 2>&1; then
  docker stop --timeout 20 "$CONTAINER" >/dev/null 2>&1 || true
  docker rm "$CONTAINER" >/dev/null
fi

docker run -d \
  --name "$CONTAINER" \
  --network host \
  --restart unless-stopped \
  --env ARTIFACT_CACHE_S3_ENDPOINT \
  --env ARTIFACT_CACHE_S3_REGION \
  --env ARTIFACT_CACHE_S3_BUCKET \
  --env ARTIFACT_CACHE_S3_PREFIX \
  --env ARTIFACT_CACHE_S3_ACCESS_KEY_ID \
  --env ARTIFACT_CACHE_S3_SECRET_ACCESS_KEY \
  --env ARTIFACT_CACHE_S3_SESSION_TOKEN \
  --env ARTIFACT_CACHE_UPSTREAM_PROXY \
  --env ARTIFACT_CACHE_CACHE_MODE \
  --env ARTIFACT_CACHE_STORAGE=s3 \
  --env ARTIFACT_CACHE_CONFIG=/app/config/sources.json \
  --volume artifact-cache-work:/data/artifact-cache \
  --volume "$REPO_ROOT/config/sources.json:/app/config/sources.json:ro" \
  "$IMAGE" \
  artifact-cache-gateway \
    --host 127.0.0.1 \
    --port 8080 \
    --config /app/config/sources.json \
  >/dev/null

echo "[3/3] Waiting for http://127.0.0.1:8080/healthz ..."
for _ in $(seq 1 30); do
  if curl --noproxy '*' -fsS \
    http://127.0.0.1:8080/healthz >/dev/null 2>&1; then
    echo "Gateway is healthy."
    echo "Web UI:   http://127.0.0.1:8080/ui/"
    echo "Status:   http://127.0.0.1:8080/v1/status"
    echo "APT root: http://127.0.0.1:8080/v1/cache"
    exit 0
  fi
  sleep 1
done

echo "ERROR: Gateway did not become healthy within 30 seconds" >&2
docker logs --tail 100 "$CONTAINER" >&2
exit 1
