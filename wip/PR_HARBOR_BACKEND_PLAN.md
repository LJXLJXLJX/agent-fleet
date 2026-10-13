# 独立 PR：拆分 Harbor 运行时 provider 并统一后端选择

日期/平台：2026-10-13，YiCloud Linux 开发机。
工作分支：`refactor/harbor-environment-adapters-resume`。
当前基线：`upstream/main` / main，`2c3420b`。
归档分支：`archive/harbor-environment-adapters-phase4-5`，提交 `fdb38f37527433ddb73971a361e178512ef6ff66`（旧基线 `84f782a`）。
恢复提交：`5f6de400cae6e6db6bc645276142f3ee5a9eadfe`；从最新 main cherry-pick 归档，无冲突，未改写 archive。
状态：用户确认本 PR 收尾阶段 4 + 5。本地验收完成，2026-10-13 单次 Oracle 真实运行验证 provider 执行与自动清理；任务补丁失败、reward 0。未推送、未创建远端 PR，阶段 7 整体未完成。

## 1. Why the change?

The YiCloud runtime previously kept control-plane SDK access, sandbox lifecycle,
multi-service readiness, exec, and file transport in one 3175-line provider.
Those responsibilities now live in focused runtime modules composed by the
concrete Harbor adapter. qz moves into a preserved legacy provider module;
existing Harbor import paths load the new classes.

Fixed/registry launches and rollout requests also used separate branches to
select provider import paths. A minimal BackendPlan centralizes that selection
and hands the same type/spec to existing launch/preflight consumers and shared
Python startup hooks.

## 2. Summary of the change

- Split YiCloud into adapter, config, client, lifecycle, services, execution,
  and transfer under `providers/opensandbox_yicloud/`, preserving existing
  methods and the one shared adapter instance state.
- Preserve qz in `providers/e2b_series/qz_legacy/adapter.py`, keeping its
  authentication, host/template semantics, timeout and retry boundaries.
  E2B series is a grouping inside providers; official E2B continues using Harbor.
- Keep `yicloud_opensandbox.py` and `qz_e2b_sandbox.py` as compatibility imports
  of the same new concrete environment classes.
- Recover only the actively consumed BackendPlan from the archived phase 1
  implementation; keep type/spec and schema 1 JSON handoff without secrets.
  Remove the unused provider/implementation fields.
- Keep legacy environment variables as projections and preserve existing
  custom import paths, E2B prebuilt mapping, and empty-value fallbacks.
- Resolve rollout backend overrides inside each request's subshell using the
  old override policy; repeated env loads reuse the selected plan.
- Add differential tests against frozen Shell behavior, process isolation,
  quoting, startup hooks, and actual fixed/registry command handoff.
- Isolate qz and command test fixtures from personal configuration. Repair the
  stale OpenSandbox static verifier fixture and mount JSON assertions so the
  complete command regression can run on current main.

This PR covers the BackendPlan portion of phase 4 and runtime provider extraction
from phase 5. It does not restore unused Artifact, capability, error, lifecycle,
or transfer scaffolding: existing Harbor contracts and the provider methods
remain the runtime contracts. Further Shell reduction remains phase 6.

## 3. Other details

已执行验证：复用 setup runner，Python 3.12.13、Harbor 0.18.0、E2B SDK 2.32.1。
未安装依赖。2026-10-10 的 TMax 入口尝试在创建前失败；2026-10-13 已完成单次 Oracle 真正运行和清理，未调用模型，未新增镜像构建/发布验收结论。

迁移前：BackendPlan 修改前 qz 129 项、E2B runtime 9 项和 qz Shell 通过；加入
plan hook 回归后，阶段 5 provider 迁移前同一套 qz 130 项、E2B runtime 9 项、
YiCloud 80 项和 qz Shell 通过。qz Shell fixture 隔离个人配置，避免访问个人 Opik。

