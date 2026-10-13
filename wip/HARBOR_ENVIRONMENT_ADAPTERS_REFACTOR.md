# Harbor Environment Adapters 重构设计（WIP）

状态：阶段 4 + 5 已恢复到 `refactor/harbor-environment-adapters-resume`，基线 `2c3420b`。2026-10-13 完成一次 Oracle 真实执行和正常结束清理验证；任务补丁失败，reward 0。阶段 6 留到下一份 PR，阶段 7 整体未完成；归档保留，尚未创建远端 PR。

更新时间：2026-10-13（Asia/Singapore）

验证范围：阶段 1–3 已完成（用户确认）。2026-10-10 本地回归 344 项 Python、6 个 Shell 和静态检查通过；2026-10-13 仅执行一个真实 Oracle task，provider 创建、exec、S3 物化、verifier/结果回收、自动删除已有实测证据，Oracle 补丁失败、reward 0。真实模型 Agent、观测、start 失败/取消、多服务 live 等仍待验证；不把 task 成绩或阶段 7 写成通过。

当前封存参考：原阶段 1 及其后续 WIP 快照已封存在 `agent-fleet` 分支
`archive/harbor-environment-adapters-phase1`，孤儿提交 `bccdfcba32ba93590b3cb058c05369dc0b1c6d42`（无父提交）。该提交保留
BackendPlan、adapter core 契约、image preparation 拆分、兼容 facade、测试和文档，
是后续取回和对照的参考实现，不代表所有模块已经接入生产路径或通过完整验收。

历史阶段 2 工作分支为 `refactor/harbor-image-preparation`，从 `main` 的 `65f4d21`
创建，只保留 image preparation 实现、兼容 facade、相关测试和文档。原混合快照
`7457039` 与孤儿封存提交的 tree 相同；阶段 4 按文件或修改块参考取回，
不整体合并孤儿历史。

本轮原工作分支为 `refactor/harbor-backend-plan`，从 `upstream/main`
`84f782a` 创建，现封存为本地分支 `archive/harbor-environment-adapters-phase4-5`。
归档分支同时保存代码和本文、`PR_HARBOR_BACKEND_PLAN.md` 的副本，路径为仓库内 `wip/`。
2026-10-09 曾切回 main 暂停；2026-10-10 从最新 main 创建恢复分支，再 cherry-pick 归档。归档历史未改写，未推送远端。

本轮独立 PR 范围为 **运行时 provider 拆分与统一后端选择（阶段 4 + 5）**：

1. 在修改公共入口前执行 qz/E2B runtime 本地基线，冻结旧 Shell 选择和 rollout 覆盖策略。
2. 仅取回并修正有实际调用方的不可变 BackendPlan，只保留 type/spec；不恢复整套未接入的 core 抽象。
3. shared env 统一解析，fixed/registry/preflight 消费 plan 的旧变量投影；rollout 请求在自己的子进程生成 plan；共享 Python 启动钩子读取同一 JSON plan。
4. 拆分 YiCloud client、configuration、lifecycle、services、execution 和 transfer，最终 Harbor adapter 组合这些内部职责模块；保持原方法行为与共享实例状态。
5. 将 qz 原实现封存在 `providers/e2b_series/qz_legacy/adapter.py`；旧模块保留兼容导入，官方 E2B 继续直接复用 Harbor。
6. 验证重复 source、旧 import path、自定义 spec、E2B prebuilt、请求切换和 qz hook 行为；执行迁移前后的同一套 provider 回归，并更新结构文档。

进一步收缩 Shell 留到后续 PR；ImageArtifact、RuntimeReference、
能力、错误、生命周期和传输契约按实际消费者需求接入，不以恢复空抽象完成阶段 4。

### 归档与恢复记录

