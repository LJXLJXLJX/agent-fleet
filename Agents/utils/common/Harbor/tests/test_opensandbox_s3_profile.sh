#!/usr/bin/env bash
set -euo pipefail

HARBOR_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=../opensandbox_s3_profile.sh
source "${HARBOR_DIR}/opensandbox_s3_profile.sh"

TEST_ROOT="$(mktemp -d)"
trap 'rm -rf "${TEST_ROOT}"' EXIT
PROFILE_ROOT="${TEST_ROOT}/.s3-profiles"

make_profile() {
  local name="$1"
  mkdir -p "${PROFILE_ROOT}/${name}"
  : > "${PROFILE_ROOT}/${name}/s3cfg"
  chmod 600 "${PROFILE_ROOT}/${name}/s3cfg"
}

reset_s3_environment() {
  unset YICLOUD_SANDBOX_UPLOAD_BACKEND
  unset YICLOUD_SANDBOX_S3_CONFIG
  unset YICLOUD_SANDBOX_S3_BUCKET
  unset YICLOUD_SANDBOX_S3_READ_ORIGIN
  unset YICLOUD_SANDBOX_S3_PREFIX
}

make_profile ceph
printf '%s\n' \
  'YICLOUD_SANDBOX_S3_BUCKET=bucket-cache' \
  'YICLOUD_SANDBOX_S3_READ_ORIGIN=http://ceph.example/bucket-cache' \
  'YICLOUD_SANDBOX_S3_PREFIX=agent-fleet-upload/v1' \
  > "${PROFILE_ROOT}/ceph/profile.env"
