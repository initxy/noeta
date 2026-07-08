# Per-session Sandbox：每会话一个容器 + 全工具/Skill 进沙箱（Tier 2，Docker-first）

> 状态：**已实现（2026-07-08，S1–S11 全落地，branch `feat/per-session-sandbox`）**。真容器 e2e 仍 gated（`NOETA_TEST_AIO_SANDBOX_URL` / 本地 Docker）。标「【替你定，可否决】」的是维护者确认「按推荐来」后我替他定的机械决策，可事后推翻。
> 前置：v1 `ExecEnv` seam 已合入 main（`docs/adr/execution-environment-seam.md` + `docs/implementation-specs/2026-07-07-sandbox-exec-env.md`）。本 spec 是它的 v2 演进。
> 评审修复（2026-07-08，post-review）：
> - **built-in/global skill 层：服务端非目标（维护者定，撤销接线）**。评审 P1 指出「sandbox 下 built-in/global skill 层容器化未在产品侧接」；曾一度补上产品接线（`serve_backend` 挂 built-in skills + `HostConfig`/`SdkHost` 容器路径重映射），但维护者判定 built-in skill 对服务端**无用**，遂全部撤回。最终形态：服务端**不配置** built-in/global skill 层；sandbox 下真正生效的是 **workspace 层**（`<workdir>/.noeta/skills`），它随 workspace mount + `workspace_dir→/workspace` 重映射天然容器化（既有机制，非本轮新增）。runtime 侧 `SkillIndexer(exec_env=)` 机制仍在、仍有单测覆盖，供未来需要时接线。故 S6 的「built-in/global 目录容器化 + mount」在服务端范围内**降级为 non-goal**。
> - **web 出网 `--fail`**：容器 `curl`（fetch/search）此前无 `-f`，4xx/5xx 下 exit 0 → fetch 把错误页当成功返回、search 静默降级为「无结果」；加 `--fail` 与 httpx `raise_for_status` 对齐（R3）。
> - **密钥不落 argv/进程表**：`SANDBOX_API_KEY` 改 `-e` 按名透传（值经子进程 env 注入），Tavily key 改走 `curl --config` 文件（经 `/v1/file/write` 非 shell 写入、`-K` 引用、用后即删）。
> - **P3 共享 attach 缓存**：`SandboxExecEnvManager.release` 不再驱逐共享 `default_ref` 的缓存 backend（per-session 路径 ref 唯一，不受影响）。

## Goal

把 v1 的「一 host 一共享容器、只路由 fs/shell、产品端未激活」升级为：

1. **每会话一个 Sandbox 容器**（per root-task tree，真 provision / teardown，而非 attach 一个共享容器）。
2. **Tier 2 全工具进沙箱**：fs + 前台 shell + skill 加载(indexer) + skill 脚本(run_skill_script) + workspace 配置加载器(instructions/environment/shell-allowlist) + web fetch/search 的**执行**都落在会话自己的容器里。
3. **产品端激活**：`apps/noeta-agent` 真正把 sandbox 接通，默认可开。
4. **agent / SDK 分层清晰**：provisioning 与生命周期归 agent 层，SDK 只定义并消费一个 `SandboxProvider` seam。

## Non-goals

- **memory 工具不进容器**：`memory_read/write` 是全局跨会话用户记忆（固定宿主目录，非 workspace 级），留宿主。放进 per-session 临时容器会随会话销毁丢记忆。
- **MCP 不进容器**（v1 本轮）：stdio server 子进程、HTTP MCP 仍在 worker 侧（宿主）跑。Tier 3（MCP 进容器）留后续——它要给 seam 加 MCP 方法且可能扰动 MCP 工具 schema（破 stable-prefix）。
- **不做 K8s / 内部分配服务 backend**（本轮）：只做 Docker backend；`SandboxProvider` seam 留好扩展位。
- **不做 warm pool / pause / snapshot**：per-session 容器绑会话生命周期，idle 成本记 known-limitations。
- **不改 tool 的 model-facing 契约**（name/schema/description）→ stable-prefix KV-cache 不变（硬约束，见 CONTEXT.md Stable Prefix）。
- **不改 EventLog 事件字节语义、不动 fencing ADR 的 D1–D3、不动 Dispatcher/Engine 主循环。**

## Context