- 原始基线：`84f782a201f0b619856adc918ece914bbf593156`。
- 封存分支：`archive/harbor-environment-adapters-phase4-5`，普通 WIP 提交，保留基线历史；不是旧阶段 1 的孤儿快照。
- 封存提交：`fdb38f37527433ddb73971a361e178512ef6ff66`（`WIP: archive Harbor provider and BackendPlan refactor`），包含代码及两份 `wip/` 文档。提交时全仓 Ruff hook 通过。
- 已执行验证：YiCloud 开发机，2026-10-09，340 项 Python 测试、5 个 Shell 回归通过；Ruff 0.16.0、Shell 语法和 diff 检查通过。
- 已知未通过项：OpenSandbox Shell fixture 过时；临时修正 fixture 后，当前代码与 upstream 基线均在同一个 DSH 空 API key 断言失败，未纳入本次改动。
- 待完成：阶段 6 Shell 收缩、阶段 7 集成验收；真实 YiCloud/qz/E2B Sandbox、agent/verifier、观测、镜像发布及清理链路均待验证。
- 本地验证日志在 `/tmp/harbor-provider-refactor-validation/`、`/tmp/harbor-backend-plan-validation/`，属于临时文件；验证结论已记录在封存文档，不依赖这些日志才能定位代码。

### 2026-10-10 恢复与本 PR 验收

- main 已 fast-forward 到 `upstream/main` 的 `2c3420b`。
- 恢复分支：`refactor/harbor-environment-adapters-resume`；cherry-pick 归档后的提交为 `5f6de400cae6e6db6bc645276142f3ee5a9eadfe`，无冲突。archive 未改写。
- 用户确认本 PR 收尾阶段 4 + 5；阶段 6 及没有实际消费者的 core 契约保持后续范围。
- 已执行：344 项 Python 测试、6 个 Shell 回归，无跳过；Ruff 0.16.0、修改 Shell 的 `bash -n` 和 `git diff --check` 通过。runner 为 Python 3.12.13、Harbor 0.18.0、E2B 2.32.1，未安装依赖。
- 已修正 OpenSandbox Shell fixture：复用现行 static verifier runtime fixture、隔离个人配置、更新两处 JSON 空格断言。新 main 已包含 DSH 显式空 API key 修复，完整 OpenSandbox Shell 回归通过。
- 仅静态检查：与当前 main 的原 provider 对照，YiCloud 104 个函数/方法 AST 节点保持不变（包含同名/嵌套节点；旧记录按名称统计为 102）；qz 全模块 AST 保持不变。
- 真实验收：通过 `agent-fleet-opensandbox-task-test` 正式入口，task 为 TMax `task_000076_ad1c5cc9`，单 worker，严格 S3，24 小时排队/生命周期和自动清理配置。预检通过，用户明确批准远程创建/上传/必要镜像构建推送。
- 已执行的入口运行目录：`/data/harbor-runs/harbor-adapters-phase45-20261010`。`tasks.txt` 仅含目标 task；Opik 健康检查 HTTP 502 导致 worker exit 22，未创建 Sandbox，无 trial result。不能算真实 provider 验收通过。
- 当前 `API_KEY`、`MODEL` 为占位配置；用户已选择 Oracle 参考解验收，不调用模型。真实 exec、S3 Sandbox 物化、verifier/reward、cleanup 和观测均待验证。
- Skill 默认等待 native benchmark exit 文件；本次走旧 queue worker，结束后无该文件。已依据 worker 失败日志停止等待进程及本次 Zellij 会话；未伪造退出文件。后续按用户 Oracle 选择为 Skill 增加 Agent 参数、参考解校验和 queue 完成检测，未改动生产入口。
- 本轮本地日志：`/tmp/harbor-adapters-resume-20261010/`；详细范围见 `wip/PR_HARBOR_BACKEND_PLAN.md`。阶段 7 的全局完成勾选保持未完成。

### 2026-10-13 Oracle 真实验收

- 用户明确批准替代 task `bfgroup__b2-113` 的单次 Oracle/S3/OpenSandbox 验收。
- 被测代码 `b580d51`，1 completed trial、0 error、0 retry；Skill/入口 exit 0，任务 reward 0。
- 控制面确认 `sbx-9dd55d21-df7` 处于 Running，环境 `env-6e45c2be-74b` / `jianxiao-sandbox-0`，镜像 digest、`sleep infinity` 和实际 1440min 生命周期匹配。
- `/solution`、`/tests`、verifier uv 和 agent artifacts 严格 S3 物化；Oracle 参考补丁应用失败，verifier 166 PASS / 1 FAIL（`install_build_no`），成绩 0。
- 运行结束后控制面 list 无目标实例，GetSandbox ret_code 105「资源不存在」，自动清理已确认。
- 运行目录：`/data/harbor-runs/harbor-adapters-phase45-oracle-20261013`；详细证据与范围见 [Oracle 真实验收记录](HARBOR_ADAPTERS_ORACLE_ACCEPTANCE_20261013.md)。不扩展到全量 prebuild smoke、不修改任务、不增加真实模型或观测结论。

