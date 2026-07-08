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

## Implementation notes (2026-07-07 — T3 landed: `AioSandboxExecEnv`)

T3（AIO adapter）已实现并单测（fake-transport，29 tests；全量 fs 套件绿、import-linter 16 kept 0 broken、mypy/ruff/naming clean）。落点 `noeta.tools.fs.exec_env`（与 `LocalExecEnv` 同文件）。要点与对 spec 的实现决策：

1. **注入式 HTTP transport**：`AioHttpPost = Callable[[url, json_bytes, headers], bytes]`（镜像 `otlp_http_post` 形状），默认走 stdlib `urllib`；测试塞 fake、永不开 socket。**真容器 end-to-end 仍 gated**（`NOETA_TEST_AIO_SANDBOX_URL`，本轮无容器不可跑）——单测只钉「线上契约形状」，契约漂移 = 单文件改（R2 隔离层就在这个 adapter）。
2. **wire 映射（都锁在 adapter 内）**：`run_argv`→`/v1/shell/exec`，command = `cd <shlex.quote(cwd)> && shlex.join(argv)`（cwd 走词法 `cd`，不赌未证实的请求字段）；AIO 把 stdout+stderr 合成单个 `output` → 落 `stdout`、`stderr` 恒空。**字节保真**：read 请求 `encoding=base64`→解码、write 送 base64（edit/patch 靠字节做 TOCTOU hash，这是最契约敏感的一处）。`glob/rglob` 用 shell `globstar`（`rglob(pat)=glob("**/"+pat)`，与 pathlib 定义一致，依赖镜像有 `bash`+globstar，R5）；stat 走 `test -e/-f/-d/-L` 读 exit_code；`unlink` 走 `rm`。
3. **错误映射**：`success=false` 的 `data.error_type` → stdlib `OSError` 子类（not_found→`FileNotFoundError`、permission_denied→`PermissionError`、already_exists→`FileExistsError`，其余→`AioSandboxError(OSError)`），令工具的 `except OSError` 分支 backend 无关。transport 故障归一到 `AioSandboxError`；`TimeoutError` 透传给 `run_argv` 以标 `timed_out`。`run_argv` 的远端故障**报成 failed run（returncode=-1）而非崩 worker**，镜像本地 run_argv 从不让 spawn 故障逃逸。
4. **patch-create 已路由**（兑现 T1→T2 note #3(a)）：新增 `ExecEnv.create_exclusive(path, body)` + 3 个 typed 异常 `ExclusiveCreate{Exists,Failed,WriteFailed}(OSError)`，各带 `.recover`(`none`/`delete`) + `.reason`（逐字保留旧 inline 文案）。`LocalExecEnv.create_exclusive` = 原样搬来的 `os.open O_EXCL`/`_write_all`/`os.close` dance（patch 测试 monkeypatch 的 `os.open/write/close` 点不变，28 tests 全绿）；`AioSandboxExecEnv.create_exclusive` 用 `set -C; : > path` noclobber 门 emulate 独占（AIO 无 O_EXCL），门赢再 base64 写体；写体失败→`WriteFailed`(recover=delete)。`patch.py` 的 create 分支收敛成 `except ExclusiveCreateError → self._fail(recover=exc.recover, reason=exc.reason)`；`_write_all` 迁入 `exec_env.py`，patch.py 去掉 `os`/`contextlib` import。
5. **`timeout_s`（run_argv 的工具超时）v1 不硬杀远端**：AIO exec 无 hard-kill，adapter 的 HTTP `timeout_s`（构造期）是唯一边界；真正的界靠 lease heartbeat + 1h cap（D1/limitations）。`fence_token` 恒 `None` 占位、v1 不发 fence header（D1，v2 stale-reclaim 时 rotate）。
6. **仍待 T4**：`AioSandboxExecEnv` 目前只能手工构造——尚无 config 入口、且 `WorkspaceRoot` 仍是宿主 `realpath` 围栏（sandbox 需要**词法** `WorkspaceRoot`：根指向容器 workdir、拒 `..` 越顶、不碰宿主 FS）。**做完 T4 才「用得上」**。shell allowlist 文件（note #3(b)）仍 inline，未决。

