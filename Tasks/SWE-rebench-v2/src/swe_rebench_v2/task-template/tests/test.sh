#!/bin/bash
set -euo pipefail

mkdir -p /logs/verifier
rm -f /logs/verifier/reward.txt /logs/verifier/report.json
LOG_FILE=$(mktemp)
export LOG_FILE
{test_commands}

set +e
PARSER_PYTHON="${HARBOR_VERIFIER_PYTHON:-}"
if [ -n "$PARSER_PYTHON" ] && [ ! -x "$PARSER_PYTHON" ]; then
  bundle_archive="${HARBOR_VERIFIER_RUNTIME_BUNDLE_ARCHIVE:-}"
  bundle_root="${HARBOR_VERIFIER_RUNTIME_BUNDLE_ROOT:-}"
  if [ -f "$bundle_archive" ] && [ -n "$bundle_root" ] && [ "$bundle_root" != "/" ]; then
    case "$bundle_root" in
      /*)
        if command -v tar >/dev/null 2>&1; then
          rm -rf -- "$bundle_root"
          mkdir -p -- "${bundle_root%/*}"
          tar -xzf "$bundle_archive" -C "${bundle_root%/*}"
        fi
        ;;
    esac
  fi
fi
if [ -z "$PARSER_PYTHON" ] || [ ! -x "$PARSER_PYTHON" ]; then
  PARSER_PYTHON="/opt/conda/bin/python"
fi
if [ ! -x "$PARSER_PYTHON" ]; then
  PARSER_PYTHON="$(command -v python3 || command -v python)"
fi
if [ -z "$PARSER_PYTHON" ] || [ ! -x "$PARSER_PYTHON" ]; then
  echo "SWE-rebench-V2 verifier requires Python, but no interpreter is available" >&2
  rm -f /logs/verifier/reward.txt /logs/verifier/report.json
  exit 127
fi
# parser.py writes report.json and reward.txt only after grading completes.
"$PARSER_PYTHON" /tests/parser.py | tee -a "$LOG_FILE"
exit_code=$?
set -e

exit "${exit_code}"