复核本 PR 使用 `git diff 2c3420b..HEAD`；旧归档与旧验证记录作为历史证据保留。

本文记录 Agent Fleet Harbor 运行环境相关代码的重构方向。目标是把 Docker、E2B-compatible 系列、YiCloud OpenSandbox 专属实现及独立 prebuilder 放到清晰的边界内，减少后端分支、全局补丁和超大文件。本文不改变现有运行行为；迁移期间保留兼容入口。

## 1. 现状与问题

当前实现同时分布在 Harbor 启动 Shell、环境配置、provider adapter、镜像管理器和全局 Python 启动钩子中。

- 后端选择同时由 `HARBOR_ENVIRONMENT_TYPE`、`HARBOR_ENVIRONMENT_SPEC`、Shell 分支和 `sitecustomize.py` 决定，存在配置分裂。
- `yicloud_opensandbox.py` 同时负责控制面认证、sandbox 生命周期、服务编排、exec、文件传输、S3 降级和清理。
- `opensandbox_image_manager.py` 同时负责 Compose 解析、Docker Buildx、BuildKit source rewrite、Registry、Skopeo、缓存、OCI manifest 和 Bundle 生成。
- `e2b_runtime.py` 通过 monkey patch 修改 Harbor/E2B 类的内部方法；qz 通过修改 E2B SDK 的 `ConnectionConfig` 复用实现。
- `prebuild_opensandbox_dataset.sh` 名称和配置都绑定 OpenSandbox，但其中大部分功能实际是通用的任务扫描、镜像构建、缓存、发布和报告；prebuilder 本应是独立包。
- qz 和 AgentENV 都属于 E2B-compatible 系列，但两者处理方式不同：AgentENV 的官方定位是直接使用标准 E2B SDK；qz 虽复用官方 SDK，仍有 endpoint、认证、host 和模板语义差异。
- 当前 OpenSandbox 代码实际是 YiCloud SDK 驱动的专属实现，公共 OpenSandbox SDK 已验证不适合作为当前运行基础；不把它设计成面向其他 OpenSandbox-like 平台的通用 adapter。
- 绝大多数任务是单镜像任务，BundleSpec/Compose 多服务抽象不是本次重构重点；当前只保留现有输入兼容和最小 image reference 处理。
- 配置、重试、超时、清理、错误分类和观测字段在不同 provider 中重复实现，难以保证行为一致。

现有代码规模也已经影响维护：

- `yicloud_opensandbox.py` 约 3175 行；
- `opensandbox_image_manager.py` 约 2867 行；
- `harboropik.sh` 约 1948 行；
- `e2b_runtime.py` 约 475 行，承担多个全局补丁职责。

## 2. 命名和顶层边界

统一目录名采用：

```text
harbor_environment_adapters/
```

这个名称强调它们是 Harbor 环境接口的适配层。prebuilder 是独立包，不放在此目录中。

当前已落地目录（2026-10-10）；其他模块只在真实消费者需要时加入：

```text
Agents/utils/common/Harbor/harbor_environment_adapters/
├── core/
│   └── config.py             # BackendPlan 解析、序列化和旧配置映射
└── providers/
    ├── e2b_series/            # 同属 provider；series 只表示内部归组
    │   ├── README.md         # 官方 E2B 直接复用 Harbor，不复制实现
    │   └── qz_legacy/
    │       └── adapter.py    # qz 原实现封存，变更从严
    └── opensandbox_yicloud/
        ├── adapter.py        # 实际 Harbor 环境类和初始化
        ├── config.py
        ├── client.py
        ├── lifecycle.py
        ├── services.py
        ├── execution.py
        └── transfer.py
```