## Implementation notes (2026-07-07 — T4 landed: config + tool-builder wiring)

T4 把 seam 接到「config + tool builder」这条线，令 sandbox backend **可达**（全量 3050 passed / 0 fail、import-linter 16 kept 0 broken、mypy 无新增错误、ruff/naming clean、schema snapshot 不变）。**per-task provision/attach 的生命周期仍是 T5/T6**——T4 只铺「拿到一个 `ExecEnv` 后如何把它接进工具装配」的可达 seam，不含「谁去 provision 容器」。

1. **词法 `WorkspaceRoot.for_container(dir)`**（`_workspace.py`）：新增 `lexical: bool = False` 字段（默认 False = 今天宿主 `realpath` 行为，所有既有构造字节等价）。`for_container` 只做 `os.path.normpath`（不碰宿主 FS、不查存在性、要求绝对路径），`resolve` 在 `lexical` 时用 `normpath` 收敛 `..`/`.` 而非 `realpath`——容器即隔离边界（D7），这层只做整洁性围栏。
2. **`build_fs_tools(exec_env=None)`**（`fs/__init__.py`）：`None` ⇒ 建一个共享 `LocalExecEnv`（无状态、doc 明示可共享，与各工具自带 `default_factory` 字节等价）；非 None ⇒ 整个 pack 走注入的 backend。不改任何工具的 name/schema/description → stable prefix 不动。
3. **`build_session_inputs(exec_env=None)` → `_stage_fs_pack`**（`execution/builder.py`）：镜像 `app_gateway` 这类「wiring-only runtime injection」的落法（`_BuildSpec` 加字段、inert 默认、非 session identity）。`spec.exec_env is None` ⇒ `WorkspaceRoot.from_path`（宿主）；非 None ⇒ `WorkspaceRoot.for_container`（词法容器根）——即「有远端 executor ⟺ workspace 是容器路径」。`test_default_host_byte_equal_to_direct_builder` 仍绿（exec_env 不入 schema）。
4. **`SandboxExecEnvConfig` + `HostConfig.exec_env`**（sdk `host_config.py`）：纯 addressing 配置（`base_url` / `api_key_env` / `provision`），`resolve_api_key()` 只在连接时读 env（密钥不入 config/log，D5）。**adapter 工厂不落 tools 层**（会违反 import-linter：`noeta.tools` 不得 import `noeta.client`）——由持有 config 的 host（`noeta.client`/`noeta.agent.host`，在 tools 之上）读 env + 造 `AioSandboxExecEnv` + 传给 `build_session_inputs(exec_env=…)`，这步是 T5/T6。
5. **修了 T1→T2 漏掉的一处宿主 stat**：`read`/`edit` 的存在性检查走共享 helper `resolve_{readable,existing}_file`（`tools/_invocation.py`），其 `resolved.is_file()` 之前**直打宿主 FS**、绕过 seam——sandbox 下必失败。已给两个 helper 加 `exec_env` 参数（默认 `None`→宿主，字节等价），`read.py`/`edit.py` 传 `self.exec_env`。**审计残留**（grep 确认）：`shell.py` 的 allowlist 文件 `read_text`（note #3(b)，governance config、non-goal）+ `skill_script.py` 的 `read_bytes`（skill 脚本，非核心 fs pack）仍走宿主，sandbox 下如何处理留后续。
6. **补测**：`test_exec_env_wiring.py`（18）覆盖词法 workspace / build_fs_tools 注入 / config / build_session_inputs 可达 seam（含用 fake `RecordingExecEnv` 证明 read 真流经 backend）；`test_shell_run_foreground.py`（4）真跑前台 shell 断言 exit_code+stdout 内容（补 handoff 额外行动项——变异 `run_argv.stdout` 会让其中 3 个失败，证明真咬 seam）。

## Implementation notes (2026-07-07 — T5 landed: host-layer sandbox manager)

T5 把「拿到 config → 造 live backend → 喂进 `build_session_inputs`」这条线接通，令 sandbox backend **真运行**（全量 3066 passed / 0 fail、import-linter 16 kept 0 broken、mypy 无新增错误〔host.py/client.py 那 3 个 `content_hashes`/`InteractionDriver` 报错 stash 对比确认 pre-existing、仅行号平移〕、ruff/naming clean、schema snapshot + `default_host_byte_equal` 仍绿）。落点与对 spec 的实现决策：