- **三层拓扑与 import-linter band**：`noeta.tools`（materials）> `noeta.runtime`（kernel-services）> `noeta.execution`；SDK `noeta.client` 在 tools 之上（可 import `AioSandboxExecEnv`）；`apps/noeta-agent`（`noeta.agent`）在最上。原始 ADR 原则：**「分配/管理」属 agent 层（对齐 workspace registry），「机制」属 runtime，config 只带寻址不带密钥。** 本 spec 贯彻这条。
- **v1 已就绪的机制**（直接复用/演进）：
  - `ExecEnv` Protocol + `LocalExecEnv` + `AioSandboxExecEnv`（`packages/noeta-runtime/noeta/tools/fs/exec_env.py`），IO+进程接口，AIO wire 契约（`/v1/shell/exec`、`/v1/file/*`）锁在一个 adapter。
  - `WorkspaceRoot.for_container`（词法容器根，`tools/fs/_workspace.py`）。
  - `build_fs_tools(exec_env=)`（`tools/fs/__init__.py`），一个 backend 共享给整个 fs/shell pack。
  - `exec_env_ref` 的 durable 全链：`TaskHostBoundPayload.exec_env_ref`(events) → `GovernanceState.exec_env_ref`(task) → fold → resolver `_bound_exec_env_ref_for` + engine cache-key 第 9 维 + subtask 继承 → `SdkHost._build_engine` reconnect（`packages/noeta-sdk/noeta/client/host.py`）。weld 在 `driver.seed_start`。
  - `SandboxExecEnvConfig` + `HostConfig.exec_env`（`packages/noeta-sdk/noeta/client/host_config.py`）；`SandboxExecEnvManager`（`noeta/client/sandbox.py`），v1 只按 base_url 缓存一个 backend。
- **v1 为何是共享容器（本 spec 要推翻的点）**：T5 note #2 记载——AIO 用的 API 面无「建容器」调用，`base_url` 寻址一个外部容器；per-root 键控被 seed engine(`task_id=None`) 与首个 driving turn 的 engine cache 合并所旁路。本 spec 通过引入真 provision（provider 按 root-task mint 独立容器）+ `exec_env_ref` 携带 `sandbox_id` 解决。
- **agent-sandbox SDK 事实**（调研）：`agent-sandbox`(PyPI) / `@agent-infra/sandbox`(npm) 是**纯 HTTP client**，**不 provision 容器**。「一会话一容器」= 自己起一个容器 per session（Docker/K8s）。AIO 单容器 8080 端口 front 所有服务：shell/bash、file、jupyter/nodejs、browser+CDP、`/mcp`、port-proxy、VNC/VSCode。Auth：`SANDBOX_API_KEY` 经 `X-AIO-API-Key` / `Authorization: Bearer` / `?api_key=`。镜像 `ghcr.io/agent-infra/sandbox:latest`（CN 镜像 `enterprise-public-cn-beijing.cr.volces.com/vefaas-public/all-in-one-sandbox:<ver>`）。⚠️ 搜索里带 `SandboxClaim`/`WarmPool` 的是**另一个**项目 kubernetes-sigs/agent-sandbox，不是 agent-infra。
- **Tier 2 涉及的宿主触点（调研审计，全部要改道）**：
  - skill indexer 裸 `Path`：`context/skills/indexer.py:155,163,170,176,180,191,276,277`；`execution/skills.py:136,166,368,369`。
  - `run_skill_script` 裸 FS + 裸 `run_argv`（无 exec_env 字段）：`tools/fs/skill_script.py:163,164,169,175,182`。
  - web 裸 httpx：`tools/web/fetch.py:229`、`tools/web/search.py:199`。
  - workspace 配置加载器读容器路径却打宿主 FS（v1 sandbox 下**已坏**）：`load_project_shell_allowlist`(`host.py:1383`/`shell.py:334-377`)、`load_environment`(`builder.py:524`)、`load_instructions`(`builder.py:509`)。
  - `ToolContext`（`protocols/tool.py:107`）故意不带 exec_env / workspace；per-call 装配点在 `runtime/tool.py:104`。

## Decisions

### D1【已确认】三层职责切分

| 关注点 | 层 | 内容 |
|---|---|---|
| 沙箱机制（跟容器对话） | runtime | `ExecEnv` seam；本 spec 把覆盖面从 fs/shell 扩到 skill 加载/脚本/workspace 加载器/web |
| 沙箱绑定（哪个 session 用哪个容器、durable、reconnect、密钥从 env 取） | SDK | `exec_env_ref` 全链（已有，演进为携带 sandbox_id）；定义 `SandboxProvider` Protocol 并调用；把 live backend 喂进 `_build_engine` |
| 沙箱 provisioning + 生命周期（真建/销容器、mount、池化） | agent | 具体 `SandboxProvider` 实现（Local / Distributed 两族）；何时 allocate/release |

