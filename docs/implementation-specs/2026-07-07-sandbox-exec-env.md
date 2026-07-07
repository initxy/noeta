# Sandbox 执行后端：ExecEnv seam + AIO Sandbox backend（v1：shell+file 隔离）

> 状态：shape 定稿，待 implement。文中标「【替你定，可否决】」的是在维护者说「按推荐来」后我替他定的边界决策，可事后推翻。

## Goal

在 noeta 的 fs/shell 工具**下面**开一个 `ExecEnv` seam，把「文件/进程副作用落在哪里」从工具实现里抽出来：默认 `LocalExecEnv`（今天的行为，宿主 subprocess + `WorkspaceRoot` realpath 围栏，零行为变化），可选 `AioSandboxExecEnv`（把 shell/file 打到一个 AIO Sandbox 容器 `agent-infra/sandbox`，Apache-2.0，单容器 `POST /v1/shell/exec` + `/v1/file/*`）。工具的**对模型契约（name/schema/description）完全不变**，只换执行后端——天然满足 stable-prefix KV-cache 可复现硬约束。

多机语义要求：worker 崩溃 / stale-reclaim 后，另一台机 fold 回来必须**重连同一个 sandbox**（endpoint+id 落进 log，密钥走 host config 不落 log）。

## Non-goals

- **不做 browser / computer-use。** AIO 的 CDP 浏览器、VNC、Jupyter 一律不接；v1 只做 shell + file 隔离。
- **不在 v1 fence sandbox 跨代副作用**（见 Decisions D1）。编排层 generation-token fence 明确留给 v2。
- **不做 sandbox 集群编排。** v1 假设有一个可达的 provisioning 权威（单 backend 进程 provision）；「多个 backend 主机共享一个 sandbox 池」的分布式调度不做。跨机**重连**做（靠 log 里的 ref），跨机**provision 拓扑**不做。
- **不做 sandbox 内的 background shell**（`run_in_background`）。AIO 无持久 job handle，v1 在 sandbox 模式下 `run_in_background=True` 直接返回清晰的 "not supported in sandbox (v1)" 错误（见 D5）。
- **不做 sandbox 的 pause/snapshot / idle 复用。** AIO 无此能力；v1 容器存活绑 root-task 生命周期，idle 成本记入 known-limitations（见 D6）。
- **不改 EventLog 事件字节、不改 fencing ADR 的 D1–D3、不动 Dispatcher/Engine 主循环。**

## Context

- **`WorkspaceRoot`（`noeta.tools.fs._workspace`）只是路径围栏，不是 sandbox。** 它对宿主做 `os.path.realpath` containment，docstring 明确写「a tool that invokes external processes (shell_run) can still touch the rest of the filesystem」。所以本 seam 是**另一层**，位于工具与真实 IO 之间。
- **"Workspace" 一词已被占两次**：一是 first-class 注册实体 `{id, name, path}`（agent 层，`~/.noeta/workspaces.json`，见 `workspace-and-session-path.md`），二是 `WorkspaceRoot` 路径围栏。故新 seam **另起名 `ExecEnv`**，避免三重重载。【替你定，可否决：名字】
- **session 路径模型**（`workspace-and-session-path.md`）：一个 session pin 一个绝对路径，写进 `TaskHostBoundPayload.workspace_dir`，resume 只读这个路径、不碰 registry；`provider` 只把**名字**落 log，密钥/连接实例不入 log。**本设计的 sandbox ref 完全复用这个 pattern**（addressing 落 log，secret 走 host config）。
- **fencing ADR（`multi-host-lease-fencing.md`）的核心前提**（第 42 行）：「系统里没有任何依赖 lease currency 正确性的、位于共享 Postgres 之外的写」，所以不用 epoch/fencing token。**sandbox 恰好打破这个前提**——它是 Kleppmann 边界上的外部资源，ADR alternative #1 已预告「一旦有写落到共享 DB 之外，epoch 才变 load-bearing」。这就是 D1 要正面处理的点。
- **worker 一次只租一段**（`worker-lease-model.md`）：租到 → 推进到下一个 suspend/terminal → 释放。工具调用必须在一个 lease 内完成（heartbeat 续租，硬上限 1h）。→ sandbox 里的 foreground shell 天然受此约束；这也是 v1 敢砍 background 的底气。
- **file checkpoint / rewind**（`conversation-rewind-and-file-checkpoint.md`）：在 `ToolRuntime.invoke` 的 `_capture_file_baselines` choke point 读「编辑前内容」存 ContentStore，rewind 时写回**磁盘**。sandbox 模式下「磁盘」= 容器 FS，故 capture 的读 + rewind 的写都要走 ExecEnv（见 D7）。
- **注入位已现成**：`ToolRuntime.__init__` 已按同一模式注入 `background_runner` / `file_checkpoint_registry`，`invoke` 构造 `ToolContext(artifact_store, metadata, background_runner)`。`ExecEnv` 顺着加一个即可。`HostConfig`（`noeta.client.host_config`）是 frozen dataclass、每字段「absent 默认 = 今天行为」，加一个 sandbox 配置字段零破坏。