1. **manager 落 `noeta.client`（非 D3 原写的 `noeta.agent.backend`）**：新 `noeta/client/sandbox.py::SandboxExecEnvManager`。唯一必须收到 live backend 的调用点是 `SdkHost._build_engine → build_session_inputs`，而 `SdkHost` 在 `noeta.client`；让 host 直接持有 manager（像 `_process_registry`）比从产品层往下多穿一个 injected callable 干净，且 import-linter 依旧 clean（`noeta.client` 在 `noeta.tools` 之上，可 import `AioSandboxExecEnv`）。这兑现 T4 note #4「由持有 config 的 host 造 adapter」。
2. **【偏离 D4，必读——影响 T6】v1 = 每 host 单容器共享，不做 per-root 键控。** D4 理想是「一个 sandbox 绑一棵 root-task 树，key = session-root task id，host-bind 处 eager provision，ref welded 进 `TaskHostBoundPayload`」。但：(a) 那个 per-root ref 的 weld/fold + reconnect **本就是 T6**，也是**唯一**能拿到 per-root key 的点——写 `TaskCreated` 的 **seed engine `task_id=None`**、且它与首个 driving turn **共享 engine cache**（key 不含 task_id，见 `resolver._engine_for_agent`），若只在 `_build_engine` 里按 root 切 backend 会被缓存**静默旁路**（seed 建了 Local、drive 复用 Local → sandbox 失效）。(b) AIO v1 用的 API 面（`/v1/shell/exec` + `/v1/file/*`）**无建容器接口**，「集群编排」是 non-goal，v1 的 `base_url` 实际就寻址**一个**外部容器。故 T5：manager lazily 建**一个**共享 `AioSandboxExecEnv`，seed 与每个 driving turn 拿**同一个**——既满足「subtasks 共享父容器」（全都共享），又消掉 seed/drive 缓存碰撞。**代价（记 v1 known-limitation，T9 写）**：同一 host 上两个并发 session 会共用一个容器工作目录，无 per-root 隔离——per-root 隔离随 T6 的 per-root provision 到来。
3. **`SandboxExecEnvConfig` 加 `workdir: str = "/workspace"`**（`host_config.py`）：容器工作目录。sandbox 模式下宿主 `workspace_dir` 在容器内无意义，`_build_engine` 用 `workdir` 覆盖它，成为**词法容器 `WorkspaceRoot` 的根**（D7）。纯 addressing，与其它字段同性质。（per-session workspace 子目录 under sandbox 留 T6/v2。）
4. **`SdkHost` 接线**：加公开字段 `exec_env: Optional[SandboxExecEnvConfig]`（Client 从 `hc.exec_env` 穿入）+ `_sandbox` runtime accelerator（`__post_init__` 仅当 config 非 None 才建 manager，否则 None ⇒ 本地路径字节不变）。`_build_engine`：`if self._sandbox: session_exec_env = self._sandbox.exec_env(); workspace_dir = Path(self._sandbox.workdir)`，再把 `exec_env=session_exec_env` 传进 `build_session_inputs`。`_build_orchestration_engine`（`__workflow__` 子）**不接** sandbox——它 `allowed_tools=()` 无 fs 工具，其 worker 各自经 `_build_engine` 走 sandbox。
5. **teardown seam（部分兑现 D6，全量挂载是 T8）**：`SandboxExecEnvManager.teardown()`（`eager` best-effort `close()`；`attach` 只丢 handle 不 close——容器归 provision 方所有）+ `SdkHost.teardown_exec_env()` + `Client.shutdown()` 调它（进程退出时收容器连接，防 idle 泄漏）。**root-task terminal 处的 per-root teardown = T8**（v1 单共享容器下按 root teardown 会误伤别的 root，正确地留到 per-root 容器存在时）。
6. **补测**：`test_sandbox_host_wiring.py`（12）——manager lazy/shared/idempotent-teardown/eager-vs-attach close；SdkHost 默认 = LocalExecEnv+宿主根（回归）；配 config ⇒ fs 工具走容器 backend + 词法容器根 = `workdir`；真 `AioSandboxExecEnv`（无 socket）；read 端到端流经 fake backend；**seed（`resolve_engine_for_agent`, `task_id=None`）也路由同一 backend**（钉 #2 的缓存碰撞安全性）；Client 穿 config + shutdown 收 backend。fake factory 经 monkeypatch `sandbox._default_factory` 注入，永不开 socket。