> 术语澄清：本 spec 无「编排层」这个第四层。所谓 provisioning / orchestration 就是「真正调 `docker run`/`docker rm`（或 K8s API）建销容器的那段代码」= `SandboxProvider` 的实现本身，落在 **agent 层**。

### D2【已确认】`SandboxProvider` seam：SDK 定义，agent 实现（Local / Distributed 两族）

- **Protocol**（落 SDK，`noeta.client`）：
  ```python
  class SandboxAuth(Protocol):    # 【为 TAE 预留】auth 是策略，不是静态 key
      def connect_headers(self) -> dict[str, str]: ...   # 连接时生成鉴权 header
  # v1 实现 StaticApiKeyAuth(env_name) → {"X-AIO-API-Key": os.environ[env_name]}
  # TAE 实现 JwtBearerAuth(signer)    → {"Authorization": f"Bearer {signer.mint()}"}（短时 JWT）

  class SandboxHandle:            # addressing 部分可序列化落 log；auth 是 live 对象不序列化
      base_url: str               # 完整 URL，必须支持网关路径前缀（https://gateway/<prefix>），非仅 host:port
      sandbox_id: str
      workdir: str                # 容器内 workspace 根，默认 /workspace
      auth: SandboxAuth           # NOT serialized；reconnect 时由本机 config 重建（密钥/私钥不落 log）

  class MountSpec:                # 单条挂载（storage 层可配置，见 D5）
      source: str                 # 宿主路径 / NAS 子目录 / volume 名 / PVC 名
      target: str                 # 容器内路径（Local 与 Distributed 保持一致 → 免路径翻译）
      mode: str                   # "rw" | "ro"
      kind: str                   # "local-path" | "nas" | "volume" | "pvc"

  class SandboxSpec:              # allocate 输入：建容器要的一切
      image: str
      mounts: list[MountSpec]     # 可配置挂载列表（workspace + skills + 任意扩展）
      resources: dict             # memory / cpus 等限制
      env: dict                   # 注入容器的额外 env

  class SandboxProvider(Protocol):
      def allocate(self, session_root_id: str, spec: SandboxSpec) -> SandboxHandle: ...
      def release(self, session_root_id: str) -> None: ...
      def attach(self, handle_ref) -> SandboxHandle: ...   # reconnect：按录制 ref 重连，不新建
  ```
- **两族实现**（真正区分轴是「容器跑在哪」，不是「Docker 还是 K8s」）：
  | | **Local Sandbox** | **Distributed Sandbox** |
  |---|---|---|
  | 容器位置 | 与 worker 同机（本地 Docker daemon） | 远程节点 / 集群（K8s Pod、远程 Docker、内部分配服务） |
  | addressing | `127.0.0.1:<port>` | 网关地址 + 路径前缀 `https://gateway/<prefix>` |
  | auth | 静态 `X-AIO-API-Key`（`StaticApiKeyAuth`） | JWT Bearer 短时（`JwtBearerAuth`，`JWT_PUBLIC_KEY` 验签） |
  | reconnect | 同机 attach；本机重启/跨机丢 | 按 session_id 重连，NAS 存活 → **跨机原生可达** |
  | 挂载 source | 宿主本地路径（`kind=local-path`，`-v`） | NAS（`kind=nas` → TAE `fuse_mount_params`）/ PVC / NFS |
  | 实现 | **`LocalDockerSandboxProvider`（本轮）** | **`TaeSandboxProvider`**（后续，seam 零改接入）/ K8s provider |
- **`LocalDockerSandboxProvider`**（`apps/noeta-agent`，`noeta.agent`）三个方法的具体动作：
  - `allocate` = 定镜像/容器名(`noeta-sbx-<session_root_id>`)/空闲宿主端口 → 组 `docker run -d`（`-p 127.0.0.1:<port>:8080` + `-e SANDBOX_API_KEY` + 各 `-v mounts` + 资源限制 + `--security-opt seccomp=unconfined`）→ 起容器 → 健康探测（轮询 `GET /v1/sandbox` 带 key 至 ready/超时）→ 返回 `SandboxHandle`。
  - `release` = `docker rm -f noeta-sbx-<id>`。
  - `attach` = 容器还在则按 ref 拼回 `base_url`（查端口）；容器没了（本机重启/跨机）报明确错——本地 Docker 的固有局限，由 Distributed/NAS backend 解。