## Decisions

### D1【核心，已选定】sandbox 跨代副作用：v1 接受 at-least-once、不 fence，落 known-limitations

被 D1(in-tx FOR SHARE) fence 掉 EventLog 的僵尸 worker，仍可能对同一 sandbox 发 `exec`/`file` 写——不经 Postgres 事务，D1 管不到；AIO 又无 session 接管机制。v1 的立场：

- **sandbox 副作用 = 外部 side effect，at-least-once，不 fence**，与现有「crashed step 副作用只上报不回滚」（`step-attempt-recovery.md` / limitations）**同一档**。
- reclaim 后新 worker 重连同一容器继续干；**慢僵尸（GC pause/SIGSTOP 复活）污染容器**的窗口，靠 step-attempt 重驱 + 人工 review 兜底，并**写进 known-limitations**。
- **v2 洞（预告好的）**：编排层 generation-token fence——sandbox manager 持 per-task generation token，`exec`/`file` 带 token，AIO 前挂受控 proxy 校验，stale-reclaim 时 rotate。这正是 fencing ADR 说的「epoch 变 load-bearing」时机。**v1 不做，但接口预留**（ExecEnv 的连接握手带一个 opaque `fence_token: str | None`，v1 恒 `None`，v2 填 generation token；这样 v2 不改 seam 形状）。

> 这是一个「未来会被追问原因」的长期取舍（「为什么 sandbox 不像别的写一样被 fence？」）→ **应写一条 ADR**（`execution-environment-seam.md`），把 D1/D2 的取舍固化，并显式链回 `multi-host-lease-fencing.md` 的 alternative #1。

### D2 seam 形状：`ExecEnv` Protocol，顺 `ToolContext` 注入，config 走 `HostConfig`

- **新 Protocol `ExecEnv`**（落 `noeta.protocols`），最小深接口（deep module）：
  - `run_shell(command: str, *, cwd: str, timeout_s: float, env: Mapping[str,str] | None) -> ShellResult`（foreground；`ShellResult = {exit_code, stdout, stderr, truncated}`）
  - `read_file(path: str) -> bytes`
  - `write_file(path: str, body: bytes) -> None`
  - `resolve(path: str) -> str`（containment/规范化，返回后端内规范路径；越界抛 `WorkspaceEscape`）
  - `close() -> None`（生命周期收尾）
  - `glob` / `grep` / `list_dir` **不进接口**——由 fs 工具在 ExecEnv 之上用 `run_shell`（`rg`/`find`）+ `read_file` 表达，保持接口小。
- **注入**：`ToolRuntime.__init__` 加 `exec_env_resolver: Callable[[task_id], ExecEnv] | None`（`None` ⇒ 构造 `LocalExecEnv`，零行为变化）；`invoke` 把解析出的 `ExecEnv` 放进 `ToolContext.exec_env`。fs/shell 工具从 `ctx.exec_env` 取，不再直接 `os`/`subprocess`。
- **config**：`HostConfig` 加 `exec_env: ExecEnvConfig | None = None`（absent ⇒ Local）。`ExecEnvConfig` 是 sdk 层可构造的**纯配置结构**（如 `SandboxExecEnvConfig(base_url: str, api_key_env: str = "SANDBOX_API_KEY", provision: "eager"|"attach")`），**runtime 的 builder 据此实例化 adapter**——backend 只填 config、不 import adapter，import-linter 围栏不破（backend 只能 import `noeta.sdk`）。