## Implementation notes (2026-07-07 — T6 landed: durable exec_env_ref + multi-machine reconnect)

T6 把「容器地址随 session 落 log → 另一台机 fold 回来重连同一容器」这条多机语义打通（全量 3070 passed / 0 fail、import-linter 16 kept 0 broken、protocols mypy `--strict` 干净、改动文件 mypy 无新增错误〔driver/resolver 那 9 个 `ResidentHost`/`EngineProtocol` 报错 stash 行号归一 diff 确认 pre-existing〕、ruff/naming clean、schema snapshot + `default_host_byte_equal` 仍绿）。落点严格镜像现有 `workspace_dir` 的 weld→fold→resolve→cache-key 全链（「prefer existing patterns」）：

1. **`exec_env_ref` = 扁平 `Optional[str]`（容器 `base_url`），非 spec 的 `{base_url, sandbox_id}`。**【偏离 D4，续 T5 note #2】理由：T5 已定「v1 每 host 单容器、由 `base_url` 寻址」，`sandbox_id` 要到 v2 真编排 mint per-container id 才独立于 `base_url`；v1 记 `base_url` 就是**重连地址 + 唯一 load-bearing 部分**。用扁平 `str` 完全对齐 `workspace`（同类型、同 `__canonical_omit_none__` idiom、cache key 同构），且**免掉** nested-dataclass 的 canonical tag/register/restore 机器。`sandbox_id` 需要时从 `base_url` 派生（日志）。记 v1 known-limitation（T9）。
2. **durable 链（逐点镜像 `workspace_dir`）**：`TaskHostBoundPayload.exec_env_ref`（`events.py`，`__canonical_omit_none__` 加它 → 老录制字节不变）→ `GovernanceState.exec_env_ref`（`task.py`）→ fold `_on_task_host_bound`（`fold.py`）→ resolver `_bound_exec_env_ref_for` + `resolve_engine`/`resolve_engine_for_agent` 穿参 → `_engine_for_agent` **cache key 第 9 维**（`None` 默认 = 非 sandbox 字节等价）→ `_build_engine` 抽象签名 + SdkHost 实现。**子任务继承**：subtask 无自己的 `TaskHostBound`（其 `governance.exec_env_ref` fold 成 None），`_build_drain_host` 加 `inherited_exec_env_ref = _bound_exec_env_ref_for(parent)` 穿进 child build——与 `inherited_workspace` 同法（D4：子任务共享父容器）。
3. **weld 在 `driver.seed_start`**：`session_exec_env_ref = getattr(host,"exec_env_ref",None)()`（host 双 guard，测试 double 无此法 → None、本地路径字节不变）→ 既 `resolve_engine_for_agent(exec_env_ref=…)`（令 seed engine 与将写的 ref 一致）又 merge 进 `TaskHostBound`。weld 条件从 `if workspace_dir` 放宽成 `if workspace_dir or session_exec_env_ref`（sandbox 无 per-session workspace 时也记 ref）。`SdkHost.exec_env_ref()` → `_sandbox.current_ref()`(= config.base_url) 或 None。
4. **reconnect 在 `SdkHost._build_engine`**：`session_exec_env = self._sandbox.exec_env(base_url=exec_env_ref)`——`exec_env_ref` 非 None（resume/reclaim 读到的录制地址）⇒ 连**那个** base_url；None（seed/非 session）⇒ config 默认。**密钥永远取本机 config env（D5），不取 ref**。manager 从「单实例」升级为**按 base_url 缓存**（`_by_url`）：`exec_env(base_url=None)` 走 config 默认，reconnect 传录制 ref；跨 host 时录制 ref 可能 ≠ 本机 config，用 `dataclasses.replace(config, base_url=ref)` 造新 adapter。`teardown` 关掉所有缓存。
5. **reclaim 零改动**：stale-lease 重排 → 任一 worker `fold`+`resolve_engine(task)`（同一读 `governance.exec_env_ref` 的路径），录制 ref 自动重建 → 透明重连。**未在 cache key 上加同 host 跨 base_url 隔离之外的东西**——v1 单 host 单 config base_url，同 host 两 session 的 ref 恒等，唯一碰撞是「一台机 fold 两个跨部署 ref」（极端，记 limitation）。
6. **补测**：`test_sandbox_exec_env_ref.py`（4）——weld+fold（`seed_start` 写 `TaskHostBound.exec_env_ref` + fold 进 governance）；非 sandbox session 不记 ref（字节等价）；**多机重连验收**（host A 绑 `http://A:1111` 起 session → host B〔SAME event log、config `http://B:2222`〕fold+`resolve_engine` → fs backend 连 **A** 不连 B，fake factory 录 base_url 证明）；`exec_env_ref` 是 cache 维（同 ref 复用 engine、异 ref 分裂）。真容器仍 gated（`NOETA_TEST_AIO_SANDBOX_URL`）。