- **SDK 消费**：`SandboxExecEnvManager` 从「按 base_url 缓存一个静态 backend」重构为「持一个 `SandboxProvider`，按 `session_root_id` allocate/缓存/release」。`SdkHost` 通过 `HostConfig` 注入 provider（默认 None ⇒ 走 `LocalExecEnv`，字节等价回退）。`SandboxSpec.mounts` 由 SdkHost 从 session 的 workspace_dir + noeta 自带 skills 目录 + 用户 global skills 目录组装（config 可扩展）。

### D3【已确认】Scope = Tier 2

进容器（执行落容器）：`read/glob/grep/edit/write/apply_patch`、前台 `shell_run`、skill indexer、`run_skill_script`、workspace 加载器（instructions/environment/shell-allowlist）、`webfetch`/`web_search`。
留宿主：`memory_*`（全局记忆）、MCP（stdio/HTTP，Tier 3）、`shell` 的 background/poll/kill（AIO 无 durable job，沿用 v1 拒绝）、`open_app`（宿主 preview gateway）。

### D4【已确认 + 演进】per-session 容器 + 生命周期 + ref 携带 sandbox_id

- **绑定粒度**：一容器绑一 root-task 树（subtasks 共享父容器，对齐 rewind ADR「子任务共享父 cwd/磁盘」）。key = session-root task id。
- **eager provision**：在 `driver.seed_start`（root task host-bind 处）调 `provider.allocate(session_root_id)`，把返回的 `SandboxHandle` welded 进 `TaskHostBoundPayload`。
- **`exec_env_ref` 从扁平 `str`（仅 base_url）扩成携带 `sandbox_id`**（兑现 v1 推迟的 D4）。落法二选一【替你定，可否决】：**推荐**保持扁平 `str` 但编码为 `"{base_url}#{sandbox_id}"`（免掉 nested-dataclass 的 canonical tag/register 机器，完全复用现有 `workspace_dir` 的 `__canonical_omit_none__` idiom），adapter 侧拆解。备选：升级为 `{base_url, sandbox_id}` 结构体（更干净但要动 canonical 序列化）。
- **teardown**：per-session 容器**可以**在 root-task terminal + session close 时 `provider.release()`（不再像 v1 因共享而只能 host-shutdown teardown）。挂点：root-task 进 terminal 的 fold 处 + `Client.shutdown` 兜底。
- **reconnect**：resume/reclaim 读 `exec_env_ref` → `provider.attach(ref)` 重连同一 `sandbox_id`；密钥仍取本机 env（D5 复用）。跨 host reclaim：只要该 host 能 docker-attach 到那个容器（同机）或容器可达；跨机 Docker-local 不通 → 记 limitation（K8s/内部服务 backend 才解跨机）。

### D5【已确认】Docker mount 播种 + 「执行仍全经 seam」

- **provider `docker run`**（挂载在 `docker run` 时做，AIO 本身不感知；每条来自 `SandboxSpec.mounts`）：
  ```
  docker run -d --name noeta-sbx-<id> \
    -p 127.0.0.1:<port>:8080 -e SANDBOX_API_KEY=<key> \
    -v <host_workspace>:/workspace \                        # 项目文件, rw
    -v <builtin_skills>:/opt/noeta/skills/builtin:ro \      # noeta 自带 skills
    -v <global_skills>:/opt/noeta/skills/global:ro \        # 用户全局 skills
    --memory 2g --cpus 2 --security-opt seccomp=unconfined \
    ghcr.io/agent-infra/sandbox:latest
  ```