`e2b_series` 放在 `providers/` 内，与 YiCloud provider 并列；不建立与 provider 平级的
“系列”概念。Docker 和官方 E2B 继续由 Harbor 提供，没有新职责时不创建空 adapter。
兼容入口为 Harbor 目录下原有 `yicloud_opensandbox.py`、`qz_e2b_sandbox.py`；测试继续放在原 `tests/`。

## 3. 核心对象和公共契约

### 3.1 BackendPlan

当前后端解析生成不可变的 `BackendPlan`，只保留生产消费者实际使用的字段：

```text
BackendPlan
  environment_type: 旧后端类型
  environment_spec: 精确的 Harbor 环境名称或 module:Class
```

Shell、worker、preflight 和 Python runtime 都消费同一个 plan，不再分别解释环境变量。

实际 Shell preflight/命令构造消费 plan 的旧变量投影；进一步移除 provider 校验和能力分支
属于阶段 6。`provider`、`implementation` 等无消费者的字段不保留，provider 配置和能力
继续由现有环境实现及 Harbor 契约负责。下述 Artifact/传输等候选契约仍按真实调用方决定是否接入。

### 3.2 单镜像任务定义

本次不引入新的 `BundleSpec` 抽象。当前绝大多数任务是单镜像任务，重构只需要保留任务环境目录、Dockerfile/Compose 现有输入和最终 image reference 的校验。

现有 `compose_bundle.py` 及已实现的多服务行为保持兼容。本次不重新设计任务定义模型，也不删除或收窄已有 Compose 能力。

### 3.3 ImageArtifact 和 RuntimeReference

构建结果使用不可变 Artifact：

```text
ImageArtifact
  digest
  platform
  source_hash
  builder_version
  manifest_schema
  registry_ref
```

运行时只消费 `RuntimeReference`，不重新推断 tag、Compose 文件或镜像来源。tag 只用于索引，实际执行优先使用 digest。

### 3.4 ArtifactTransfer

统一文件和目录传输接口：

```text
upload_file()
upload_dir()
download_file()
download_dir()
materialize_bundle()
```

具体实现包括本地挂载、E2B File API、YiCloud execd、HTTP 和 S3。verifier runtime、agent runtime 和 task artifact 不直接依赖 provider SDK。

### 3.5 生命周期和错误

统一生命周期：

```text
Provisioning -> Starting -> Ready -> Running -> Stopping -> Deleted
                         \-> Failed
```

统一错误分类：

```text
ConfigurationError
CapabilityError
ProvisionError
DispatchError
RemoteCommandError
TransferError
ArtifactBuildError
CleanupError
```

重试策略必须区分“请求尚未到达远端”和“请求已经产生副作用”，避免 sandbox 或模板重复创建。

## 4. Prebuilder 独立包

已确定：prebuilder 拆为独立包，允许脱离 Agent Fleet 环境 adapter 单独安装、调用和复用。它从当前历史实现中抽离，不作为 `harbor_environment_adapters` 的子包，不依赖 Sandbox 生命周期或平台 SDK。任务解析和 hash 等已有 Harbor 工具依赖需要逐项处理，不能为追求独立而改变已有 hash 语义。

### 4.1 当前产物：保持不变

2026-09-22 静态核对 `agent-fleet/Agents/utils/common/Harbor/opensandbox_image_manager.py`：

| 阶段 | 当前产物/行为 | 代码位置 |
| --- | --- | --- |
| 本地构建 | Buildx 输出 gzip 压缩的 OCI archive，关闭 provenance；构建路径使用临时 `image.oci.tar` | `run_build()`、`_prepare_service_image()` |
| 发布 | `skopeo copy --format v2s2` 将镜像发布为 Docker schema 2 manifest | `SkopeoPublisher.copy()` |
| 校验和引用 | 比对 copy 的 digest 与独立 inspect 结果，返回 `repository@sha256:…` | `SkopeoPublisher.copy()`、`prepare_bundle()` |
| 附属元数据 | 版本化 JSON Bundle manifest，包含任务、服务、镜像引用等；单镜像任务为隐式 `main` 服务 | `prepare_bundle()` |
| CLI 输出 | image-ref、manifest 路径或摘要 JSON | `main()` |

因此它生产的是通用容器镜像，而不是 YiCloud 私有镜像格式。严格区分本地 OCI archive 与发布后的 Docker schema 2 media type；JSON Bundle manifest 是附属元数据，不是 OCI image manifest。

