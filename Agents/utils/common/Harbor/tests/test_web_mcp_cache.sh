#!/usr/bin/env bash
set -euo pipefail

HARBOR_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_ROOT="$(cd "$HARBOR_DIR/../../../.." && pwd)"
eval "$(sed -n '/^harbor_prepare_web_mcp()/,/^}/p' "$HARBOR_DIR/env/dependencies.sh")"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
export LOCAL_WHEEL_DIR="$tmp/cache" HARBOR_CC_WEB_MCP_ENABLED=1
builder="$REPO_ROOT/Agents/utils/common/mcp/build.py"
expected="$(python3 "$builder" "$LOCAL_WHEEL_DIR" --path-only)"
[[ ! -e "$LOCAL_WHEEL_DIR" ]]
resolved="$(HARBOR_CC_WEB_MCP_SOURCE=/ignored/custom.py EXA_API_KEY= bash -c \
  'source "$1"; printf "%s\n%s\n" "$HARBOR_CC_WEB_MCP_SOURCE" "$EXA_API_KEY"' _ "$HARBOR_DIR/env.sh")"
[[ "$resolved" == "$expected"$'\n'"anonymous" ]]
HARBOR_CC_WEB_MCP_SOURCE="$expected"
harbor_prepare_web_mcp
[[ -f "$expected" ]]
touch -t 200001010000 "$expected"
mtime="$(stat -c %Y "$expected")"
harbor_prepare_web_mcp
[[ "$(stat -c %Y "$expected")" == "$mtime" ]]
python3 -m zipfile -l "$expected" | grep -q '__main__.py'
disabled="$(HARBOR_CC_WEB_MCP_ENABLED=0 HARBOR_CC_WEB_MCP_SOURCE="$expected" \
  LOCAL_WHEEL_DIR="$tmp/disabled" bash -c \
  'source "$1"; harbor_prepare_web_mcp; printf "%s" "$HARBOR_CC_WEB_MCP_SOURCE"' _ "$HARBOR_DIR/env.sh")"
[[ -z "$disabled" && ! -e "$tmp/disabled" ]]