- **storage 层可配置**（不写死）：上面三条挂载是默认集，实际由 `SandboxSpec.mounts: list[MountSpec]` 驱动——可加共享数据/缓存目录，可换 `kind`（Local=`local-path`、Distributed=`nas`/`pvc`）。**同路径原则**：`target` 两族保持一致（Local 换 source 为宿主路径、Distributed 换为 NAS 子目录），上层 exec_env / workspace root 零改。
- **mount 只管 seed + persist**：项目文件进得去、built-in/global skills 容器内可见、workspace 变更经 mount 落回宿主。
- **所有工具执行仍全部经 seam 打进容器（已确认 2026-07-08，定死、非可否决）**（honoring「所有工具调用都在 Sandbox 里执行」）：fs IO 走 `AioSandboxExecEnv`（`/v1/file/*`）、进程走 `/v1/shell/exec`。mount/共享存储使字节共享，routing 经 HTTP 的代价是延迟且在共享存储下 fs-经-HTTP 是冗余——但维护者明确选**语义统一**：agent 的一切工具执行（含 fs）都只经容器，不走宿主侧直连。**排除的备选**（曾考虑、已否决）：fs IO 直连宿主 mount 路径、只有进程/网络进容器（混合 ExecEnv）——更快但破坏「一切在沙箱执行」的统一语义。
- **隔离级别**：进程在容器命名空间 + 容器 FS 只能碰挂进来的目录（非完整 FS 隔离）。写进 known-limitations。
- **存储层 vs 执行层正交**：mount（本地 Docker volume）是**存储层**播种/持久化机制；**NAS 共享存储是它的生产泛化**——宿主与 Sandbox 底层挂同一份网络存储（宿主侧可见、持久、跨机可达），作为 `SandboxProvider` 之后的一个存储/播种 backend（见 D5-NAS）。执行层不变（全经容器）。**前提**：共享存储在宿主与容器挂到**同一路径**（否则要路径翻译），provider 负责保证。
- **skills 目录解析**：sandbox 模式下 built-in/global 指向容器内 mount 点（`/opt/noeta/skills/*`），workspace 层是 `<workdir>/.noeta/skills`；三层 merge 逻辑不变，只是根路径换成容器路径。

### D5-NAS【方向，本轮 non-goal，seam 留位】NAS 共享存储 + TAE 托管 backend

维护者的目标态：**宿主与 Sandbox 底层用同一份 NAS 存储打通，执行仍全在 Sandbox**，且将来切到 **TAE 托管**。这是 D5 本地 Docker volume 的生产泛化，落在 `SandboxProvider` 之后。

**TAE 就是这个目标态的托管实现**（AIO 内部文档 `mount` / `provider` 面证实）：TAE 平台**提供了 OSS 版没有的 control-plane**——「**API 动态创建 Session**」+ per-session `fuse_mount_params` 挂 NAS。两种挂载 scope：
- **静态挂载**（PSM 级，对该 PSM 所有 session 生效）：在 TAE 建/编辑 Sandbox 时配 NAS——映射我们的 provider 级默认 mount。
- **动态挂载**（session 级，`fuse_mount_params`）：动态建 session 时按需挂 NAS——映射我们的 **per-session** mount（`MountSpec{kind=nas}`）。

- **provider 变体** `TaeSandboxProvider`（Distributed 族）：`allocate` = 调 TAE「动态创建 Session」API（`fuse_mount_params` 由 `SandboxSpec.mounts` 里 `kind=nas` 的项生成）；`release` = 销毁 session；`attach` = 按 `sandbox_id`(=session id) 重连（TAE 网关 + NAS 跨机可达，无 Docker-local 的绑机问题）。
- **执行层不变**：所有工具仍经容器（D5），NAS 只改变「存储在哪、谁能看见」，不改「在哪执行」。
- **顺带收益**：NAS 跨机可达 → **解掉 R2「跨机 Docker 重连不通」**。
- **本轮 non-goal**：只做本地 Docker volume（D5）；TAE/NAS provider 留后续。但 seam 现在就要满足三条「零返工」前提（本 spec 已纳入）：(a) `SandboxHandle.auth` 是策略（`StaticApiKeyAuth` 本地 / `JwtBearerAuth` TAE），非静态 env 名；(b) `SandboxHandle.base_url` 支持网关路径前缀，`AioSandboxExecEnv` 拼 URL 用 `base_url + "/v1/..."` 不假设 `host:port`；(c) `MountSpec{kind=nas}` 抽象已不假设本地 volume，TAE 侧翻成 `fuse_mount_params`。

### D6【替你定，可否决】seam widening：沿用构造期字段注入，不上 `ToolContext`