本次保持上述产物契约，不更换镜像格式，不新增 E2B/qz template 产物或平台 publisher。现有内容 hash、tag/repository 规则、digest 校验、缓存判定和 manifest 兼容性都需要回归保护。Registry 访问与配置的历史耦合可以拆分，但不借此要求迁移现有 Registry。

### 4.2 包结构与入口

建议目标结构：

```text
agent-fleet/harbor-prebuilder/
├── pyproject.toml
├── src/harbor_prebuilder/
│   ├── planner.py
│   ├── worker.py
│   ├── build_engine.py
│   ├── registry.py
│   ├── cache.py
│   ├── manifest.py
│   ├── report.py
│   └── cli.py
└── tests/
```

通用 prebuilder 的输入输出如下：

```text
TaskDefinition
  -> ImageBuildPlan
  -> ImageArtifact
  -> PublishResult
  -> RuntimeReference
```

通用逻辑包括：

- 扫描任务并验证环境定义；
- 解析任务环境中的 Dockerfile 和现有 image 配置；
- 计算内容哈希；
- 调用 Buildx 或其他 BuildEngine；
- 本地内容寻址缓存；
- OCI Registry push、inspect 和 digest 校验；
- 并发、锁、超时、GC 和报告。

各运行平台消费现有镜像引用。将来若需要模板注册，应单独讨论该接入步骤；本次独立包拆分不引入插件框架或多平台发布流程。

`prebuild_opensandbox_dataset.sh` 暂时保留为兼容 wrapper，内部改为调用独立包的 `harbor-prebuilder` CLI。新代码不应继续新增 `HARBOR_OPENSANDBOX_PREBUILD_*` 业务变量；旧变量只在 wrapper 中做映射。

## 5. E2B-compatible 系列

E2B 不作为多个互相复制的 provider 处理，而在 `providers/e2b_series/` 内组织已有兼容实现。series 是 provider 内部归组，不是顶层概念。公共基础优先复用 Harbor 官方 `E2BEnvironment` 和官方 `e2b` SDK；只有存在平台差异时才增加实现级配置或兼容代码。

