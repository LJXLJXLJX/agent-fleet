#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../../../.." && pwd)"
# shellcheck source=../../../../../scripts/config_loader.sh
source "$REPO_ROOT/scripts/config_loader.sh"
agent_fleet_load_config "$REPO_ROOT"

runner_python="${HARBOR_TASK_IMAGE_PYTHON:-${HARBOR_OPIK_PYTHON:-}}"
if [[ -z "$runner_python" ]]; then
  if [[ -x /opt/harbor-runner/bin/python ]]; then
    runner_python=/opt/harbor-runner/bin/python
  elif [[ -x "${HOME}/.local/share/agent-fleet/harbor-runner/bin/python" ]]; then
    runner_python="${HOME}/.local/share/agent-fleet/harbor-runner/bin/python"
  else
    runner_python=python3
  fi
fi
exec "$runner_python" "$SCRIPT_DIR/dataset_cli.py" "$@"
