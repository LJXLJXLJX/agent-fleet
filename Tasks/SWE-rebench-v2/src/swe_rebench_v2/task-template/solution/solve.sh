#!/bin/bash
set -euo pipefail

REPO_DIR={repo_dir}
if [ ! -d "$REPO_DIR/.git" ]; then
  printf 'Expected repository is missing: %s\n' "$REPO_DIR" >&2
  exit 127
fi
cd "$REPO_DIR"

cat > /tmp/solution_patch.diff << '__SOLUTION__'
{patch}
__SOLUTION__

git apply -v --3way --recount --ignore-space-change --whitespace=nowarn /tmp/solution_patch.diff
