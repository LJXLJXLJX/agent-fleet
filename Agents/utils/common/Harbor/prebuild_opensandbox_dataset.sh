#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

DATASET_ROOT="${1:?usage: prebuild_opensandbox_dataset.sh DATASET_ROOT BENCHMARK_NAME}"
BENCHMARK_NAME="${2:?usage: prebuild_opensandbox_dataset.sh DATASET_ROOT BENCHMARK_NAME}"
HARBOR_OPENSANDBOX_REGISTRY="${HARBOR_OPENSANDBOX_REGISTRY:-registry.gate.yicloud.com.cn}"
HARBOR_OPENSANDBOX_IMAGE_REPOSITORY="${HARBOR_OPENSANDBOX_IMAGE_REPOSITORY:-fdj-infra/syslab-benchmark-task-images}"
HARBOR_OPENSANDBOX_SANDBOX_IMAGE_PREFIX="${HARBOR_OPENSANDBOX_SANDBOX_IMAGE_PREFIX:-${HARBOR_OPENSANDBOX_IMAGE_REPOSITORY}}"
HARBOR_OPENSANDBOX_DOCKER_CONFIG="${HARBOR_OPENSANDBOX_DOCKER_CONFIG:-${HOME}/.docker/config.json}"
HARBOR_OPENSANDBOX_IMAGE_CACHE_ROOT="${HARBOR_OPENSANDBOX_IMAGE_CACHE_ROOT:-/data/harbor-runs/opensandbox-images}"
HARBOR_OPENSANDBOX_IMAGE_PLATFORM="${HARBOR_OPENSANDBOX_IMAGE_PLATFORM:-linux/amd64}"
HARBOR_OPENSANDBOX_DOCKERHUB_MIRROR_PREFIX="${HARBOR_OPENSANDBOX_DOCKERHUB_MIRROR_PREFIX:-m.daocloud.io/docker.io}"
HARBOR_OPENSANDBOX_APT_MIRROR="${HARBOR_OPENSANDBOX_APT_MIRROR:-http://mirrors.tuna.tsinghua.edu.cn}"
HARBOR_OPENSANDBOX_BUILD_ARGS_JSON="${HARBOR_OPENSANDBOX_BUILD_ARGS_JSON:-}"
if [[ -z "${HARBOR_OPENSANDBOX_BUILD_ARGS_JSON}" ]]; then
  HARBOR_OPENSANDBOX_BUILD_ARGS_JSON='{}'
fi
HARBOR_OPENSANDBOX_BUILD_USE_PROXY="${HARBOR_OPENSANDBOX_BUILD_USE_PROXY:-0}"
HARBOR_OPENSANDBOX_PREBUILD_CONCURRENCY="${HARBOR_OPENSANDBOX_PREBUILD_CONCURRENCY:-1}"
HARBOR_OPENSANDBOX_DRY_RUN="${HARBOR_OPENSANDBOX_DRY_RUN:-0}"
HARBOR_OPENSANDBOX_PREBUILD_ROOT="${HARBOR_OPENSANDBOX_PREBUILD_ROOT:-/data/harbor-runs/opensandbox-prebuild}"
HARBOR_OPENSANDBOX_IMAGE_MANAGER="${HARBOR_OPENSANDBOX_IMAGE_MANAGER:-${SCRIPT_DIR}/opensandbox_image_manager.py}"
HARBOR_OPENSANDBOX_MANAGER_PYTHON="${HARBOR_OPENSANDBOX_MANAGER_PYTHON:-${HARBOR_OPIK_PYTHON:-python3}}"

color_enabled() {
  [[ -z "${NO_COLOR:-}" && "${TERM:-dumb}" != dumb && -t "$1" ]]
}

print_error() {
  if color_enabled 2; then
    printf '\033[31m%s\033[0m\n' "$*" >&2
  else
    printf '%s\n' "$*" >&2
  fi
}

print_warning() {
  if color_enabled 2; then
    printf '\033[33m%s\033[0m\n' "$*" >&2
  else
    printf '%s\n' "$*" >&2
  fi
}

colorize_prebuild_output() {
  local line
  local use_color=0
  color_enabled 1 && use_color=1
  while IFS= read -r line || [[ -n "${line}" ]]; do
    if [[ "${use_color}" == 1 \
      && ("${line}" == *"[ERROR]"* || "${line}" == *"[failed]"*) ]]; then
      printf '\033[31m%s\033[0m\n' "${line}"
    elif [[ "${use_color}" == 1 \
      && ("${line}" == *"[WARN]"* || "${line}" == *"[warning]"*) ]]; then
      printf '\033[33m%s\033[0m\n' "${line}"
    else
      printf '%s\n' "${line}"
    fi
  done
}