2026-10-10 新基线通过 **344 项 Python 测试，无跳过**（upstream 新增 4 项 env aliases 回归）：

| Suite | Passed |
| --- | ---: |
| BackendPlan（含 24 组固定选择和 32 组 rollout 差异比较） | 8 |
| qz 全部本地 Python tests | 130 |
| YiCloud（多服务、healthcheck、alias、exec、传输、失败清理、取消、保留策略、删除确认） | 80 |
| 旧/新 class import 与真实 Harbor/E2B API 兼容检查 | 2 |
| E2B runtime / prebuilt | 9 / 6 |
| env aliases / env helpers | 19 / 20 |
| Harbor-to-E2B commands（含 fixed/registry 实际 fake CLI 交接） | 12 |
| native concurrency / package environment | 2 / 1 |
| rollout request context / listener startup | 33 / 7 |
| native rollout worker / rollout worker utils | 7 / 8 |

以下 6 个 Shell 脚本通过：

- `Agents/utils/common/Harbor/tests/test_harboropik_opensandbox.sh`
- `Agents/utils/common/Harbor/tests/test_harboropik_qz.sh`
- `Agents/utils/common/Harbor/tests/test_harboropik_extra_compose.sh`
- `Agents/utils/rl/tests/test_rollout_request_verifier_runtime.sh`
- `Agents/utils/rl/tests/test_rollout_worker_idle_wait.sh`
- `Agents/utils/rl/tests/test_rollout_timeout_trace_gate.sh`

仅静态检查：Ruff 0.16.0 检查全部修改/新增 Python 文件、修改 Shell 和冻结
fixture 的 `bash -n`、`git diff --check` 均通过。localhost HTTP/socket 用例最初
受到沙箱 socket 限制；解除限制后使用同一 runner 通过。

机械迁移静态核对：与当前 main 对照，YiCloud 原有 104 个函数/方法 AST 节点未变
（含同名/嵌套节点；旧记录按名称统计为 102），qz 全模块 AST 未变。
旧入口缩为 17 行和 27 行兼容模块；实际 YiCloud Harbor adapter 为 219 行。
YiCloud 测试继续通过旧 class 入口运行；依赖 patch 更新到各自归属模块。

旧阻塞已解决：OpenSandbox Shell fixture 已更新为现行 static runtime 格式并隔离
个人配置，两处 JSON 空格断言已修正。新 main 自带 DSH 显式空 `API_KEY` 修复；
完整 `test_harboropik_opensandbox.sh` 本轮通过，没有新增生产认证改动。

2026-10-10 历史入口尝试（使用工作区 `agent-fleet-opensandbox-task-test` Skill）：

- task：`task_000076_ad1c5cc9`；dataset：`/data/harbor-datasets/TMax-15K-Harbor`。
- 分支/代码 SHA：本工作分支 / `5f6de40`；仅有本地 Shell fixture 修改。
- 正式入口预检通过；单 worker、严格 S3、排队 86400s、生命周期 1440min、retain flags 0；`tasks.txt` 精确选中目标。
- 入口输出：`/data/harbor-runs/harbor-adapters-phase45-20261010`。
- Opik 健康检查 HTTP 502，worker exit 22 / missing trial result；失败发生在 Sandbox 创建前。没有 OCI digest、Sandbox ID、Running、S3 Sandbox 物化、Agent/verifier/reward 或 cleanup 证据。
- 当前模型 `API_KEY`、`MODEL` 为占位配置，用户已选择 Oracle 验收；观测本轮未通过。
- Skill 只等待 native benchmark exit 文件，旧 queue worker 已结束却无该文件。已停掉等待进程及本次 Zellij，没有伪造退出文件。Skill 已补 queue 完成检测；native 模式只适用于任务已带受支持预构建引用的情况。

2026-10-13 已执行一次真实 Oracle 验收（用户批准具体替代 task）：

