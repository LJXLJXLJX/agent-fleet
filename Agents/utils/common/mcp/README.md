# Exa Web MCP
Enable automatic preparation alongside the agent dependency cache:
```bash
export HARBOR_CC_WEB_MCP_ENABLED=1
```

The bundled Python sources produce a deterministic, content-addressed zipapp in
`LOCAL_WHEEL_DIR`. Unchanged sources reuse it; source changes select a new artifact
without overwriting artifacts in use. OpenSandbox checks/reuses the existing S3
object or uploads it through the existing transport. Docker uses the existing
local artifact mount. This does not upload to a remote HTTP cache.

Managed E2B/qz backends do not support MCP artifact delivery. Enabling this
switch on either backend is rejected during configuration loading, before
dependency preparation or Sandbox launch, even when a local artifact is cached.

Anonymous Exa access is the default. Set `EXA_API_KEY` in private runtime
configuration to use your own key; keys are never embedded in the artifact.
Anonymous access can be rate-limited and is not guaranteed to support a benchmark.

`HARBOR_CC_WEB_MCP_ENABLED` is the only MCP enable switch. Its default is `0`.
`HARBOR_CC_WEB_MCP_SOURCE` is an internally derived cache path, not user
configuration: an old exported value is ignored, including when MCP is disabled.
For a standalone build outside Agent Fleet:
```bash
python3 Agents/utils/common/mcp/build.py /data/mcp-cache
```

## Research Content Policy

This branch applies task-aware filtering inside the bundled MCP, not the rollout
loop or verifier. There is only one user-facing MCP enable switch:

```bash
export HARBOR_CC_WEB_MCP_ENABLED=1
```

With MCP enabled, filtering is active. The Claude hook passes only the current
public instruction to that task's MCP process, never the verifier reference or
answer. Missing instructions fail closed. Policy configuration stays in
`exa/config.py`; it does not introduce another startup enable variable.

| Profile | Question word window | Search-query word window |
| --- | ---: | ---: |
| `browsecomp` | 5 | 5 |
| `deepsearchqa` | 8 | 6 |

These windows follow [AvaCore's rollout recipes](https://github.com/sii-avalanche/AvaCore/tree/f28d1093483f696b04ff4ef7e7ac44e0e957112c/examples/rollout).
The default `LEAK_PROFILE = "browsecomp"` conservatively uses 5/5 windows for both
datasets, including mixed runs. To reproduce DeepSearchQA's 8/6 windows, set
`LEAK_PROFILE = "deepsearchqa"` in that file before starting a separate run. The
source change automatically selects a new cached bundle. Unknown profiles fail
closed. This choice changes the evaluation protocol and must be recorded with
scores; profiles are never guessed from task names.

The policy checks outgoing arguments for configured benchmark terms/URL patterns.
Returned text is checked for those patterns, current-question word windows, and
(for search only) query word windows. Defaults also block the two benchmark names.
`exa/config.py` contains the profiles and optional term/URL lists. The tool
description discourages verbatim-question and answer-key searches; question
windows are response filters, not a prohibition on sending those words to Exa.

Exa's MCP returns rendered content rather than Serper's organic-result objects.
If any returned content, structured content or metadata matches, the proxy replaces
the **entire tool result** with a generic message without matching excerpts. Opaque
non-text content is rejected; only the two filtered tools are exposed. Source
changes automatically select a new cached bundle.

This is **best-effort lexical filtering, not an internet security boundary**.
It cannot prevent Bash/curl, native tools, another client, encoded/paraphrased
answers, or model memorization. It also cannot inspect original pages that Exa has
already summarized or truncated before returning them. Legitimate repeated phrases
can be blocked, especially with query windows. Unlike AvaCore's per-search-result
filter, one contaminated result can suppress otherwise valid results in the same
MCP response. Stronger guarantees require controlled tool access and network egress.