- v1 已确立「exec_env 是工具构造期字段，不进 `ToolContext`」（保 call-site 不变、不碰 stable-prefix）。Tier 2 新增的工具/加载器**沿用同法**，不改 `ToolContext`：
  - `run_skill_script`：`build_skill_script_wiring` 加 `exec_env` 参数，工具加 `exec_env` 字段；FS 读走 `exec_env.read_bytes`，执行走 `exec_env.run_argv`（替掉直接 import 的 `_subprocess.run_argv`）。
  - web pack：`build_web_tools(exec_env=)`；`webfetch`/`web_search` 在 sandbox 模式经容器出网——**机制【替你定，可否决】**：v1 用 `exec_env.run_argv(["curl", ...])` 在容器内发请求（复用现有 IO+进程接口，不给 seam 加 network 方法），响应按原逻辑解析；Local 模式保持 httpx 原路。备选：给 seam 加 `http_fetch` 方法或用 AIO browser——留后续。
  - skill indexer：`SkillIndexer` / `resolve_skill_*` / `skill_content_hash` 接受一个 IO 抽象（exec_env 或其 read/stat/walk 子集），sandbox 模式**经容器读 SKILL.md（已确认，见 D6-Skills）**。
  - workspace 加载器（instructions/environment/shell-allowlist）：加 `exec_env` 参数，sandbox 模式经容器读/写；修掉 v1「读容器路径打宿主 FS」的坏点。
- `ExecEnv` Protocol 按需补方法（如 indexer 需要的 `iterdir`/`stat`），保持深模块（能用 `run_argv`+`read_bytes` 表达的不进接口）。

### D6-Skills【已确认】skill 加载/脚本进容器：保留 noeta SkillIndexer，不用 AIO 原生 skills API

现状：main 的 skill 加载 100% 宿主侧（`SkillIndexer` 裸读宿主 `Path`），sandbox 模式**已坏**（拿容器路径 `/workspace` 读宿主 FS）。目标：skill 的读+执行都落容器。

- **不使用 AIO 原生 skills API**（`/v1/skills/register|metadatas|{name}/content`）——即便 AIO skill 与 noeta skill **同格式**（`SKILL.md`+frontmatter+`scripts/`，Anthropic Agent Skills 约定）。**排除理由**（同 ADR alt #5「不挂 AIO `/mcp`」）：会引入 AIO 的 skill 名/元数据/渲染，**扰动 stable-prefix**，且与 noeta 三层 merge + event-sourced 激活重复。noeta 保留自己的 `SkillIndexer`，只把 IO 挪进容器。
- **skill 目录（容器内，provision 时 mount）**：built-in→`/opt/noeta/skills/builtin`(RO)、global→`/opt/noeta/skills/global`(RO)、workspace→`<workdir>/.noeta/skills`（随 workspace mount）。三层 merge 不变，只换根路径。
- **ref（渲染给模型的 `Base directory for this skill: <path>`）= 容器路径**（如 `/opt/noeta/skills/builtin/foo`）。因为模型随后用 `read`/`run_skill_script` 都在容器里解析该路径；渲染宿主路径在容器内不存在。
- **脚本（`run_skill_script`，S7）**：加 `exec_env` 字段——脚本 hash 校验读走 `exec_env.read_bytes`（容器）、执行走 `exec_env.run_argv`（容器内，cwd=容器 workdir），替掉直接 import 的宿主 `_subprocess.run_argv`。脚本文件本身在容器（mount 进去）。
- **【已确认 2026-07-08：经容器读】** indexer 读 SKILL.md 字节走容器（`exec_env` → `/v1/file/*`），与「一切经容器」一致，路径天然容器化、ref 直接对。接受代价：session 启动时每 skill 一次 HTTP 读（几十个≈几百 ms）。**否决**宿主侧读 mount 源 + 路径翻译（更快但引入宿主/容器路径翻译，破坏统一模型）。ref 与脚本执行本就必须容器侧，此决策把 index 读也统一到容器。

### D7 fencing：per-session 缩小 blast radius，仍 unfenced（v1 立场不变）

per-session 容器让「慢僵尸污染」只波及**它自己会话的容器**（v1 是污染 host 共享容器，波及所有会话）→ blast radius 显著缩小。跨代仍不 fence，`fence_token` 恒 `None` 占位（v2 编排层 generation-token fence 时填）。记 known-limitations（更新 v1 那条）。

### D8 密钥 / addressing / auth 策略（复用 v1 D5 + 为 TAE 泛化）