AgentENV 尚未正式接入。已知它兼容官方 E2B SDK，但这不代表其所有行为与官方平台相同。[AgentENV README](https://github.com/kvcache-ai/AgentENV#-e2b-compatibility) 本次只记录这一未来接入方向，不创建 AgentENV 专属目录、profile、adapter、能力声明或测试占位，也不为可能存在的平台扩展预建接口。

本次 E2B series 只组织已有实现：

```text
官方 E2B：直接复用 Harbor 官方 E2BEnvironment，不新增本地目录或复制实现

providers/e2b_series/qz_legacy/
  封存现有 qz 行为，只做必要的安全和兼容修复

```

公共逻辑从当前代码中有明确重复的部分提取，优先复用 SDK 和 Harbor 已有接口。以下是后续接入时需要核对的差异点，不是本次必须实现的 capability 字段或扩展框架：

- 模板是否可以动态构建；
- template alias、tag 和 digest 语义；
- API URL 和认证头；
- envd host 拼接规则；
- sandbox timeout 上限；
- network policy 和动态网络切换；
- pause/resume/snapshot/fork；
- 多服务和 sidecar 支持；
- 文件传输限制和批量行为。

因此 `qz_e2b_sandbox.py` 迁移时应作为 `qz_legacy` 封存，尽量保留原始行为，不将其重写成新的通用 adapter。AgentENV 正式接入时优先使用官方 SDK，先核对所需行为；配置即可解决时只配置，确有差异时再增加最小兼容代码。

本次的可扩展性止于清晰的模块边界：qz 特例不进入公共路径，新实现将来可以在 `providers/e2b_series/` 下独立加入，配置入口不与单个平台强绑定。不提前设计 SDK facade、插件注册框架或 snapshot/fork 等未使用功能的抽象。

## 6. Provider 责任边界

按实际职责拆分模块，不强制每个平台创建全套空壳；可采用：

```text
config.py       配置解析、默认值、校验和脱敏
client.py       第三方 SDK/HTTP 控制面
adapter.py      Harbor BaseEnvironment 适配
capabilities.py 能力声明和限制
```

### 6.1 YiCloud OpenSandbox 专属环境

当前目录命名使用 `opensandbox_yicloud`，明确这是一套 YiCloud 专属环境。它可以复用通用 Artifact、传输和生命周期契约，但不建立 `opensandbox_family`，也不追求其他 OpenSandbox-like 实现的兼容。当前实现主要基于 YiCloud SDK；公共 OpenSandbox SDK 已验证存在问题，因此不应作为重构后的底层依赖。

OpenSandbox YiCloud 额外拆出：

- `services.py`：多服务拓扑、depends_on、healthcheck 和 alias；
- `transfer.py`：HTTP/快速上传/S3/execd；
- `client.py`：认证、请求、状态轮询和删除。

YiCloud 已有多服务逻辑包括服务启动顺序、`depends_on`、healthcheck、服务别名通信，以及服务级 exec/stop 和失败清理。这些是需要保持的既有行为，不是本次新增的通用 Compose 架构；也不作为待裁剪功能。重构重点仍是单镜像主路径和超大文件拆分。

新 E2B 接入避免新增全局 SDK 补丁；qz 的兼容补丁和启动钩子作为 legacy 行为保留，不将移除它们作为本次完成条件。AgentENV 默认不增加 adapter，实际差异另行验证。YiCloud 环境独立维护自己的 SDK client 和运行时逻辑。

## 7. 迁移阶段 TODO

阶段顺序按真实依赖排列。旧的“阶段 1 core 契约”实现已单独封存，待阶段 2、3
明确实际消费者后再取回、校正并接入；不把未使用的契约当作已完成的生产能力。

### 1. 冻结行为基线

- [x] 建立 Docker、E2B official、qz legacy、YiCloud 专属环境的现有能力矩阵；AgentENV 不纳入本次实现与验收。
- [x] 保存现有 Bundle manifest、镜像 digest 和关键启动参数的 golden fixtures。
- [x] 记录 create/start/exec/upload/download/stop/cleanup 的验收路径。
- [x] 在修改 qz 或其依赖的公共入口之前，通过第 8 节规定的单机测试。
- [x] 暂不修改线上默认值和旧入口。

### 2. 拆分镜像管理实现，保持产物

- [x] 验证 `image_preparation/` 拆分后的 BuildEngine、Registry、Cache、Manifest 和 planner。
- [x] 保持 OCI archive、gzip、关闭 provenance、Docker schema 2、digest 校验和 Bundle manifest 兼容。
- [x] 保持旧 `opensandbox_image_manager.py` import path、CLI 参数、输出和退出码。
- [x] 完成 image manager 单元回归和 manifest golden fixture 验证。

### 3. 提取独立 prebuilder

- [x] 新增可独立安装和调用的 `harbor-prebuilder` 包与 CLI。
- [x] 将 `prebuild_opensandbox_dataset.sh` 改为兼容 wrapper，旧变量只在 wrapper 中映射并提示弃用。
- [x] 保持内容 hash、tag/repository、缓存、并发、锁、digest、OCI/v2s2 和 JSON manifest 语义。
- [x] 使用重构后的 prebuilder 完整跑完 `agent-fleet-prebuild-smoke`，保存每个任务的日志、digest、manifest 和状态。

### 4. 取回并落地 core 契约

- [x] 从 `archive/harbor-environment-adapters-phase1` 取回并修正 BackendPlan。
- [x] 将 BackendPlan 接入 fixed run、registry run、rollout 和 preflight，共享同一解析结果；旧变量作为兼容投影保留。
- [x] 为 BackendPlan 增加纯单元测试、冻结旧 Shell 的差异测试和进程交接测试。
- [ ] 按阶段 2、3 和 provider 实际消费者需求接入 ImageArtifact、RuntimeReference、错误、能力、生命周期和传输契约；未使用的抽象暂不恢复。

2026-10-09，YiCloud 开发机：迁移前 qz 129 项、E2B runtime 9 项和 qz Shell 基线通过；
qz Shell fixture 增加隔离配置，避免加载个人配置后访问 Opik。迁移后验证结果见
`wip/PR_HARBOR_BACKEND_PLAN.md`。2026-10-10 新基线上的本地回归再次通过；真实验证进展见恢复记录，尚不能宣称 provider、模型、镜像发布或清理通过。

### 5. 拆分运行时 provider

- [x] 拆分 YiCloud 专属 client、service graph、transfer、execution 和 lifecycle，并保持多服务、healthcheck、alias、exec/stop 和失败清理。
- [x] 将 qz 代码迁移为 legacy 封存模块，保留认证、endpoint、host、模板、超时、重试和 verifier 注入语义。
- [x] qz 迁移前后使用同一套本地测试；真实 qz Sandbox 不作为本次门槛。
- [x] E2B official 继续复用 Harbor 官方实现；不实施 AgentENV 接入或占位代码。

2026-10-09，YiCloud 开发机：provider 迁移前后 qz 130 项、E2B runtime 9 项、YiCloud
80 项本地测试通过；兼容导入检查使用已安装的真实 Harbor/E2B API 验证旧新类相同且非抽象。
静态核对 YiCloud 原有 102 个函数/方法 AST 及 qz 全模块 AST 保持不变。
2026-10-13 已补一次 Oracle 真实创建、传输、exec/verifier 和正常结束删除证据，任务成绩 0；见验收记录。start 失败/取消、多服务和真实模型/观测仍待验证，不将阶段 7 整体标为完成。

### 6. 收缩 Shell 编排

- [ ] `harboropik.sh` 只负责参数收集、plan/prebuilder 调用和 Harbor 启动。
- [ ] 将后端校验、能力判断、artifact 解析和错误分类移到 Python。
- [ ] fixed run、registry run、rollout 共用同一个 plan 生成器。
- [ ] 保留 Docker backend 的配置级回滚路径和旧环境变量映射。

### 7. 集成验收与兼容收口

- [ ] 运行 core、provider contract 和旧入口回归测试。
- [ ] 运行 YiCloud 主力链路 live smoke，区分基础执行、Agent、verifier、观测和清理验证范围。
- [ ] 核对 start 失败、取消、正常结束的幂等清理。
- [ ] 核对所有运行时使用不可变 digest 或明确模板引用。
- [ ] 完成文档、迁移说明和最终 PR 拆分；未验证的外部依赖明确标记为待验证。

## 8. 兼容和验证要求

迁移期间必须保留：

- 旧 `HARBOR_ENVIRONMENT_SPEC` import path；
- `prebuild_opensandbox_dataset.sh` 命令入口；
- 现有 Bundle manifest 读取兼容；
- Docker backend 的配置级回滚路径；
- 旧环境变量到 typed config 的显式映射。

验证分三层：

1. `core` 纯单元测试：Bundle、digest、cache、manifest、config 和错误分类；
2. provider contract tests：所有 provider 共用生命周期、exec、传输和 cleanup 契约；
3. live smoke：YiCloud 主力链路的验证需在实际重构时执行；AgentENV 在后续接入时执行。依赖真实 qz 平台 Sandbox 的测试不属于本次范围，也不作为 qz 迁移门槛。

### 8.1 qz 重构前本地测试门槛

- **必须先跑过再重构**：qz 所有单机可执行的单元测试和功能测试必须通过；只读静态检查不能替代。存在失败或本地用例未执行时，不进入影响 qz 行为的迁移。
- 按工作空间 `AGENTS/agent-fleet-testing.md` 复用已有 runner 环境，记录 Python/Harbor/SDK 版本、命令、通过/失败/跳过数量。迁移后用同一套测试复验。
- 已发现的直接测试入口：Harbor `tests/test_qz_*.py` 和 `tests/test_harboropik_qz.sh`。范围覆盖 sandbox 兼容、模板映射/解析/管理/批量准备、任务初始化与 instruction、repository plan、Pi 集成、sitecustomize 和 Shell 参数编排。
- 还需执行 qz 依赖的 `test_e2b_runtime.py`，并按实际依赖检查共享配置、启动和 rollout 的本地 qz 路径；不能仅跑 sandbox 类测试就认定完成。
- 使用 fake SDK、mock HTTP、临时文件、子进程和本地服务即可完成的功能测试均属于必跑范围。只依赖远端平台才能验证的创建、exec、文件传输和销毁不要求执行；必须区分同名操作的本地模拟测试与真实平台测试。
- 不以缺少远端凭据为由跳过可单机完成的测试；对真正的平台依赖测试明确记录不执行及原因，不宣称平台可用。
- 封存时保留认证与 endpoint 映射、host 补丁、超时上限、模板/任务初始化语义、重试、verifier 注入以及旧入口；不为目录统一重写这些行为。

当前状态（2026-10-09，YiCloud 开发机）：BackendPlan 迁移前 qz 129 项、E2B runtime
9 项及 qz Shell 通过；新增 plan hook 回归后，阶段 5 provider 迁移前后同一套 qz
130 项、E2B runtime 9 项及 qz Shell 通过。真实 qz Sandbox 未执行。

### 8.2 Prebuilder 必须跑完现有 smoke 数据集

验收对象是已有的 Harbor dataset **`agent-fleet-prebuild-smoke`**，不是泛指随意挑一个任务做 smoke，也不是 `test_opensandbox_prebuild.py` 等本地测试的别名。

- 本地入口：工作空间 `datasets/agent-fleet-prebuild-smoke/`，实际指向 `/data/harbor-datasets/agent-fleet-prebuild-smoke/`；2026-09-22 静态核对含 27 个 `task.toml`。
- 用户确认线上也有该数据集；线上路径/版本本轮未核对，不臆造 registry ID 或地址。执行时记录实际使用的副本、版本或内容标识和完整任务清单，任务数以该固定副本为准。
- **必须使用重构后的 prebuilder 跑完整套数据集的预构建和发布流程，并核对每个任务的结果。** 不用抽样、dry-run、mock 或只通过单元测试替代。
- 对每个任务保存构建/发布日志、最终镜像 digest、manifest 和成功/失败状态；失败、漏跑或跳过均需明确列出，不能当作通过。
- 记录构建与缓存命中情况；如果全程只复用旧镜像，不能据此宣称新的构建和发布路径通过验收。
- 本节验收 prebuild 链路。Harbor agent/oracle/verifier 运行结果属于另一层验证，不能以其替代实际预构建，也不据数据集名称自动扩大到整套模型评测。

当前状态：阶段 3 已完成（用户确认，保留该阶段的完成勾选）。本轮不重新执行 prebuild smoke，
不新增构建、发布或平台可用性结论；历史证据以阶段 3 的实际报告为准。

### 8.3 完成标准


- 后端分支不再散落在多个 Shell 文件；
- qz legacy 本地测试在重构前、后均通过；其必要兼容补丁保持行为，其他新路径避免新增全局补丁；
- prebuilder 作为独立包发布，不依赖 OpenSandbox；
- 重构后的 prebuilder 完整跑完 `agent-fleet-prebuild-smoke` 数据集并通过结果及产物检查；
- 运行时使用不可变 Artifact digest；
- start 失败、进程取消和正常结束都能幂等清理；
- 各 provider 的能力限制可以通过统一接口查询；
- 旧入口在兼容期内行为不变。

## 9. 后续接入事项与已确定边界

- AgentENV 正式接入时再调查实际行为并验证 Harbor 所需操作；不阻塞本次重构，也不据此提前增加抽象。

已确定，不再作为开放问题：qz 本地测试必须先通过、真实平台测试不要求；YiCloud 现有多服务行为保留；prebuilder 当前产物保持不变，并允许作为独立包复用。

2026-10-10 Oracle 验收准备：TMax 原 task 无 `solution/solve.sh`，native 入口也因无预构建镜像引用在创建前失败。候选改为现有 `agent-fleet-prebuild-smoke/bfgroup__b2-113`，已有参考解，Oracle/1 worker/S3/queue 模式预检通过。当时自动审批要求对这个具体替代 task 单独授权；2026-10-13 用户已授权并执行，结果见上述记录。

Skill 辅助改动仅在工作区 `.agents/skills/agent-fleet-opensandbox-task-test/`：增加 `--agent oracle`、明确 Claude 的模型配置要求、Oracle 参考解预检及 queue 完成检测，同步相关说明。该目录不属于本仓库 PR；Shell 语法及非法 Agent/缺参考解拒绝检查已通过。
