#!/usr/bin/env bash

# Resolve one project-local S3 profile into the existing OpenSandbox settings.
# A profile always binds its anonymous read location and object prefix. Its
# sibling s3cfg is an optional development-host write capability, not a
# prerequisite for consuming objects that another maintainer has published.
harbor_resolve_opensandbox_s3_profile() {
  local repo_root="$1"
  local profile="${YICLOUD_SANDBOX_S3_PROFILE:-}"
  local profile_root profile_dir profile_file s3cfg
  local raw_line line key value line_number=0
  local bucket="" read_origin="" prefix=""
  local bucket_seen=0 read_origin_seen=0 prefix_seen=0

  if [[ -z "${profile}" ]]; then
    return 0
  fi
  if [[ ! "${profile}" =~ ^[a-z0-9][a-z0-9._-]*$ ]]; then
    echo "[ERROR] invalid YICLOUD_SANDBOX_S3_PROFILE: ${profile}" >&2
    return 1
  fi
  if [[ -n "${YICLOUD_SANDBOX_UPLOAD_BACKEND:-}" &&
        "${YICLOUD_SANDBOX_UPLOAD_BACKEND}" != s3 &&
        "${YICLOUD_SANDBOX_UPLOAD_BACKEND}" != auto ]]; then
    echo '[ERROR] YICLOUD_SANDBOX_S3_PROFILE requires the s3 or auto upload backend' >&2
    return 1
  fi
  profile_root="${repo_root}/.s3-profiles"
  profile_dir="${profile_root}/${profile}"
  profile_file="${profile_dir}/profile.env"
  s3cfg="${profile_dir}/s3cfg"

  if [[ -L "${profile_root}" || ! -d "${profile_root}" ]]; then
    echo "[ERROR] S3 profile root is missing or is a symlink: ${profile_root}" >&2
    return 1
  fi
  if [[ -L "${profile_dir}" || ! -d "${profile_dir}" ]]; then
    echo "[ERROR] S3 profile directory is missing or is a symlink: ${profile_dir}" >&2
    return 1
  fi
  if [[ -L "${profile_file}" || ! -f "${profile_file}" || ! -r "${profile_file}" ]]; then
    echo "[ERROR] S3 profile metadata is not a readable regular file: ${profile_file}" >&2
    return 1
  fi
  if [[ -e "${s3cfg}" || -L "${s3cfg}" ]]; then
    if [[ -L "${s3cfg}" || ! -f "${s3cfg}" || ! -r "${s3cfg}" ]]; then
      echo "[ERROR] S3 profile write config is not a readable regular file: ${s3cfg}" >&2
      return 1
    fi
  else
    s3cfg=""
  fi

  while IFS= read -r raw_line || [[ -n "${raw_line}" ]]; do
    line_number=$((line_number + 1))
    line="${raw_line#"${raw_line%%[![:space:]]*}"}"
    line="${line%"${line##*[![:space:]]}"}"
    [[ -z "${line}" || "${line}" == \#* ]] && continue

    if [[ "${line}" =~ ^export[[:space:]]+ ]]; then
      line="${line#export}"
      line="${line#"${line%%[![:space:]]*}"}"
    fi
    if [[ ! "${line}" =~ ^([A-Za-z_][A-Za-z0-9_]*)[[:space:]]*=(.*)$ ]]; then
      echo "[ERROR] invalid assignment at ${profile_file}:${line_number}" >&2
      return 1
    fi

    key="${BASH_REMATCH[1]}"
    value="${BASH_REMATCH[2]}"
    value="${value#"${value%%[![:space:]]*}"}"
    value="${value%"${value##*[![:space:]]}"}"
    if [[ "${value}" == \"*\" && "${value}" == *\" ]]; then
      value="${value:1:${#value}-2}"
    elif [[ "${value}" == \'*\' && "${value}" == *\' ]]; then
      value="${value:1:${#value}-2}"
    fi

    case "${key}" in
      YICLOUD_SANDBOX_S3_BUCKET)
        if (( bucket_seen )); then
          echo "[ERROR] duplicate ${key} in ${profile_file}" >&2
          return 1
        fi
        bucket="${value}"
        bucket_seen=1
        ;;
      YICLOUD_SANDBOX_S3_READ_ORIGIN)
        if (( read_origin_seen )); then
          echo "[ERROR] duplicate ${key} in ${profile_file}" >&2
          return 1
        fi
        read_origin="${value%/}"
        read_origin_seen=1
        ;;
      YICLOUD_SANDBOX_S3_PREFIX)
        if (( prefix_seen )); then
          echo "[ERROR] duplicate ${key} in ${profile_file}" >&2
          return 1
        fi
        prefix="${value}"
        prefix_seen=1
        ;;
      *)
        echo "[ERROR] unsupported S3 profile key ${key} in ${profile_file}" >&2
        return 1
        ;;
    esac
  done < "${profile_file}"

  if [[ -z "${bucket}" || "${bucket}" == */* ]]; then
    echo "[ERROR] S3 profile ${profile} has an invalid bucket" >&2
    return 1
  fi
  if [[ ! "${read_origin}" =~ ^https?://[^/?#]+(/[^?#]*)?$ ||
        "${read_origin}" == *"@"* ||
        "${read_origin}" == *[[:space:]]* ]]; then
    echo "[ERROR] S3 profile ${profile} has an invalid anonymous read origin" >&2
    return 1
  fi
  if [[ -z "${prefix}" || "${prefix}" == /* || "${prefix}" == */ ||
        "/${prefix}/" == *"/../"* || "/${prefix}/" == *"/./"* ||
        "${prefix}" == *"//"* ]]; then
    echo "[ERROR] S3 profile ${profile} has an invalid prefix" >&2
    return 1
  fi

  if [[ -n "${YICLOUD_SANDBOX_S3_CONFIG:-}" &&
        ( -z "${s3cfg}" || "${YICLOUD_SANDBOX_S3_CONFIG}" != "${s3cfg}" ) ]]; then
    echo '[ERROR] YICLOUD_SANDBOX_S3_CONFIG conflicts with the selected S3 profile' >&2
    return 1
  fi
  if [[ -n "${YICLOUD_SANDBOX_S3_BUCKET:-}" &&
        "${YICLOUD_SANDBOX_S3_BUCKET}" != "${bucket}" ]]; then
    echo '[ERROR] YICLOUD_SANDBOX_S3_BUCKET conflicts with the selected S3 profile' >&2
    return 1
  fi
  if [[ -n "${YICLOUD_SANDBOX_S3_READ_ORIGIN:-}" &&
        "${YICLOUD_SANDBOX_S3_READ_ORIGIN%/}" != "${read_origin}" ]]; then
    echo '[ERROR] YICLOUD_SANDBOX_S3_READ_ORIGIN conflicts with the selected S3 profile' >&2
    return 1
  fi
  if [[ -n "${YICLOUD_SANDBOX_S3_PREFIX:-}" &&
        "${YICLOUD_SANDBOX_S3_PREFIX}" != "${prefix}" ]]; then
    echo '[ERROR] YICLOUD_SANDBOX_S3_PREFIX conflicts with the selected S3 profile' >&2
    return 1
  fi

  YICLOUD_SANDBOX_UPLOAD_BACKEND="${YICLOUD_SANDBOX_UPLOAD_BACKEND:-auto}"
  YICLOUD_SANDBOX_S3_CONFIG="${s3cfg}"
  YICLOUD_SANDBOX_S3_BUCKET="${bucket}"
  YICLOUD_SANDBOX_S3_READ_ORIGIN="${read_origin}"
  YICLOUD_SANDBOX_S3_PREFIX="${prefix}"
  export YICLOUD_SANDBOX_UPLOAD_BACKEND
  export YICLOUD_SANDBOX_S3_CONFIG
  export YICLOUD_SANDBOX_S3_BUCKET
  export YICLOUD_SANDBOX_S3_READ_ORIGIN
  export YICLOUD_SANDBOX_S3_PREFIX
}
