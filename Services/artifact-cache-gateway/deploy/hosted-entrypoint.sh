#!/bin/sh
set -eu

runtime_dir="${ARTIFACT_CACHE_RUNTIME_CONFIG_DIR:-/etc/artifact-cache-gateway}"
env_file="${ARTIFACT_CACHE_ENV_FILE:-${runtime_dir}/config.local.env}"
mihomo_source="${MIHOMO_CONFIG_FILE:-${runtime_dir}/mihomo.yaml}"
git_plan_source="${ARTIFACT_CACHE_GIT_MIRROR_PLAN:-${runtime_dir}/git-mirror-plan.json}"
download_plan_dir="${ARTIFACT_CACHE_DOWNLOAD_PLAN_DIR:-${runtime_dir}/download-plans}"

if [ ! -r "${env_file}" ]; then
  echo "ERROR: runtime environment file is not readable: ${env_file}" >&2
  exit 1
fi
if [ ! -r "${mihomo_source}" ]; then
  echo "ERROR: mihomo configuration is not readable: ${mihomo_source}" >&2
  exit 1
fi

set -a
# shellcheck disable=SC1090
. "${env_file}"
set +a

export ARTIFACT_CACHE_STORAGE="${ARTIFACT_CACHE_STORAGE:-s3}"
export ARTIFACT_CACHE_DIR="${ARTIFACT_CACHE_DIR:-/data/artifact-cache}"
export ARTIFACT_CACHE_UPSTREAM_PROXY="${ARTIFACT_CACHE_UPSTREAM_PROXY:-http://127.0.0.1:7890}"
export ARTIFACT_CACHE_GIT_MIRROR_ROOT="${ARTIFACT_CACHE_GIT_MIRROR_ROOT:-/data/artifact-cache/github_mirrors}"
gateway_source="${ARTIFACT_CACHE_CONFIG:-${runtime_dir}/sources.json}"

for name in \
  ARTIFACT_CACHE_S3_ENDPOINT \
  ARTIFACT_CACHE_S3_REGION \
  ARTIFACT_CACHE_S3_BUCKET \
  ARTIFACT_CACHE_S3_PREFIX \
  ARTIFACT_CACHE_S3_ACCESS_KEY_ID \
  ARTIFACT_CACHE_S3_SECRET_ACCESS_KEY
do
  eval "value=\${${name}:-}"
  if [ -z "${value}" ]; then
    echo "ERROR: runtime environment is missing ${name}" >&2
    exit 1
  fi
done

if [ ! -r "${gateway_source}" ]; then
  echo "ERROR: Gateway source configuration is not readable: ${gateway_source}" >&2
  exit 1
fi

umask 077
mkdir -p /tmp/artifact-cache-hosted /tmp/mihomo-home "${ARTIFACT_CACHE_DIR}/tmp"
cp "${mihomo_source}" /tmp/artifact-cache-hosted/mihomo.yaml
cp "${gateway_source}" /tmp/artifact-cache-hosted/sources.json
export ARTIFACT_CACHE_CONFIG=/tmp/artifact-cache-hosted/sources.json
if [ -r "${git_plan_source}" ]; then
  cp "${git_plan_source}" /tmp/artifact-cache-hosted/git-mirror-plan.json
  export ARTIFACT_CACHE_GIT_MIRROR_PLAN=/tmp/artifact-cache-hosted/git-mirror-plan.json
else
  unset ARTIFACT_CACHE_GIT_MIRROR_PLAN
fi
if [ -d "${download_plan_dir}" ]; then
  export ARTIFACT_CACHE_DOWNLOAD_PLAN_DIR="${download_plan_dir}"
else
  unset ARTIFACT_CACHE_DOWNLOAD_PLAN_DIR
fi
mkdir -p "${ARTIFACT_CACHE_GIT_MIRROR_ROOT}"
if [ "$(id -u)" -eq 0 ]; then
  chown -R \
    "${ARTIFACT_CACHE_RUNTIME_UID:-10250}:${ARTIFACT_CACHE_RUNTIME_GID:-10250}" \
    /tmp/artifact-cache-hosted \
    /tmp/mihomo-home \
    "${ARTIFACT_CACHE_DIR}"
fi

exec python /app/deploy/hosted_supervisor.py \
  --mihomo-config /tmp/artifact-cache-hosted/mihomo.yaml