- **落 log 的只有 addressing**：`base_url + sandbox_id`（+ `workdir`）；durable、可 reconnect。
- **auth 是 live 策略、不落 log、不序列化**（`SandboxHandle.auth: SandboxAuth`）：连接时 `auth.connect_headers()` 生成鉴权 header。v1 `StaticApiKeyAuth`（从 env 读 key → `X-AIO-API-Key`）；TAE `JwtBearerAuth`（本机私钥短时 mint JWT → `Authorization: Bearer`，sandbox 侧 `JWT_PUBLIC_KEY` 验签）。reconnect（含跨机）时 auth 由**本机 config 重建**，密钥/私钥永不入 config/log/event。
- **网关路径前缀已天然支持**（`exec_env.py:412/444` 已 `base_url.rstrip("/") + path`）→ `https://gateway/<prefix>` 零改可用。**唯一要改的**：`AioSandboxExecEnv` 当前把鉴权 header 固定在构造期（静态 key 够用），TAE 短时 JWT 需改成**每次调用** `auth.connect_headers()` 现取。

## Implementation plan

1. **`SandboxProvider` seam（SDK）**：定义 Protocol + `SandboxHandle`/`SandboxSpec`；`SandboxExecEnvManager` 重构为持 provider、按 session_root_id allocate/缓存/release/attach；`HostConfig` 注入 provider（默认 None ⇒ Local 回退）。
2. **`DockerSandboxProvider`（agent）**：`docker run`（mount + api-key + 端口 + 资源限制）/ `docker rm` / attach；健康探测（等 `/v1/sandbox` ready）。
3. **per-session provision 接线**：`seed_start` eager allocate + weld handle 进 `TaskHostBound`；`exec_env_ref` 携带 `sandbox_id`；`_build_engine` 用 handle 造 `AioSandboxExecEnv`。
4. **生命周期**：root-task terminal fold 处 + `Client.shutdown` 调 `provider.release`；reconnect 走 `provider.attach`。
5. **seam widening（Tier 2 工具）**：skill indexer / `run_skill_script` / workspace 加载器 / web pack 全部改道 exec_env（D6）。修掉 v1「加载器打宿主 FS」坏点。
6. **skills 目录容器化**：built-in/global 指向容器 mount 点；三层 merge 用容器路径。
7. **产品激活**：`apps/noeta-agent` 默认可配 sandbox（provider + 镜像 + mount 策略）；文档教怎么开。
8. **文档 + ADR + CONTEXT**：更新 ADR（v1→v2：per-container、SandboxProvider、Tier 2）；known-limitations 更新（mount 隔离级别、idle 成本、跨机 Docker 重连不通）；CONTEXT 加 `SandboxProvider` 术语。

## Task breakdown

| # | 任务 | 层 | 依赖 / 并行 |
|---|---|---|---|
| S1 | `SandboxProvider`/`SandboxAuth` Protocol + `SandboxHandle`/`SandboxSpec`/`MountSpec` + manager 重构；`AioSandboxExecEnv` header 从构造期固定改为**每调用** `auth.connect_headers()`（TAE 短时 JWT 前提）。注：URL 前缀已 OK（`exec_env.py:412/444` 已 `base_url.rstrip+path`），不改 | SDK | 基座，先做 |
| S2 | `LocalDockerSandboxProvider`（run/rm/attach/health + 可配置 mounts） | agent | 依赖 S1 |
| S3 | `exec_env_ref` 携带 sandbox_id（weld/fold/resolve/cache 全链演进） | SDK/runtime | 依赖 S1，镜像 v1 现链 |
| S4 | seed_start eager provision + `_build_engine` 用 handle | SDK | 依赖 S1/S3 |
| S5 | teardown（root-terminal release + shutdown 兜底）+ attach reconnect | SDK/agent | 依赖 S2/S4 |
| S6 | skill indexer 改道 exec_env + skills 目录容器化 | runtime | 依赖 v1 seam；与 S2 并行 |
| S7 | `run_skill_script` 改道 exec_env（FS + run_argv） | runtime | 与 S6 并行 |
| S8 | workspace 加载器（instructions/environment/allowlist）改道 exec_env（修 v1 坏点） | runtime/SDK | 与 S6 并行 |
| S9 | web pack 改道（sandbox 经容器出网 via curl/run_argv） | runtime | 与 S6 并行 |
| S10 | 产品激活（apps/noeta-agent 配置 + 默认） | agent | 依赖 S1–S5 |
| S11 | 文档 + ADR + CONTEXT + known-limitations | — | 收尾，依赖全部 |

## Dependencies / sequencing

- **S1 是缝，先落**；S3（ref 携带 sandbox_id）严格镜像 v1 `exec_env_ref` 现链，单独验收多机语义。
- **S6/S7/S8/S9 是 runtime 侧 seam widening，彼此并行**，只依赖 v1 已有 seam，可与 agent 侧 S2 并行推进。
- **S4/S5 是 per-session 生命周期正确性落点**，依赖 provider（S2）与 ref（S3）。
- **S10 依赖整条链通**；S11 收尾。
- 每一步保持「config 缺省 ⇒ Local 字节等价回退」，任何时候可关。