### D3 分层落点

- **`ExecEnv` Protocol + `LocalExecEnv` + `AioSandboxExecEnv` adapter → runtime**（`noeta.runtime.exec_env`）。adapter-to-external-service 属 adapter 层，等同 `noeta.storage.postgres`。
- **sandbox manager / provision / teardown / 生命周期 → agent 层**（`noeta.agent.backend`，等同 workspace registry 的「allocation & management 在 agent 层」）。它经 `HostConfig.exec_env` 把配置传进来，runtime builder 造 adapter，manager 负责真正拉起/销毁容器并把 ref 交给 runtime。

### D4 sandbox 绑定粒度 + ref 落 log【替你定】

- **一个 sandbox 绑一个 root-task 树**（subtasks 按 rewind ADR 共享父 cwd/磁盘 → 必须共享同一容器）。key = session-root task id（复用 `ToolRuntime._session_root` 已有的 root 解析）。
- **eager provision**：在 root task 起步时（host-bind 处）就 provision，**ref welded 进 `TaskHostBoundPayload`**（新增可选 `exec_env_ref: {base_url, sandbox_id} | None`），复用现成 weld+fold 路径，**不新增事件类型**。Local 模式 `exec_env_ref=None`，`workspace_dir` 照旧 → 老录制字节不变、fold 不变。
  - 选 eager 而非 lazy：coding agent 几乎必 shell，省掉「lazy 首次 provision 要新开 `ExecEnvBound` 事件」的复杂度。
- **密钥不落 log**：log 只存 `base_url + sandbox_id`（addressing）；`SANDBOX_API_KEY` 连接时从 host config / env 取，等同 provider 只落名字。
- **重连**：resume/reclaim 读 `exec_env_ref` → `AioSandboxExecEnv` 以 `provision="attach"` 连上同一 `sandbox_id`。任何主机可达该 URL 即可重连 → 满足「另一台机 fold 回来重连同一 sandbox」。

### D5 background shell【替你定：v1 砍】

sandbox 模式下 `shell_run(run_in_background=True)` **直接返回清晰错误**（"background shell not supported in sandbox mode (v1)"）。理由：AIO 无持久 job handle，noeta 现有 background 子系统（host `ProcessRegistry` + growable artifact + PID 恢复，见 `shell-permission-and-background.md`）全是为宿主造的，改造成对容器 durable job 是独立的 v2 工程。foreground shell 受 lease 内完成约束（已有），够 v1。

### D6 idle 成本【替你定：v1 认了】

AIO 无 pause/snapshot。v1：容器存活绑 root-task active 生命周期，**root-task terminal + session close 时 teardown**（与 background-shell 的 session-lifetime teardown 对齐）；**不做 idle reaper**（reap 会丢容器 FS 态，反而制造惊讶）。**idle 容器成本写入 known-limitations**，snapshot/pause 留 v2。

### D7 两套围栏不打架 + rewind 走 ExecEnv【替你定】

- **sandbox 模式下容器即隔离边界**，`LocalExecEnv` 的宿主 `os.path.realpath` 围栏在 `AioSandboxExecEnv` 里**不再跑**（没有宿主路径可 realpath）；改为**容器内相对路径规范化**：`resolve` 把 path 收敛进容器工作目录、拒绝 `..` 越顶（整洁性，不让模型乱逛容器 `/`）。安全保证从「realpath 围栏」转为「容器隔离」。**不双重围栏。**
- **rewind**：`_capture_file_baselines` 的读 + file_checkpoint restore 的写回，改走 `ctx.exec_env.read_file/write_file`（今天走 `os`）。因 edit/read 已在同一 choke point 过 ExecEnv，rewind **无需特判即可工作**；baseline 内容照旧存 ContentStore（从容器 `/v1/file/read` 拉）。