reset_s3_environment
YICLOUD_SANDBOX_S3_PROFILE=ceph
harbor_resolve_opensandbox_s3_profile "${TEST_ROOT}"
[[ "${YICLOUD_SANDBOX_UPLOAD_BACKEND}" == auto ]]
[[ "${YICLOUD_SANDBOX_S3_CONFIG}" == "${PROFILE_ROOT}/ceph/s3cfg" ]]
[[ "${YICLOUD_SANDBOX_S3_BUCKET}" == bucket-cache ]]
[[ "${YICLOUD_SANDBOX_S3_READ_ORIGIN}" == http://ceph.example/bucket-cache ]]
[[ "${YICLOUD_SANDBOX_S3_PREFIX}" == agent-fleet-upload/v1 ]]
# Re-resolution in worker processes must accept the inherited exact values.
harbor_resolve_opensandbox_s3_profile "${TEST_ROOT}"

reset_s3_environment
YICLOUD_SANDBOX_S3_PROFILE=ceph
YICLOUD_SANDBOX_UPLOAD_BACKEND=s3
harbor_resolve_opensandbox_s3_profile "${TEST_ROOT}"
[[ "${YICLOUD_SANDBOX_UPLOAD_BACKEND}" == s3 ]]

reset_s3_environment
YICLOUD_SANDBOX_S3_PROFILE=ceph
YICLOUD_SANDBOX_UPLOAD_BACKEND=auto
harbor_resolve_opensandbox_s3_profile "${TEST_ROOT}"
[[ "${YICLOUD_SANDBOX_UPLOAD_BACKEND}" == auto ]]

reset_s3_environment
YICLOUD_SANDBOX_S3_PROFILE=../ceph
if harbor_resolve_opensandbox_s3_profile "${TEST_ROOT}" 2>/dev/null; then
  echo 'path-traversal profile unexpectedly succeeded' >&2
  exit 1
fi

reset_s3_environment
YICLOUD_SANDBOX_S3_PROFILE=ceph
YICLOUD_SANDBOX_S3_BUCKET=wrong-bucket
if harbor_resolve_opensandbox_s3_profile "${TEST_ROOT}" 2>/dev/null; then
  echo 'mixed profile and legacy config unexpectedly succeeded' >&2
  exit 1
fi

reset_s3_environment
YICLOUD_SANDBOX_S3_PROFILE=ceph
YICLOUD_SANDBOX_UPLOAD_BACKEND=http
if harbor_resolve_opensandbox_s3_profile "${TEST_ROOT}" 2>/dev/null; then
  echo 'profile with HTTP backend unexpectedly succeeded' >&2
  exit 1
fi

make_profile unknown-key
printf '%s\n' \
  'YICLOUD_SANDBOX_S3_BUCKET=bucket-cache' \
  'YICLOUD_SANDBOX_S3_READ_ORIGIN=http://ceph.example/bucket-cache' \
  'YICLOUD_SANDBOX_S3_PREFIX=agent-fleet-upload/v1' \
  'S3_VENDOR=ceph' \
  > "${PROFILE_ROOT}/unknown-key/profile.env"
reset_s3_environment
YICLOUD_SANDBOX_S3_PROFILE=unknown-key
if harbor_resolve_opensandbox_s3_profile "${TEST_ROOT}" 2>/dev/null; then
  echo 'unknown profile key unexpectedly succeeded' >&2
  exit 1
fi

make_profile credentialed-read-origin
printf '%s\n' \
  'YICLOUD_SANDBOX_S3_BUCKET=bucket-cache' \
  'YICLOUD_SANDBOX_S3_READ_ORIGIN=http://access-key@ceph.example/bucket-cache' \
  'YICLOUD_SANDBOX_S3_PREFIX=agent-fleet-upload/v1' \
  > "${PROFILE_ROOT}/credentialed-read-origin/profile.env"
reset_s3_environment
YICLOUD_SANDBOX_S3_PROFILE=credentialed-read-origin
if harbor_resolve_opensandbox_s3_profile "${TEST_ROOT}" 2>/dev/null; then
  echo 'credentialed anonymous read origin unexpectedly succeeded' >&2
  exit 1
fi

make_profile duplicate
printf '%s\n' \
  'YICLOUD_SANDBOX_S3_BUCKET=first' \
  'YICLOUD_SANDBOX_S3_BUCKET=second' \
  'YICLOUD_SANDBOX_S3_READ_ORIGIN=http://ceph.example/bucket-cache' \
  'YICLOUD_SANDBOX_S3_PREFIX=agent-fleet-upload/v1' \
  > "${PROFILE_ROOT}/duplicate/profile.env"
reset_s3_environment
YICLOUD_SANDBOX_S3_PROFILE=duplicate
if harbor_resolve_opensandbox_s3_profile "${TEST_ROOT}" 2>/dev/null; then
  echo 'duplicate profile key unexpectedly succeeded' >&2
  exit 1
fi

make_profile symlinked
printf '%s\n' \
  'YICLOUD_SANDBOX_S3_BUCKET=bucket-cache' \
  'YICLOUD_SANDBOX_S3_READ_ORIGIN=http://ceph.example/bucket-cache' \
  'YICLOUD_SANDBOX_S3_PREFIX=agent-fleet-upload/v1' \
  > "${PROFILE_ROOT}/symlinked/profile.env"
mv "${PROFILE_ROOT}/symlinked/s3cfg" "${TEST_ROOT}/outside-s3cfg"
ln -s "${TEST_ROOT}/outside-s3cfg" "${PROFILE_ROOT}/symlinked/s3cfg"
reset_s3_environment
YICLOUD_SANDBOX_S3_PROFILE=symlinked
if harbor_resolve_opensandbox_s3_profile "${TEST_ROOT}" 2>/dev/null; then
  echo 'symlinked s3cfg unexpectedly succeeded' >&2
  exit 1
fi

reset_s3_environment
YICLOUD_SANDBOX_S3_PROFILE=
harbor_resolve_opensandbox_s3_profile "${TEST_ROOT}"
[[ -z "${YICLOUD_SANDBOX_S3_CONFIG:-}" ]]

echo 'OpenSandbox S3 profile tests passed'
