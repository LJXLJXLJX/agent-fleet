#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source_repo_root="$(cd "$script_dir/.." && pwd)"
repo_root="${1:-$source_repo_root}"

if [[ ! -e "$repo_root/.git" ]] && \
   ! git -C "$repo_root" rev-parse --git-dir >/dev/null 2>&1; then
  printf '[ERROR] not a Git checkout: %s\n' "$repo_root" >&2
  exit 1
fi
if [[ ! -x "$repo_root/.githooks/pre-commit" ]]; then
  printf '[ERROR] pre-commit hook is missing or not executable: %s\n' \
    "$repo_root/.githooks/pre-commit" >&2
  exit 1
fi

git -C "$repo_root" config core.hooksPath .githooks
printf '[INFO] Git hooks enabled for %s (core.hooksPath=.githooks)\n' "$repo_root"