### D8 产出物 / stdout 回 ContentStore

`run_shell` 返回的 stdout 照旧 offload 进 ContentStore（同今天）；工具产出的文件经 `read_file`（→ `/v1/file/read`）拉回存 Artifact（ContentRef）。纯 plumbing，无新机制。

### D9 MCP 打样路径（可选，throwaway）

AIO 暴露 `/mcp`。**第 0 天可选**：用现成 live-MCP resolver 把 AIO 的 `/mcp` 挂上验证「容器能跑通」，**一次性验证、非本设计**——它会引入 AIO 的 MCP 工具名/schema、扰动 stable prefix、与内建 fs/shell 重叠，不能当终态。

## Implementation plan

1. **Protocol + Local 后端（零行为变化基线）**：定义 `ExecEnv` / `ShellResult`（`noeta.protocols`）；实现 `LocalExecEnv`（`noeta.runtime.exec_env`）把今天的 `WorkspaceRoot` + `_subprocess` + 文件读写原样包进去；`ToolRuntime` 加 `exec_env_resolver` 注入，`ToolContext` 加 `exec_env`；fs/shell 各工具（read/glob/grep/edit/write/patch/shell）改从 `ctx.exec_env` 取 IO。**验收**：全量既有测试绿（Local 模式字节级不变）。
2. **AIO adapter**：`AioSandboxExecEnv`（`run_shell`→`/v1/shell/exec`，`read/write_file`→`/v1/file/*`，`resolve`=容器内路径规范化，`fence_token` 占位恒 `None`）；HTTP transport 可注入（同 `mcp_http_post`/`otlp_http_post` pattern，测试塞 fake）。
3. **config + 分层接线**：`HostConfig.exec_env: ExecEnvConfig | None` + sdk 层 `SandboxExecEnvConfig`；runtime builder 据 config 造 adapter；agent 层 `noeta.agent.backend` 加 sandbox manager（provision/attach/teardown）。
4. **ref 落 log + 重连**：`TaskHostBoundPayload.exec_env_ref` 可选字段 + weld/fold；resume/reclaim 读 ref → attach 同容器。
5. **rewind 走 ExecEnv**：`_capture_file_baselines` + file_checkpoint restore 的 IO 改道 `ctx.exec_env`。
6. **边界处理**：sandbox 模式 `run_in_background=True` 清晰报错（D5）；teardown 挂 root-task terminal + session close（D6）。
7. **文档 + ADR**：known-limitations 增「sandbox 跨代副作用未 fence」「idle 容器成本」两条；新 ADR `execution-environment-seam.md`；CONTEXT.md 加 `ExecEnv` 术语。

## Task breakdown

| # | 任务 | 可并行？ |
|---|---|---|
| T1 | `ExecEnv`/`ShellResult` Protocol + `ToolContext.exec_env` + `ToolRuntime.exec_env_resolver` 注入 | 基座，先做 |
| T2 | `LocalExecEnv` 包住今天行为 + 各 fs/shell 工具改道 `ctx.exec_env` | 依赖 T1 |
| T3 | `AioSandboxExecEnv` adapter（含可注入 HTTP transport + fake） | 依赖 T1，与 T2 并行 |
| T4 | `HostConfig.exec_env` + `SandboxExecEnvConfig` + runtime builder 实例化 | 依赖 T1/T3 |
| T5 | agent 层 sandbox manager（provision/attach/teardown 生命周期） | 依赖 T4 |
| T6 | `TaskHostBoundPayload.exec_env_ref` weld/fold + resume/reclaim attach | 依赖 T4，是多机重连的关键 |
| T7 | rewind（file_checkpoint capture/restore）改道 ExecEnv | 依赖 T2 |
| T8 | 边界：background 报错 + teardown 挂生命周期 | 依赖 T5 |
| T9 | 文档 + ADR + CONTEXT term + known-limitations | 收尾，依赖全部 |

## Dependencies / sequencing

