#!/usr/bin/env bash
set -euo pipefail

# Selection baseline from upstream/main 84f782a, before BackendPlan migration.
HARBOR_ENVIRONMENT_TYPE="${HARBOR_ENVIRONMENT_TYPE:-$RL_ENVIRONMENT_TYPE}"
if [[ "$HARBOR_CC_WEB_MCP_ENABLED" == "1" \
  && ( "$HARBOR_ENVIRONMENT_TYPE" == "e2b" || "$HARBOR_ENVIRONMENT_TYPE" == "qz" ) ]]; then
  echo "[ERROR] Web MCP is not supported on $HARBOR_ENVIRONMENT_TYPE: artifact delivery requires docker or opensandbox." >&2
  return 1
fi
if [[ -z "${HARBOR_ENVIRONMENT_SPEC:-}" ]]; then
  if [[ "$HARBOR_ENVIRONMENT_TYPE" == "opensandbox" ]]; then
    HARBOR_ENVIRONMENT_SPEC="yicloud_opensandbox:YiCloudOpenSandboxEnvironment"
  elif [[ "$HARBOR_ENVIRONMENT_TYPE" == "e2b" && -n "$HARBOR_E2B_PREBUILT_TEMPLATE" ]]; then
    HARBOR_ENVIRONMENT_SPEC="$HARBOR_E2B_PREBUILT_ENVIRONMENT_SPEC"
  elif [[ "$HARBOR_ENVIRONMENT_TYPE" == "qz" ]]; then
    HARBOR_ENVIRONMENT_SPEC="qz_e2b_sandbox:QzSandboxEnvironment"
  else
    HARBOR_ENVIRONMENT_SPEC="$HARBOR_ENVIRONMENT_TYPE"
  fi
fi
