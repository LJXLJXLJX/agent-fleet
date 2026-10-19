# Harbor provider 重构：Oracle 真实验收记录

日期/平台：2026-10-13，YiCloud Linux CPU 开发机；时区 Asia/Singapore。
结论：已执行一次真实 Oracle run，provider 的创建、执行、S3 传输、verifier 运行、结果回收及正常结束清理获得实测证据；**任务未通过，reward 0**。不将框架 exit 0 或 provider 操作完成写成 Oracle 任务成功。

## 运行对象

- 分支：`refactor/harbor-environment-adapters-resume`。
- 被测代码：`b580d519a62f2a2f7b93620346249ea2849bb4bc`，运行前 Git 工作区干净。
- Task：`bfgroup__b2-113`；dataset：`/data/harbor-datasets/agent-fleet-prebuild-smoke`。
- Agent：Oracle 参考解，1 worker、1 trial、0 retry；没有调用模型，没有扩展到全量 smoke。
- Run ID：`harbor-adapters-phase45-oracle-20261013`。
- 入口：工作区 `agent-fleet-opensandbox-task-test` Skill → `scripts/run_fleet.sh` → `start.sh` → `harboropik.sh`。
- `tasks.txt` 只有 `bfgroup__b2-113`，队列模式；旧兼容 spec `yicloud_opensandbox:YiCloudOpenSandboxEnvironment` 实际加载拆分后的 provider。
- 严格 `YICLOUD_SANDBOX_UPLOAD_BACKEND=s3`，没有切换 HTTP fallback；排队 timeout 86400s，生命周期 1440min，两个 retain flags 均为 0。
- `OPIK_URL=`、summary/monitor/analyzer 关闭，本次不验证观测上传。

完整镜像引用由当前 image manager 解析，未手工给固定镜像：

```text
harbor-sandbox.lg.shzhisuan.com/agent-fleet-swe-rebench-v2-syslab-adapter/bfgroup__b2-113@sha256:9706dcddc22ca52036440c6915f4334fe439adf1b8ec8213a8856ace6aa460d9
```

Bundle schema 2，main service 为 `main`；平台返回的镜像省略 registry host，repository/digest 与完整引用一致。本轮没有新构建/推送日志，不据此增加 image build/publish 验收结论。

## 已执行验证

| 环节 | 实际证据 |
| --- | --- |
| 创建与 Running | 控制面确认 `sbx-9dd55d21-df7` 为 Running，创建时间 `2026-10-13T01:59:18.02801Z` |
| 环境绑定 | `env-6e45c2be-74b` / `jianxiao-sandbox-0`；GetSandboxEnvironment 与 GetSandbox/ListSandboxes 均已实际调用 |
| 创建入口 | 控制面返回 `Entrypoint=["sleep","infinity"]` |
| 生命周期 | 控制面返回 `LifecycleMinutes=1440`，`ExpiresAt=2026-10-14T01:59:18.02801Z` |
| S3 物化 | `job.log` / `trial.log` 有 `backend=s3` 成功记录：`/opt/tb-uv-backup/bin`、`/solution`、`/logs/agent`、`/tests` |
| Oracle 执行 | `agent/exit-code.txt=1`；`oracle.txt` 捕获补丁应用错误，不是未启动 |
| Verifier 执行 | `test-stdout.txt` 记录 166 PASS / 1 FAIL；失败测试 `install_build_no` |
| 结果回收 | `reward.txt=0`，`report.json.resolved=false`；trial `exception_info=null`；job 1 completed、0 errored、0 retries；入口和 Skill exit 0 |
| 自动清理 | `2026-10-13T02:04:06.711069Z` 控制面环境列表已无目标实例；再次 GetSandbox 返回 ret_code 105「资源不存在」；未执行手工删除 |

排队 timeout 为配置/调用参数检查；没有等待 24 小时，不宣称长时间 Pending 场景已实测。

Oracle 输出：

```text
Checking patch src/tools/stage.jam...
error: src/tools/stage.jam: does not match index
```

Verifier 缺失预期通过项 `install_build_no :PASSED`，最终成绩 0。这里仅确认失败发生在参考解补丁应用并影响任务测试；没有继续修改数据集或镜像，也没有用第二次 task run 替代这次失败。

## 证据位置

运行根目录：`/data/harbor-runs/harbor-adapters-phase45-oracle-20261013`。

- `tasks.txt`、`1-bfgroup__b2-113.console.log`、`skill-launcher.log`。
- Trial 相对目录：`jobs/oracle/worker-1/1-bfgroup__b2-113/2026-10-13__09-59-12/2026-10-13__09-59-17/bfgroup__b2-113__uzNHPKx`。
- Trial：`result.json`、`trial.log`、`agent/{oracle.txt,exit-code.txt}`、`verifier/{test-stdout.txt,report.json,reward.txt}`。
- job 的 `result.json` / `job.log` 位于 trial 的父目录。
- 控制面只读记录：`/tmp/harbor-adapters-live-20261013/control-plane-target.json`、`control-plane-after.json`、`control-plane-deleted-get.err`。本文记录必要字段，不依赖临时文件才能理解结果。
- 控制面观测复用正式 runner 的 pinned SDK 和 `env.sh`。初次使用工作区 SDK 默认配置的 GetSandboxEnvironment 请求曾返回 400；后续同 runner/config 的成功在线查询才作为本次证据。
- 为只读 Skill `yicloud-opensandbox-safe/scripts/observe.py` 补出已有 API 的 Entrypoint、LifecycleMinutes 和 ExpiresAt 字段；辅助 Skill 改动位于工作区，不纳入 agent-fleet PR。

Task 输入 SHA-256：

| 文件 | SHA-256 |
| --- | --- |
| task.toml | `0f823f0b0f8fc8f31463409c178bcfb923c21d7060bdd890f07c62c11c1de83b` |
| solution/solve.sh | `533aa2b646f9c296f23069d1927eebc3d883ed89db4268fdba6ae75745f5209b` |
| tests/test.sh | `3d2e752f3b59c64e51fcb51144315c516468ca9af0762fc9c09efc0d8c9b9912` |
| environment/Dockerfile | `ff561a101eef001092a56a98b829e603cacdf91da6bb1d57f7be75bbea4be9b9` |

## 尚未验证

- Oracle 成功任务成绩；当前 task 的补丁/index 不匹配根因仍待进一步排查。
- Claude Code/Node Runtime、真实模型 Agent、Opik/其他观测上传。
- live start 失败/取消的幂等清理、多服务场景、长时间 Pending。
- 官方 E2B/qz live、Docker live 回滚、新镜像构建发布。

2026-10-10 的 344 项 Python、6 个 Shell 回归以及 Ruff/语法检查记录仍有效，但本轮未重新执行。阶段 6、阶段 7 整体保持未完成。