- **T1 → T2 是零行为变化重构**，必须先落且全测试绿，才谈 sandbox（这是回滚保险：任何时候 config 缺省都回到 Local）。
- **T3 可与 T2 并行**（一个包旧行为、一个写新 adapter，接口 T1 已定）。
- **T6 是「多机重连」正确性的落点**，依赖 T4 的 ref 结构；单独验收。
- **T7（rewind）依赖 T2**（choke point 已改道后顺带）。
- T9 收尾，需要 T1/T5/T6 的最终形状。

## Acceptance criteria

1. **零回归**：`HostConfig()` 缺省（Local 模式）下全量既有测试绿；老录制 fold/replay 字节级不变（`exec_env_ref=None` 不改 `TaskHostBound` 字节）。
2. **stable prefix 不变**：接入前后，同一 agent 的工具 schema 序列化字节相同（sandbox 只换执行后端，不动 name/schema/description）。
3. **sandbox 跑通**：配 `SandboxExecEnvConfig` 后，`shell_run` / `read` / `write` / `edit` / `apply_patch` / `glob` / `grep` 的副作用落在 AIO 容器内、宿主 FS 不受影响；产出经 ContentStore 存为 Artifact。
4. **多机重连**（核心）：worker 起 sandbox-X 干活 → kill -9 → 另一进程/主机 fold 回来，读 `exec_env_ref` **attach 回同一 sandbox-X**（同一 `sandbox_id`），容器内既有文件态可见、任务继续。集成测试用两个 dispatcher 实例共享一个 DSN（复用 fencing 契约测试的多机夹具）+ 一个真实 AIO 容器（gated env `NOETA_TEST_AIO_SANDBOX_URL`）。
5. **rewind under sandbox**：sandbox 模式下对 AI 编辑过的文件做 rewind，写回落在容器内、恢复正确。
6. **边界明确**：sandbox 模式 `run_in_background=True` 返回清晰不支持错误；root-task terminal + session close 后容器被 teardown。
7. **known-limitations** 增两条（跨代副作用未 fence、idle 成本）；ADR + CONTEXT `ExecEnv` 术语落地。

## Risks

- **R1（已知并接受）跨代副作用未 fence**：慢僵尸污染容器。缓解=step-attempt 重驱 + 人工 review；v2 编排层 token fence（接口已留 `fence_token` 占位）。
- **R2 AIO API 契约漂移**：`/v1/shell/exec` 等是 v1 文档面，字段可能演进；用可注入 HTTP transport + 一层薄 adapter 隔离，契约变只改 adapter。
- **R3 provision 拓扑**：v1 单 backend provision；多 backend 主机共享池未做——若真上多机 backend，provision 权威要单独设计（已列 non-goal，别默默扩散）。
- **R4 idle 容器成本**：长 suspend（等人/等 timer 数小时）占着容器。v1 记 limitation；v2 pause/snapshot 或 idle reaper。
- **R5 grep/glob 经 `run_shell` 依赖容器内有 `rg`/`find`**：AIO 镜像需自带（大概率有）；adapter 里探测缺失时优雅降级或明确报错。

## Implementation notes (2026-07-07 — T1→T2 landed)

T1→T2（零行为变化基座）已实现并验证（全量 3003 passed / 0 fail、import-linter 16 kept 0 broken、mypy/ruff clean、schema snapshot 不变）。落地时对上文 3 处做了**实现细节修正**（终态不变，记录供 T3 续接）：