## Implementation notes (2026-07-07 — T7 landed: rewind restore routes through ExecEnv)

T7 让 rewind 在 sandbox 下把 baseline 写回**容器**而非宿主（全量 3076 passed / 0 fail、import-linter 16 kept 0 broken、改动文件 mypy 无新增错误〔stash 行号归一 diff = ∅〕、ruff/naming clean、既有 `test_rewind_fold.py` 回归绿）。

1. **【卡点消解——与 handoff 假设不同】capture 侧本就走 exec_env，无需碰 `ToolRuntime`。** handoff 记「T7 卡点：exec_env 是 per-tool 字段、runtime choke point `_capture_file_baselines` 拿不到」。实读代码：`_capture_file_baselines` **不读盘**——它读 `result.file_changes[*]["before"]`，而那份 pre-edit 字节是**写侧工具**（edit/write）**用自己的 `self.exec_env` 读好塞进去的**（T2 已改道）。故 capture 在 sandbox 下**已经正确**，`ToolRuntime` 零改动、`ctx.exec_env` 仍不需要（T1→T2 note #2 的否决继续成立）。真正需要改道的只有 **restore 侧**。
2. **restore 侧 = `driver._restore_files`**（唯一直打宿主 pathlib 的点）：原来 `root = host.workspace_dir_for(gov.workspace)` + `target.exists()/unlink()/parent.mkdir()/write_bytes()`。改道来源不是 per-tool 字段、也不是 ctx——而是 **T6 的 ref**：`host.exec_env_for_ref(gov.exec_env_ref) -> (ExecEnv, container_root) | None`。非 None（sandbox session）⇒ 用容器 backend + 容器 workdir 写回；None（local / 无 sandbox / 测试 double）⇒ **原 pathlib 分支逐字保留**（零回归，rewind 是精细区，故不合并两分支、只加旁路）。密钥仍取本机 config env（D5）。
3. **`ExecEnv` 加 `mkdir(path)`**（parents=True/exist_ok=True 语义）：restore 里「被 rewound span 删掉的目录要重建」这一步需要，兑现 T1→T2 note #3 的「mkdir 待需要时再加」。`LocalExecEnv.mkdir`=`Path.mkdir(parents,exist_ok)`；`AioSandboxExecEnv.mkdir`=`mkdir -p`（同 `unlink` 走 `_shell` 判 exit_code）。`ExecEnv` 是 tools 层 Protocol（非 `noeta.protocols`），isinstance 检查都是对具体类、不对 Protocol，故加方法不破坏既有 fake。
4. **补测**：`test_sandbox_rewind.py`（6）——`exec_env_for_ref`（sandbox+ref → (backend, 容器根)；local / ref=None / 无 sandbox → None）；`_restore_files` 直调：content_ref baseline → 容器 `write_bytes`+`mkdir(parent)`、宿主不碰；content_ref=None → 容器 `unlink`；**local（ref=None）rewind 仍写宿主 FS、容器 backend 零触**（回归护栏）。真容器写回仍 gated。

## Implementation notes (2026-07-07 — T8 landed: boundaries — background refuse + teardown)