## Acceptance criteria

1. **零回归**：`HostConfig()` 缺省（无 provider）下全量既有测试绿；老录制 fold/replay 字节级不变（`exec_env_ref` 缺省不改 `TaskHostBound` 字节）。
2. **stable prefix 不变**：接入前后同一 agent 的工具 schema 序列化字节相同。
3. **per-session 隔离**：两个并发 session 各拿**独立容器**（不同 `sandbox_id`），一个会话在容器内的文件/进程副作用对另一个不可见。
4. **Tier 2 全工具进容器**：fs/shell/skill 加载/skill 脚本/workspace 加载器/web 的执行都落在会话容器内；memory 仍在宿主；MCP 仍在宿主。
5. **skill 在容器内跑通**：built-in/global（mount RO）+ workspace 层 skill 都能被 index 到，`run_skill_script` 在容器内执行、cwd 是容器 workdir。
6. **多机/重连**：worker 起 sandbox-X 干活 → kill -9 → 另一进程 fold 回来读 `exec_env_ref` → `attach` 回同一 `sandbox_id`，容器内文件态可见、任务继续（Docker-local：同机重连；跨机记 limitation）。
7. **生命周期**：root-task terminal + session close 后容器被 `release`（`docker rm`）；进程退出兜底 release。
8. **产品激活**：`apps/noeta-agent` 配上 provider 后端到端可用（真容器 e2e，gated `NOETA_TEST_AIO_SANDBOX_URL` 或本地 Docker）。
9. **文档**：ADR 更新为 v2 形态；known-limitations 更新（mount 隔离级别 / idle 成本 / 跨机 Docker 重连不通）；CONTEXT 加 `SandboxProvider`。

## Risks

- **R1 mount 弱隔离**：容器经 mount 直写宿主 workspace，非完整 FS 隔离。缓解=只 mount workspace + skills（不 mount host root）；记 limitation；真隔离走 copy-in/sync-out provider（seam 已留）。
- **R2 跨机 Docker 重连不通**：Docker-local backend 的容器绑在起它的那台机；跨机 reclaim attach 不到。缓解=记 limitation，跨机靠 K8s/内部服务 backend（seam 已留）；**NAS 共享存储 backend（D5-NAS）从存储层解此问题**——文件态跨机可达，换机只需重拉容器挂同一份 NAS。
- **R3 web 经容器 curl 的保真**：`web_search`(Tavily) 等经容器 `curl` 重发，需保响应解析一致。缓解=Local/容器两路共用解析层，仅换 transport；契约测试钉。
- **R4 idle 容器成本**：per-session 容器在会话 suspend（等人/等 timer）期间占资源。缓解=记 limitation；warm pool/pause 留后续。
- **R5 skill 索引路径翻译**：mount 下容器路径 ≠ 宿主路径，base-directory / cwd 必须是容器路径。缓解=统一经容器读（D6），路径不跨界。
- **R6 provision 延迟**：每 session 冷启动一个 AIO 容器（拉镜像/起服务）有秒级延迟。缓解=本地预拉镜像；warm pool 留后续。

## Files / areas to inspect

- SDK：`packages/noeta-sdk/noeta/client/sandbox.py`（manager→provider 重构）、`host_config.py`（provider 注入）、`host.py`（`_build_engine`/`exec_env_ref`/teardown）。
- runtime seam：`packages/noeta-runtime/noeta/tools/fs/exec_env.py`（按需补方法）、`tools/fs/skill_script.py`、`tools/web/fetch.py` + `search.py`、`context/skills/indexer.py`、`execution/skills.py`、`execution/builder.py`（加载器改道 + skills 目录容器化）。
- durable ref 链：`protocols/events.py`、`protocols/task.py`、`core/fold.py`、`execution/resolver.py`、`execution/driver.py`（`seed_start` weld + terminal release）。
- agent：`apps/noeta-agent`（`DockerSandboxProvider` + 生命周期挂载 + 默认配置）。
- 参照：`docs/adr/execution-environment-seam.md`、`docs/implementation-specs/2026-07-07-sandbox-exec-env.md`（v1 现链，逐点镜像）、`multi-host-lease-fencing.md`（D7 边界）、`conversation-rewind-and-file-checkpoint.md`（子任务共享父容器）。