- Task：`agent-fleet-prebuild-smoke/bfgroup__b2-113`；被测代码 `b580d51`，旧 spec 实际加载拆分后的 YiCloud provider。
- 1 completed trial、0 errored、0 retries；入口和 Skill exit 0，reward 0、`exception_info=null`。
- 控制面确认 `sbx-9dd55d21-df7` / `env-6e45c2be-74b` / Running，镜像 digest 匹配，入口 `sleep infinity`，生命周期 1440min。
- 严格 S3 传入 verifier uv、`/solution`、`/logs/agent`、`/tests`；实际 exec、Oracle/verifier 和结果下载已运行。
- Oracle 补丁失败：`src/tools/stage.jam: does not match index`；verifier 166 PASS / 1 FAIL（`install_build_no`），任务未通过。
- 自动清理后控制面 list 已无目标实例，再次 GetSandbox 返回 ret_code 105「资源不存在」。
- 详细证据：[Oracle 真实验收记录](HARBOR_ADAPTERS_ORACLE_ACCEPTANCE_20261013.md)；run root `/data/harbor-runs/harbor-adapters-phase45-oracle-20261013`。

本次获得 provider 真实执行及正常结束删除证据，不把 Oracle 成绩写成成功。
真实模型 Agent/Claude Runtime、Opik 观测、start 失败/取消 live cleanup、多服务、
官方 E2B、Docker live 回滚和新镜像发布仍待验证；真实 qz 不属于本次门槛。阶段 7 整体未完成。

复现命令（仓库根目录，按 workspace 测试约定复用 runner）：

```bash
runner_python="${HARBOR_RUNNER_HOST_DIR:-$HOME/.local/share/agent-fleet/harbor-runner}/bin/python"
PYTHONPATH=. timeout 180 "$runner_python" -m unittest discover \
  -s Agents/utils/common/Harbor/tests -p test_backend_plan.py -v
PYTHONPATH=. timeout 180 "$runner_python" -m unittest discover \
  -s Agents/utils/common/Harbor/tests -p 'test_qz_*.py' -v
PYTHONPATH=. timeout 120 "$runner_python" -m unittest discover \
  -s Agents/utils/common/Harbor/tests -p test_e2b_runtime.py -v
PYTHONPATH=. timeout 180 "$runner_python" -m unittest discover \
  -s Agents/utils/common/Harbor/tests -p test_yicloud_opensandbox.py -v
PYTHONPATH=. timeout 120 "$runner_python" -m unittest discover \
  -s Agents/utils/common/Harbor/tests -p test_environment_adapter_imports.py -v
timeout 180 bash Agents/utils/common/Harbor/tests/test_harboropik_qz.sh
HARBOR_OPIK_PYTHON="$runner_python" timeout 240 bash \
  Agents/utils/common/Harbor/tests/test_harboropik_opensandbox.sh
```

阶段 4 日志和诊断 fixture patch：`/tmp/harbor-backend-plan-validation/`。
阶段 5 迁移前后完整日志、原始 provider 对照和诊断 fixture：`/tmp/harbor-provider-refactor-validation/`。
临时 upstream worktree 已移除。

恢复后验证日志：`/tmp/harbor-adapters-resume-20261010/`。

2026-10-10 Oracle 验收准备：TMax 原 task 无 `solution/solve.sh`，native 入口也因无预构建镜像引用在创建前失败。候选改为现有 `agent-fleet-prebuild-smoke/bfgroup__b2-113`，已有参考解，Oracle/1 worker/S3/queue 模式预检通过。当时自动审批要求对这个具体替代 task 单独授权；2026-10-13 用户已授权并完成一次实际运行，结果见上述记录。

Skill 辅助改动仅在工作区 `.agents/skills/agent-fleet-opensandbox-task-test/`：增加 `--agent oracle`、明确 Claude 的模型配置要求、Oracle 参考解预检及 queue 完成检测，同步相关说明。该目录不属于本仓库 PR；Shell 语法及非法 Agent/缺参考解拒绝检查已通过。