T8 收边界（全量 3079 passed / 0 fail、import-linter 16 kept 0 broken、ruff/naming clean、background-shell 回归绿）。

1. **sandbox 下 `run_in_background=True` 清晰报错（D5）**：background 走宿主 `ProcessRegistry`（spawn 宿主 detached 子进程）——够不到容器，且 AIO 无 durable job handle（v2）。`ExecEnv` 加 `supports_background` 属性（`LocalExecEnv`=True、`AioSandboxExecEnv`=False）；`shell.py` 在 background 分支前判 `getattr(self.exec_env,"supports_background",True)`（默认 True → 本地/旧 backend 路径不变），非则回 `_err("run_in_background is not supported in sandbox mode (v1)…")`，**不 spawn**。`background_status`/`background_kill` 无需改（sandbox 下根本没 job 被建，查/杀都落「unknown job」）。
2. **teardown（D6）= host 级，per-conversation 故意不做**：v1 每 host 单容器共享，某个会话关闭时 teardown 会**误伤同 host 其它在跑会话**。故 teardown 只挂在 `Client.shutdown → SdkHost.teardown_exec_env`（T5 已接，进程退出收所有容器连接）；`ConversationClosed`/root-task terminal 处**不** teardown。per-container teardown 随 v2 per-root 容器到来。这与 background-shell 的「session-lifetime teardown」在**语义上**对齐（都在会话/进程收尾收资源），只是 v1 的资源边界是 host 而非 conversation。
3. **补测**：`test_sandbox_background_shell.py`（3）——具体类 capability（Local True / AIO False）；sandbox backend `shell_run(run_in_background=True)` → 清晰失败、含「not supported in sandbox mode」；**前台 shell 仍正常**（只拦 background）。

## Implementation notes (2026-07-07 — T9 landed: docs + ADR + CONTEXT + known-limitations) — **initiative complete**

T9 收尾文档（全量 3079 passed、`test_docs_codeblocks` 绿、lint-naming clean）。**至此 T1→T9 全部落地**，`feat/exec-env-sandbox` 分支就绪、未合并。

1. **新 ADR `docs/adr/execution-environment-seam.md`**：按 ADR 纪律写（present tense、无 T1–T9 过程编号、why-not-how），固化 D1（跨代不 fence，显式链回 `multi-host-lease-fencing.md` 的 alternative #1）/D2（seam 形状 + config addressing / secret-in-env）/D3（分层落点）+ v1 单容器/host + `exec_env_ref` reconnect + background 拒绝。ADR 被 VitePress `srcExclude`（`**/adr/**`），不进站点、按 prose 引用其它 ADR（同既有风格），无 dead-link 风险。
2. **CONTEXT.md 加 `ExecEnv` 术语**（Execution model 段）：deep seam、Local/AIO 两 backend、per-tool 构造字段不入 schema（stable prefix）、`exec_env_ref` 重连、密钥不落 log；`_Avoid_` 钉「Sandbox（是 backend 不是 seam）/ Workspace 已被占 / Executor（Engine 义）」。
3. **`docs/operations/limitations.md` 加两条**（站点发布页，仅 prose 引 ADR、无新 markdown link → 无 dead-link）：(a)「Sandbox side effects are not fenced across worker generations」（D1/R1）；(b)「One sandbox container per host; idle containers stay billed」（合并 v1 单容器无 per-session 隔离 + idle 成本 + exec_env_ref 只记 base_url + teardown 是 host 级）。

**验收对照 spec Acceptance criteria**：1 零回归 ✅（`default_host_byte_equal` + 全量绿）；2 stable prefix 不变 ✅（schema snapshot 绿）；3 sandbox 跑通 ✅（fake-transport，真容器 gated）；4 多机重连 ✅（`test_sandbox_exec_env_ref.py` 跨 host 用例）；5 rewind under sandbox ✅（`test_sandbox_rewind.py`）；6 边界明确 ✅（background 拒绝 + host-shutdown teardown）；7 known-limitations + ADR + CONTEXT ✅。**唯一 gated**：真 AIO 容器 e2e（`NOETA_TEST_AIO_SANDBOX_URL`）——上真容器前 adapter 契约（R2）不当「已验证」宣称。

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
