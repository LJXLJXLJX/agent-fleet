# 独立 PR：拆分 Harbor 运行时 provider 并统一后端选择

日期/平台：2026-10-09，YiCloud Linux 开发机。
原工作分支：`agent-fleet/refactor/harbor-backend-plan`。
封存分支：`archive/harbor-environment-adapters-phase4-5`。
基线：封存时的 `upstream/main`，`84f782a`。
状态：2026-10-09 按用户要求暂停并保存为本地 WIP 提交，以处理其他高优先级工作；未推送、未创建远端 PR，尚未完成完整重构验收。

本草稿与设计记录一同保存在 archive 分支的 `wip/` 下。开发目录已切回 `main`。
恢复工作请从 archive 新建分支；阶段 6、7 和下述未通过/待验证项仍未完成。

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
- Isolate qz and command test fixtures from personal configuration.

This PR covers the BackendPlan portion of phase 4 and runtime provider extraction
from phase 5. It does not restore unused Artifact, capability, error, lifecycle,
or transfer scaffolding: existing Harbor contracts and the provider methods
remain the runtime contracts. Further Shell reduction remains phase 6.

## 3. Other details

已执行验证：复用 setup runner，Python 3.12.13、Harbor 0.18.0、E2B SDK 2.32.1。
未安装依赖、未访问真实 Sandbox 或模型、未构建/发布镜像。

迁移前：BackendPlan 修改前 qz 129 项、E2B runtime 9 项和 qz Shell 通过；加入
plan hook 回归后，阶段 5 provider 迁移前同一套 qz 130 项、E2B runtime 9 项、
YiCloud 80 项和 qz Shell 通过。qz Shell fixture 隔离个人配置，避免访问个人 Opik。

迁移后通过 **340 项 Python 测试，无跳过**：

| Suite | Passed |
| --- | ---: |
| BackendPlan（含 24 组固定选择和 32 组 rollout 差异比较） | 8 |
| qz 全部本地 Python tests | 130 |
| YiCloud（多服务、healthcheck、alias、exec、传输、失败清理、取消、保留策略、删除确认） | 80 |
| 旧/新 class import 与真实 Harbor/E2B API 兼容检查 | 2 |
| E2B runtime / prebuilt | 9 / 6 |
| env aliases / env helpers | 15 / 20 |
| Harbor-to-E2B commands（含 fixed/registry 实际 fake CLI 交接） | 12 |
| native concurrency / package environment | 2 / 1 |
| rollout request context / listener startup | 33 / 7 |
| native rollout worker / rollout worker utils | 7 / 8 |

以下 5 个 Shell 脚本通过：

- `Agents/utils/common/Harbor/tests/test_harboropik_qz.sh`
- `Agents/utils/common/Harbor/tests/test_harboropik_extra_compose.sh`
- `Agents/utils/rl/tests/test_rollout_request_verifier_runtime.sh`
- `Agents/utils/rl/tests/test_rollout_worker_idle_wait.sh`
- `Agents/utils/rl/tests/test_rollout_timeout_trace_gate.sh`

仅静态检查：Ruff 0.16.0 检查全部修改/新增 Python 文件、修改 Shell 和冻结
fixture 的 `bash -n`、`git diff --check` 均通过。localhost HTTP/socket 用例最初
受到沙箱 socket 限制；解除限制后使用同一 runner 通过。

机械迁移静态核对：YiCloud 原有 102 个函数/方法 AST 未变，qz 全模块 AST 未变。
旧入口缩为 17 行和 27 行兼容模块；实际 YiCloud Harbor adapter 为 219 行。
YiCloud 测试继续通过旧 class 入口运行；依赖 patch 更新到各自归属模块。

已执行但未通过的已有套件：`test_harboropik_opensandbox.sh`。
原 fixture 仍使用已失效的 verifier runtime marker，另有两处 JSON 空格断言过时。
临时修正这些 fixture 后，当前分支和独立 upstream `84f782a` worktree 都在同一个
DSH 断言失败：显式空 `API_KEY` 被 `env.sh` 的 `:-xxx` 默认值替代，测试要求拒绝。
阶段 5 拆分后再次运行同一诊断 fixture，仍在该 DSH 断言失败。
这不是本次重构引入的回归。诊断用 fixture 修改已撤回，未扩大本 PR 到
verifier fixture 或模型认证行为；该套件完整通过仍待修复，不能算作通过。

本轮真实 YiCloud/qz/E2B Sandbox、agent/verifier、观测、镜像发布及清理链路均待验证。

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
```

阶段 4 日志和诊断 fixture patch：`/tmp/harbor-backend-plan-validation/`。
阶段 5 迁移前后完整日志、原始 provider 对照和诊断 fixture：`/tmp/harbor-provider-refactor-validation/`。
临时 upstream worktree 已移除。