case "${HARBOR_OPENSANDBOX_PREBUILD_CONCURRENCY}" in
  ''|*[!0-9]*|0)
    print_error "[ERROR] HARBOR_OPENSANDBOX_PREBUILD_CONCURRENCY must be positive"
    exit 1
    ;;
esac
case "${HARBOR_OPENSANDBOX_BUILD_USE_PROXY}" in
  0|1) ;;
  *) print_error "[ERROR] HARBOR_OPENSANDBOX_BUILD_USE_PROXY must be 0 or 1"; exit 1 ;;
esac
case "${HARBOR_OPENSANDBOX_DRY_RUN}" in
  0|1) ;;
  *) print_error "[ERROR] HARBOR_OPENSANDBOX_DRY_RUN must be 0 or 1"; exit 1 ;;
esac

DATASET_ROOT="$(cd "${DATASET_ROOT}" && pwd)"
[[ -f "${HARBOR_OPENSANDBOX_IMAGE_MANAGER}" ]] || {
  print_error "[ERROR] image manager not found: ${HARBOR_OPENSANDBOX_IMAGE_MANAGER}"
  exit 1
}
[[ -f "${HARBOR_OPENSANDBOX_DOCKER_CONFIG}" || "${HARBOR_OPENSANDBOX_DRY_RUN}" == 1 ]] || {
  print_error "[ERROR] Docker config not found: ${HARBOR_OPENSANDBOX_DOCKER_CONFIG}"
  exit 1
}
command -v "${HARBOR_OPENSANDBOX_MANAGER_PYTHON}" >/dev/null 2>&1 || {
  print_error "[ERROR] Python not found: ${HARBOR_OPENSANDBOX_MANAGER_PYTHON}"
  exit 1
}
if [[ "${HARBOR_OPENSANDBOX_DRY_RUN}" != 1 ]] \
  && ! docker buildx version >/dev/null 2>&1; then
  print_error "[ERROR] docker buildx is required to prebuild benchmark task images."
  print_error "[ERROR] Install Docker Buildx, then verify: docker buildx version"
  exit 1
fi
if [[ "${HARBOR_OPENSANDBOX_DRY_RUN}" != 1 \
  && "${HARBOR_OPENSANDBOX_BUILD_USE_PROXY}" == 1 ]]; then
  for proxy_name in HTTP_PROXY HTTPS_PROXY http_proxy https_proxy; do
    proxy_value="${!proxy_name:-}"
    case "${proxy_value}" in
      *://127.0.0.1:*|*://127.0.0.1|*://localhost:*|*://localhost|*://\[::1\]:*|*://\[::1\])
        print_error \
          "[ERROR] ${proxy_name} is a loopback proxy and is unreachable from BuildKit containers."
        print_error \
          "[ERROR] Disable HARBOR_OPENSANDBOX_BUILD_USE_PROXY or use a container-reachable proxy address."
        exit 1
        ;;
    esac
  done
fi

run_stamp="$(date -u +%Y%m%dT%H%M%SZ)"
run_dir="${HARBOR_OPENSANDBOX_PREBUILD_ROOT}/${BENCHMARK_NAME}-${run_stamp}"
supported_nul="${run_dir}/supported.nul"
supported_txt="${run_dir}/supported.txt"
skipped_txt="${run_dir}/skipped.txt"
run_log="${run_dir}/prebuild.log"
mkdir -p "${run_dir}"
: > "${supported_nul}"
: > "${supported_txt}"
: > "${skipped_txt}"