1. **落点从 `noeta.runtime` 改到 `noeta.tools.fs.exec_env`。** import-linter 拓扑里 `noeta.tools` 属 materials band、**高于** `noeta.runtime`（kernel-services band），kernel 不得 import material → `LocalExecEnv`（包 `WorkspaceRoot`/`run_argv`）只能与它们同层。D3 原写的「Local/AIO → runtime」作废；`AioSandboxExecEnv`（T3）同样落 `noeta.tools.fs`。
2. **seam 是 IO-only，经工具构造期字段注入，不走 `ctx.exec_env`。** 现有架构里 `workspace` 本就是工具的构造期 `WorkspaceRoot` 字段（`build_fs_tools`/`_stage_fs_pack` per-spec 构造），故 ExecEnv 镜像它：每个工具新增 `exec_env: ExecEnv = field(default_factory=LocalExecEnv)`，默认即今天行为（95 处 `Tool(workspace=…)` 测试构造零改动）。**路径解析（resolve/relative/root/skill_roots）保留在 `WorkspaceRoot` 不动**；只有真正的 IO（read/write/stat/walk/run_argv）走 `self.exec_env`。ToolRuntime/ToolContext **未改动**。→ T3 的 D7「容器即围栏」通过让 sandbox 的 `WorkspaceRoot` 根指向容器工作目录（词法容器化）实现，IO 经 `AioSandboxExecEnv` 走远端。→ T5/T6 的 per-task sandbox 绑定：`_stage_fs_pack` 本就 per-task-spec 跑，届时据 `exec_env_ref` 构造 per-task 的 `AioSandboxExecEnv` 传入 `build_fs_tools`（需给 `build_fs_tools` 加一个 `exec_env` 可选参数，T1→T2 未加、用默认）。
3. **两处 IO 暂留 inline，留 T3 路由**：(a) `apply_patch` 的原子 `create`（`os.open O_EXCL`/`_write_all`/`os.close`，带 open→none / write·close→delete 的分级 recovery）——与 AIO 单次 `file/write` API 真实失配，且测试在 `os.open/write/close` 层 monkeypatch 依赖精确 reason；T3 定义 sandbox 的 create 恢复契约（可能引入 3 个 typed 异常）时再路由。(b) shell allowlist 文件（`.noeta/shell-allowlist.json`）的 `read_text/write_text/mkdir`——属 governance config，non-goal 已排除，sandbox 下如何处理留后续。ExecEnv 接口当前**只含实际被调用的方法**（read_bytes/read_text/write_bytes/unlink/exists/is_file/is_dir/is_symlink/glob/rglob/run_argv），`create_exclusive`/`mkdir` 待 T3 需要时再加，避免死代码。

## Files / areas to inspect

- `packages/noeta-runtime/noeta/tools/fs/` — `read.py` / `edit.py` / `write.py` / `patch.py` / `shell.py` / `_subprocess.py` / `_workspace.py`（改道 `ctx.exec_env`）。
- `packages/noeta-runtime/noeta/runtime/tool.py` — `ToolRuntime.__init__`（加 `exec_env_resolver`）/ `invoke`（构造 `ToolContext.exec_env`）/ `_capture_file_baselines`（rewind 读改道）。
- `packages/noeta-runtime/noeta/runtime/file_checkpoint.py` — restore 写回改道。
- `packages/noeta-runtime/noeta/runtime/exec_env.py`（**新增**）— `LocalExecEnv` / `AioSandboxExecEnv`。
- `packages/noeta-runtime/noeta/protocols/` — `tool.py`（`ToolContext.exec_env`）、新 `exec_env.py`（`ExecEnv`/`ShellResult`）、`events.py`（`TaskHostBoundPayload.exec_env_ref`）。
- `packages/noeta-sdk/noeta/client/host_config.py` — `exec_env` 字段 + `SandboxExecEnvConfig`；`host.py` builder 实例化 adapter。
- `apps/noeta-agent/**`（`noeta.agent.backend`）— sandbox manager（provision/attach/teardown）+ 生命周期挂载。
- `packages/noeta-runtime/noeta/execution/` — `driver`/`resolver`（`exec_env_ref` weld/fold；对齐 `workspace_dir` 的现有写法）。
- 参照 ADR：`workspace-and-session-path.md`（ref 落 log pattern）、`multi-host-lease-fencing.md`（D1 与外部资源边界）、`shell-permission-and-background.md`（background 为何 v1 砍）、`conversation-rewind-and-file-checkpoint.md`（rewind choke point）、`step-attempt-recovery.md`（副作用兜底立场）。
- 新增 ADR：`docs/adr/execution-environment-seam.md`；CONTEXT.md 加 `ExecEnv` 术语；`docs/operations/limitations` 加两条。