for task_dir in "${DATASET_ROOT}"/*; do
  [[ -d "${task_dir}" ]] || continue
  task_name="$(basename "${task_dir}")"
  if [[ ! -f "${task_dir}/task.toml" ]]; then
    printf '%s\tmissing-task.toml\n' "${task_name}" >> "${skipped_txt}"
  elif [[ -f "${task_dir}/environment/docker-compose.yml" \
    || -f "${task_dir}/environment/docker-compose.yaml" ]]; then
    printf '%s\tcompose-not-supported\n' "${task_name}" >> "${skipped_txt}"
  elif [[ ! -f "${task_dir}/environment/Dockerfile" ]]; then
    printf '%s\tmissing-Dockerfile\n' "${task_name}" >> "${skipped_txt}"
  else
    printf '%s\0' "${task_dir}" >> "${supported_nul}"
    printf '%s\n' "${task_name}" >> "${supported_txt}"
  fi
done

supported_count="$(wc -l < "${supported_txt}" | tr -d ' ')"
skipped_count="$(wc -l < "${skipped_txt}" | tr -d ' ')"
printf '[prebuild] benchmark=%s supported=%s skipped=%s concurrency=%s dry_run=%s\n' \
  "${BENCHMARK_NAME}" "${supported_count}" "${skipped_count}" \
  "${HARBOR_OPENSANDBOX_PREBUILD_CONCURRENCY}" "${HARBOR_OPENSANDBOX_DRY_RUN}"
printf '[prebuild] repository=%s/%s\n' \
  "${HARBOR_OPENSANDBOX_REGISTRY}" "${HARBOR_OPENSANDBOX_IMAGE_REPOSITORY}"
printf '[prebuild] run_dir=%s\n' "${run_dir}"
if [[ "${skipped_count}" != 0 ]]; then
  print_warning \
    "[WARN] skipped ${skipped_count} unsupported tasks; details=${skipped_txt}"
fi

export BENCHMARK_NAME
export HARBOR_OPENSANDBOX_REGISTRY
export HARBOR_OPENSANDBOX_IMAGE_REPOSITORY
export HARBOR_OPENSANDBOX_SANDBOX_IMAGE_PREFIX
export HARBOR_OPENSANDBOX_DOCKER_CONFIG
export HARBOR_OPENSANDBOX_IMAGE_CACHE_ROOT
export HARBOR_OPENSANDBOX_IMAGE_PLATFORM
export HARBOR_OPENSANDBOX_DOCKERHUB_MIRROR_PREFIX
export HARBOR_OPENSANDBOX_APT_MIRROR
export HARBOR_OPENSANDBOX_BUILD_ARGS_JSON
export HARBOR_OPENSANDBOX_BUILD_USE_PROXY
export HARBOR_OPENSANDBOX_DRY_RUN
export HARBOR_OPENSANDBOX_IMAGE_MANAGER
export HARBOR_OPENSANDBOX_MANAGER_PYTHON

set +e
xargs -0 -r -P "${HARBOR_OPENSANDBOX_PREBUILD_CONCURRENCY}" -n 1 \
  bash -c '
    task_dir="$1"
    task_name="$(basename "${task_dir}")"
    command=(
      "${HARBOR_OPENSANDBOX_MANAGER_PYTHON}"
      "${HARBOR_OPENSANDBOX_IMAGE_MANAGER}"
      --task-dir "${task_dir}"
      --registry "${HARBOR_OPENSANDBOX_REGISTRY}"
      --repository "${HARBOR_OPENSANDBOX_IMAGE_REPOSITORY}"
      --sandbox-image-prefix "${HARBOR_OPENSANDBOX_SANDBOX_IMAGE_PREFIX}"
      --docker-config "${HARBOR_OPENSANDBOX_DOCKER_CONFIG}"
      --cache-root "${HARBOR_OPENSANDBOX_IMAGE_CACHE_ROOT}"
      --platform "${HARBOR_OPENSANDBOX_IMAGE_PLATFORM}"
      --tag-prefix "${BENCHMARK_NAME}"
      --dockerhub-mirror-prefix "${HARBOR_OPENSANDBOX_DOCKERHUB_MIRROR_PREFIX}"
      --apt-mirror "${HARBOR_OPENSANDBOX_APT_MIRROR}"
      --build-args-json "${HARBOR_OPENSANDBOX_BUILD_ARGS_JSON}"
    )
    [[ "${HARBOR_OPENSANDBOX_BUILD_USE_PROXY}" == 1 ]] && command+=(--use-proxy)
    [[ "${HARBOR_OPENSANDBOX_DRY_RUN}" == 1 ]] && command+=(--dry-run)
    if image_ref="$("${command[@]}")"; then
      printf "[prebuild][ready] task=%s image_ref=%s\n" "${task_name}" "${image_ref}"
    else
      printf "[prebuild][failed] task=%s\n" "${task_name}" >&2
      exit 1
    fi
  ' _ < "${supported_nul}" 2>&1 | tee "${run_log}" | colorize_prebuild_output
xargs_status="${PIPESTATUS[0]}"
set -e

if [[ "${xargs_status}" != 0 ]]; then
  print_error "[ERROR] one or more task images failed; rerun the same command to resume"
  print_error "[ERROR] log=${run_log}"
  exit "${xargs_status}"
fi

printf '[prebuild] complete; log=%s skipped=%s\n' "${run_log}" "${skipped_txt}"
