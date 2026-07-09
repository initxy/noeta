# Sandbox 浏览器子系统：noeta 自持 browser 工具（层3）+ browser 子 agent（层4）

> 状态：**shape 完成，待实现（2026-07-09）**。标「【替你定，可否决】」的是维护者确认「按推荐来」后我替他定的机械决策，可事后推翻。
> 前置：per-session Sandbox（Tier 2）已合入（`docs/adr/execution-environment-seam.md` v2 + `docs/implementation-specs/2026-07-08-per-session-sandbox.md`）。本 spec 在其上新增浏览器能力，只在 sandbox 模式（`NOETA_AGENT_SANDBOX=1`）生效。
> 姊妹 spec：③「前端预览面板 + WebSocket 反代（browser/terminal/code）」单独立 spec，本文不含。

## Goal

给运行在 per-session Sandbox 里的 agent 一套**可用的浏览器能力**，分两层落地：

1. **层3 — browser 工具面**：一套 **noeta 自持 name/schema** 的 browser 工具（`browser_navigate` / `browser_click` / `browser_type` / `browser_extract` / `browser_screenshot`），实现层在内部转调容器 `/mcp` 的 `browser_*`。模型的工具契约由 noeta 钉死，AIO 镜像升级换工具名不扰动 stable-prefix。
2. **层4 — browser 子 agent**：一个 `web` 子 agent（`AgentSpec`：browser 工具 + 浏览专用 prompt + 独立 context），主 agent 委托它做网页任务，只回收提炼后的结果，把浏览的 token 膨胀隔离在子 agent 里。

## Non-goals

- **不挂 AIO `/mcp` 当 MCP 连接器**（不进 `mcp_registry`、不走 alias 机制）。browser 工具是 per-session 工具 pack，走 exec_env 那条构造期注入路径。理由见 D2。
- **不引入 `browser-use` / Playwright 作为依赖跑外来 agent 循环**：noeta 自己的 ReAct 循环就是层4的决策者。Playwright 只作为「模型写脚本 via `shell_run`」的确定性补充，本就免费可用，不在本 spec 范围。
- **v1 不做视觉/多模态**：`browser_screenshot` 返回 workspace 文件 ref（既有 file panel / Lightbox 可看），**不**把截图作为图片内容喂回模型。视觉+开关留**增量2**（seam 留好，见 D4）。
- **不做 `/v1/browser` 坐标级 computer-use 路径**：实测 `/v1/browser/actions` 只有像素坐标级动作（`CLICK(x,y)`/`MOVE_TO`/`SCROLL(dx,dy)`/`DRAG_TO`/`HOTKEY`/`TYPING`），高层元素级语义只在 MCP。坐标路径存为反爬/视觉站点的**备选**，不是 v1 主路。
- **不改既有工具的 model-facing 契约**（name/schema/description）→ stable-prefix 不变（硬约束，CONTEXT.md Stable Prefix）。
- **非 sandbox 模式无 browser 工具**：无容器即无浏览器，老录制字节不变。
- **③ 前端预览面板 + WS 反代**：另立 spec。

## Context

- **三层拓扑**：`noeta.tools`（materials）> `noeta.runtime`（kernel-services）> `noeta.execution`；SDK `noeta.client` 在 tools 之上；`apps/noeta-agent`（`noeta.agent`）最上。browser 工具 pack 落 `noeta.tools.browser`（materials 带，与 fs 同带，可 import `noeta.tools.mcp._http_client`）。
- **AIO Sandbox 浏览器事实（调研 + 文档实证）**：
  - 容器起来后 headless Chromium **常驻**，8080 端口 front 全部服务。浏览器保有状态（tabs/cookie/当前页）跨多次调用存活——**不是** `shell_run`（一次性、无常驻状态）。
  - **高层、元素级、LLM 友好**的能力（`browser_navigate` / 按元素 `click` / `type` / `extract`）**只在 `/mcp` 聚合端点的 browser server**（内部多半自己包了 Playwright/CDP）。工具名文档里出现过 `navigate` 与 `browser_navigate` 两种写法——**这正是 stable-prefix 漂移风险的实证**。
  - `/v1/browser/*` HTTP 面是**坐标级**：`/v1/browser/actions`（像素动作）、`/v1/browser/screenshot`（整窗截图）、`/v1/browser/info`（返 `cdp_url` + viewport）、`/v1/browser/config`（设分辨率）。**无** selector 点击、**无**元素列表、**无** markdown 抽取。
  - Auth：同容器其余 API，`X-AIO-API-Key`（`SandboxAuth.connect_headers`，D8）。
- **noeta 已就绪、可直接复用的机制**：
  - `McpHttpClient`（`packages/noeta-runtime/noeta/tools/mcp/_http_client.py`）：同步单线程 JSON-RPC over HTTP，`initialize` + `tools/call`，支持 SSE 单响应解析、静态 header 注入、total_cap。**browser backend 的内部传输直接用它**。
  - `AioSandboxExecEnv`（`tools/fs/exec_env.py`）：把 AIO file/shell wire 钉在**一个 adapter + fake-transport 测试**里的范式——browser adapter 照抄这个形状。
  - per-session sandbox handle 全链：`SandboxExecEnvManager.resolve(exec_env_ref) -> (backend, workdir)`（`packages/noeta-sdk/noeta/client/sandbox.py`），handle 持 `base_url` + `auth`；`_build_engine` 已在 sandbox 模式解析出 `session_exec_env`（`host.py:1377-1394`）。
  - 工具集装配：`build_session_inputs`（`packages/noeta-runtime/noeta/execution/builder.py:862`）按 capability + 注入物组工具集，`_build_engine`（`host.py:1341`）喂参数。fs 工具靠 `exec_env=` 注入；MCP 工具靠 `mcp_tools_override`；`open_app` 靠 `app_gateway` 条件挂载——**browser 工具照 fs 的注入范式**。
  - `AgentSpec.Capabilities`（`packages/noeta-runtime/noeta/agent/spec.py:71`）：`todo_write` / `delegation` / `skill_invocation` / `memory` / `mcp` 布尔位；四个官方 agent 的 preset 在 `packages/noeta-runtime/noeta/presets/__init__.py`。加一个 `browser` 位即可。
- **既有 ADR posture（本 spec 要正面处理）**：
  - `execution-environment-seam.md` **alt #5** 否决过「挂 AIO `/mcp` 当 backend」——理由：引入容器工具名/schema、扰动 stable-prefix、与 fs/shell 重叠。**本 spec 不违反**：我们不挂 MCP 连接器、不把 AIO schema 暴露给模型（noeta 自持 schema）、browser 是**净新增**无 fs/shell 重叠。
  - 同 ADR **line 214** 明确预告「AIO's browser 作为后续原生 refinement」——本 spec 正是兑现这条。
  - `mcp-connectors.md` + per-session spec 把「MCP 进容器」标为 Tier 3 缓办，理由是「要给 seam 加 MCP 方法 + 扰动 MCP 工具 schema」。**本 spec 两点都不碰**：browser backend 用独立 `McpHttpClient` 作**内部传输**，**不给 `ExecEnv` seam 加 MCP 方法**；模型面是 noeta schema，不吐 AIO schema。→ 穿过针眼，需一条 ADR 记录此立场。

## Decisions

### D1【已确认】层3 = B3：noeta 自持 schema，内部转调容器 `/mcp`

- browser 工具的 name/schema/description **由 noeta 定义并拥有**（stable-prefix 由 noeta 钉死）。
- 实现层 = 一个 `AioBrowserBackend`（`noeta.tools.browser`，镜像 `AioSandboxExecEnv` 形状）：持 `base_url` + `auth_headers`，内部用 `McpHttpClient(url=base_url+"/mcp", headers=auth())` 做 `tools/call browser_*`。**AIO browser wire（哪些 `browser_*` 工具、参数名、返回结构）钉在这一个 adapter + fake-transport 契约测试里**；AIO 改名/改签名 → 只炸这一处、被测试当场抓到，模型面不动。
- **无需运行时 `tools/list`**：noeta 硬编码 noeta-tool → AIO-tool 的映射（不像 MCP 连接器要动态发现），更确定。仅需 `McpHttpClient.start()` 握手一次（lazy）。
- **CDP 直驱排除**（异步 + 重依赖，违背 noeta 纯 stdlib 同步纪律）；`/v1/browser` 坐标路径存为备选（Non-goals）。

### D2【已确认】不是 MCP 连接器，是 per-session 工具 pack（走 exec_env 注入路径）

- browser 工具**不进 `mcp_registry`、不占 alias、不进「enabled alias clean list」**。它像 fs 工具一样：在 `_build_engine` 里，当 session 有 sandbox handle 时，用 handle 造 `AioBrowserBackend`，传给 `build_browser_tools(backend=...)`，`build_session_inputs` 把它并进工具集。
- 这样**不碰 MCP 层**（不发明「per-session 动态合成连接器」这个静态 registry 不支持的新概念），也**不用翻 MCP=Tier3 的案**：MCP client 只是 backend 的内部传输实现，不是模型面的连接器。

### D3【已确认】层4 = `web` 子 agent，opt-in per spec

- 新增一个官方子 agent `web`（`AgentDefinition` in `presets/__init__.py`）：
  - tools = browser 工具全集 + `read`/`write`（存证据/结果）+ 只读 `shell`/`webfetch`（对齐 explore 的只读底盘）。
  - `capabilities=Capabilities(browser=True, skill_invocation=True)`。
  - prompt（`presets/prompts/web`）：浏览专用——强调「用 `browser_extract` 拿带编号元素→按元素操作」的 browser-use 式循环，任务完成回**提炼摘要**给父 agent。
- **能委托它的**：main / general-purpose（`delegation=True` 且 allow-list 含 `web`）。explore / plan 不给。
- 主 agent 是否**直接**挂 browser 工具：`main` 的 `Capabilities.browser` 默认 **True**【替你定，可否决】（主循环也能开浏览器；但重活推荐委托给 `web` 子 agent 以隔离 token）。

### D4【已确认】感知 v1 = 文本/元素级；screenshot 存文件 ref；视觉+开关留增量2

- v1 工具集：`browser_navigate(url)` / `browser_click(ref)` / `browser_type(ref, text)` / `browser_extract()`（返页面文本 + **带编号可交互元素列表**，browser-use 式表示）/ `browser_screenshot()`。可能再加 `browser_wait` / `browser_get_tabs` / `browser_navigate_back`（按容器 MCP 实有能力，implement 时对 live 容器钉）。
- **文本进、文本出**：`click`/`type` 按 `extract` 给出的**元素编号/ref**定位，不走像素坐标。
- `browser_screenshot` 的结果 = **存 PNG 到 workspace + 返文件 ref**（既有 file panel/Lightbox 可看），**不**作为图片内容喂回模型。
- **增量2（seam 留好，本轮不做）**：把截图作为视觉内容喂回模型，用一个 capability/config 开关控制（默认关）。因工具 schema 两模式一致（是否喂视觉是运行时行为，不进 prefix），此开关**不扰动 stable-prefix**。

### D5【替你定，可否决】权限：browser 工具 high risk，走 shell 那套 approval

- browser 工具能出网到任意站点，`risk_level="high"`，经 `PermissionGuard` / approval predicate（与 `shell_run` 同一套 effective_permission 逻辑，`host.py:1425-1452`）。`bypassPermissions` 放行；default/acceptEdits 下未在 allowlist 的导航/操作走 HITL。
- **备选**（可选）：把「导航到某 host」纳入类似 shell allowlist 的 host allowlist。本轮不做，记为后续。

### D6【已确认】仅 sandbox 模式 + 条件工具集成员 + stable-prefix 安全

- browser 工具**当且仅当**：session 有 sandbox handle（`exec_env_ref` present）**且** agent spec `Capabilities.browser=True` 时出现。二者都是 durable session 状态/静态 spec，live 与 resume 一致 → prefix 确定。
- **resume 走 fs 工具范式，不走 MCP 范式**：browser 工具 schema 是 noeta 自持**静态**的，resume 时从 `exec_env_ref` 解析 sandbox → 照常重建 browser backend + 工具（像 fs 工具那样从 session 状态重建），**不**需要 MCP 那套「录 alias、resume 传空」的机制。recorded tool spec（noeta 自己的 schema）是 durable truth，resume 字节一致。
- 非 sandbox / `browser=False` → 无 browser 工具，老录制字节不变。

### D7【已确认】per-session 解析：`SandboxExecEnvManager` 多 vend 一个 browser backend

- manager 已持 per-session handle（`base_url` + `auth`）。新增 `resolve_browser(exec_env_ref) -> AioBrowserBackend`（或让 `resolve` 一并返回 handle，让 `_build_engine` 自己造 backend）。**复用**现有 handle 缓存/attach/reconnect 全链，不新增生命周期。
- `_build_engine`（`host.py:1386-1394`）已解析 `session_ref`；在同处解析出 browser backend，经 `build_session_inputs(..., browser_backend=...)` 下传。`None`（无 sandbox / `browser=False`）→ 不挂，字节等价回退。

### D8【已确认】auth / wire 复用 v1 D8

- browser backend 的 auth 走 `SandboxHandle.auth.connect_headers()`（每调用现取，D8），与 `AioSandboxExecEnv` 一致；密钥只上 wire、不落 log/event/durable。
- AIO browser wire 契约钉在 `AioBrowserBackend` 一个文件 + fake `McpHttpClient` 测试（对齐 `AioSandboxExecEnv` 的 fake-transport 测试）。

## Implementation plan

1. **browser backend（runtime/materials）**：`noeta.tools.browser._backend.AioBrowserBackend`——持 `base_url`+`auth_headers`，内部 `McpHttpClient` 转调 `browser_*`；方法 `navigate/click/type/extract/screenshot(+wait/tabs/back)`；wire 钉此处 + 契约测试。定义一个窄 `BrowserBackend` Protocol（注入点，测试可替身）。
2. **browser 工具 pack（runtime/materials）**：`noeta.tools.browser.__init__.build_browser_tools(backend, *, mode/permission)`——返回 noeta 自持 schema 的 `Tool` 字典（`browser_navigate` 等）；`invoke()` 调 backend；`screenshot` 结果存 workspace 返 ref。
3. **capability 位**：`AgentSpec.Capabilities` 加 `browser: bool = False`（`agent/spec.py`）；`resolver.py` 转发 `browser_enabled`。
4. **装配接线**：`build_session_inputs` 加 `browser_backend` 参数（`builder.py:862`），当 `browser_backend` 且 `capabilities.browser` → 并入 browser 工具（follow fs→script→MCP→control 的 append 顺序，browser 放在 fs 之后/MCP 之前，定死一个位置保序）。
5. **SDK 解析**：`SandboxExecEnvManager` vend browser backend（D7）；`_build_engine` 从 session handle 造 backend 下传（`host.py`）。
6. **层4 子 agent**：`presets/__init__.py` 加 `web` `AgentDefinition` + `presets/prompts/web` prompt；main/general-purpose 的 delegation allow-list 纳入 `web`；`main` 默认 `browser=True`。
7. **权限**：browser 工具 `risk_level="high"`，接 approval predicate（D5）。
8. **文档 + ADR + CONTEXT**：ADR 记录「browser 作为 noeta 自持原生工具、MCP client 仅内部传输」立场（补进 `execution-environment-seam.md` 或新 ADR）；CONTEXT 加术语（browser 工具 pack / browser 子 agent）；known-limitations 更新（v1 无视觉、坐标路径未做、浏览器 idle 随容器计费）。

## Task breakdown

| # | 任务 | 层/带 | 依赖 / 并行 |
|---|---|---|---|
| B1 | `AioBrowserBackend` + `BrowserBackend` Protocol + AIO browser wire 契约测试（fake `McpHttpClient`） | runtime/materials | 基座，先做；依赖既有 `McpHttpClient` |
| B2 | `build_browser_tools` 工具 pack（noeta 自持 schema + `invoke` 调 backend + screenshot 存 ref） | runtime/materials | 依赖 B1 |
| B3 | `Capabilities.browser` 位 + `resolver` 转发 | runtime | 与 B1/B2 并行 |
| B4 | `build_session_inputs(browser_backend=)` 条件并入工具集（保序） | runtime | 依赖 B2/B3 |
| B5 | `SandboxExecEnvManager` vend browser backend + `_build_engine` 下传 | SDK | 依赖 B1/B4；镜像 exec_env 现链 |
| B6 | 层4 `web` 子 agent（AgentDefinition + prompt + delegation allow-list + main 默认 browser=True） | runtime/presets | 依赖 B2/B3 |
| B7 | 权限（high risk + approval 接线） | SDK/runtime | 依赖 B2/B4 |
| B8 | 真容器 e2e（gated `NOETA_TEST_AIO_SANDBOX_URL` / 本地 Docker）：起容器→`browser_navigate`/`extract`/`click`/`screenshot` 跑通；对 live `tools/list` 钉准确工具名/签名 | — | 依赖 B1–B7 |
| B9 | 文档 + ADR + CONTEXT + known-limitations | — | 收尾 |

## Dependencies / sequencing

- **B1 是 wire 缝，先落**；B3 与 B1/B2 并行。
- **B2→B4→B5** 是「工具→装配→per-session 解析」主链。
- **B6/B7** 依赖工具就位，可与 B5 并行。
- **B8** 依赖整链通，且是唯一能把「准确工具名/签名」钉死的地方（文档里名字不一致，必须对 live 容器）。
- 每步保持「无 sandbox / `browser=False` ⇒ 字节等价回退」。

## Acceptance criteria

1. **零回归**：非 sandbox / `Capabilities.browser=False` 下全量既有测试绿；老录制 fold/replay 字节不变（无 browser 工具时工具集/prefix 与 pre-spec 相同）。
2. **stable prefix 由 noeta 掌控**：browser 工具 schema 是 noeta 自持静态字节；模拟 AIO 把 `browser_navigate` 改名 `navigate`（fake backend）→ **模型面工具 schema 不变**，只有 backend 契约测试变。
3. **B3 转调正确**：`browser_navigate/click/type/extract/screenshot` 经 `AioBrowserBackend` 正确映射到容器 `/mcp` 的 `browser_*`（fake transport 断言 wire）。
4. **per-session 隔离 + resume**：两个并发 sandbox session 各自浏览器状态独立；resume 一个 sandbox session 后 browser 工具照常在（从 `exec_env_ref` 重建），工具集字节与 live 一致。
5. **感知 v1**：`browser_extract` 返文本 + 带编号可交互元素；`browser_click(ref)`/`type(ref,...)` 按编号定位；`browser_screenshot` 存 workspace 返 ref、**不**入模型多模态。
6. **层4 子 agent**：main/general-purpose 能 `spawn_subagent("web", ...)`；`web` 只有 browser + 只读底盘工具；浏览发生在子 agent context，父只收摘要。explore/plan 无法委托 `web`。
7. **权限**：default 模式下未授权导航走 approval；`bypassPermissions` 放行。
8. **真容器 e2e**（gated）：起 AIO 容器，`web` 子 agent 打开一个页面、抽取、点击、截图，端到端跑通；准确工具名/签名已对 live 钉。
9. **文档**：ADR 记录 browser 立场（noeta 自持 + MCP client 仅内部传输，与 alt #5 / Tier3 的边界）；CONTEXT 加术语；known-limitations 更新。

## Risks

- **R1 AIO browser wire 漂移**：镜像升级改 `browser_*` 名/签名 → 炸 `AioBrowserBackend`。缓解=wire 钉一处 + 契约测试当场抓；pin 镜像 tag（非 `:latest`）；模型面因 noeta 自持 schema 不受影响（这正是选 B3 的收益）。**已按发布源校准（见下「Implementation notes」）**，R1 从「近似猜测」降为「镜像升级回归」——名字/参数不再是猜的。
- **R2 高层能力只在 MCP，backend 强耦合容器 `/mcp`**：若某能力 MCP 也没有（只在 `/v1/browser` 坐标级），v1 该动作缺失。缓解=v1 只承诺 MCP 有的元素级动作；坐标/视觉留增量。
- **R3 感知表示质量**：`browser_extract` 的元素列表若不够「带编号可点」，模型难精确操作。缓解=implement 时对 live 容器验 extract 返回；不足则在 backend 侧后处理成 browser-use 式编号表示（一处补，不影响 schema）。
- **R4 token 膨胀**：浏览多轮 `extract` 文本累积。缓解=层4 子 agent 隔离 context + 只回摘要；`extract` 输出走既有 output_cap/artifact 溢出。
- **R5 浏览器 idle 成本**：浏览器随容器常驻，会话 suspend 期间占资源。缓解=随 per-session 容器生命周期（既有 limitation，无新增）。
- **R6 权限面**：browser 能出网到任意站点，是新的外呼面。缓解=high risk + approval（D5）；容器仍是隔离边界。

## Files / areas to inspect

- **新增**：`packages/noeta-runtime/noeta/tools/browser/`（`_backend.py` = `AioBrowserBackend` + `BrowserBackend` Protocol；`__init__.py` = `build_browser_tools` + 各 `Tool`；`descriptions/` 工具描述，对齐 `tool-description-canonical.md`）。`packages/noeta-runtime/noeta/presets/prompts/web`（子 agent prompt）。
- **复用/镜像**：`packages/noeta-runtime/noeta/tools/mcp/_http_client.py`（`McpHttpClient` 作内部传输）、`packages/noeta-runtime/noeta/tools/fs/exec_env.py`（`AioSandboxExecEnv` adapter + fake-transport 测试范式）。
- **改**：`packages/noeta-runtime/noeta/agent/spec.py:71`（`Capabilities.browser`）、`packages/noeta-runtime/noeta/presets/__init__.py`（`web` AgentDefinition + delegation allow-list + `main` browser 默认）、`packages/noeta-runtime/noeta/execution/builder.py:862`（`build_session_inputs` 加 `browser_backend` + 条件并入 + 保序）、`packages/noeta-runtime/noeta/execution/resolver.py`（转发 `browser_enabled`）。
- **SDK**：`packages/noeta-sdk/noeta/client/sandbox.py`（`SandboxExecEnvManager` vend browser backend）、`packages/noeta-sdk/noeta/client/host.py:1341-1569`（`_build_engine` 造 backend 下传）。
- **文档/ADR/CONTEXT**：`docs/adr/execution-environment-seam.md`（补 browser 立场，呼应 alt #5 / line 214）、`docs/adr/mcp-connectors.md`（注：browser 用 MCP client 作内部传输、非连接器）、`CONTEXT.md`（术语）、known-limitations。
- **参照**：`docs/implementation-specs/2026-07-08-per-session-sandbox.md`（per-session handle 现链，逐点镜像）、`docs/adr/tool-and-agent-catalog.md` + `docs/adr/mcp-connectors.md`（子 agent capability / 委托 / 录制确定性边界）。

## Implementation notes

### 2026-07-09 — AIO browser wire 按发布源校准（B8 的无-Docker 部分）

本地无 Docker，改从 AIO Sandbox 实际打包的发布源 `@agent-infra/mcp-server-browser`（`bytedance/UI-TARS-desktop`，`packages/agent-infra/mcp-servers/browser/src/{server,tools}.ts`）把 `AioBrowserBackend` 的 wire 常量**钉准**。原 B1 常量是照 Playwright-MCP 猜的，与真容器有三处偏差，已修正：

- **元素引用是数字 `index`，不是字符串 `ref`**。容器按 `browser_get_clickable_elements` 给出的编号定位（`browser_click {index:number}`），`ref` 是猜错的。→ 模型面 schema 也随之改为 `index: integer`（本轮全部未 commit，改 schema 零成本，且更贴合 `extract` 回给模型的 `[7]` 编号）。
- **无 `browser_type`**：真工具是 `browser_form_input_fill {index, value, clear}`；提交靠 `browser_press_key {key:"Enter"}`。noeta 的 `browser_type` 在 backend fan-out 成 fill(+Enter)——正是 D1 这条 seam 的用处（noeta 自持 `browser_type` 名，wire 归容器）。
- **无 `browser_extract`**：真工具是 `browser_get_markdown`（页面文本）+ `browser_get_clickable_elements`（编号元素）。noeta `extract` 把两者拼成「页面文本 + `# Interactive elements` 编号表」（spec R3 sanctioned 的 backend 后处理）。
- **对上的**：`browser_navigate {url}`（且**返回值内联带编号元素**，noeta navigate 直接透传）、`browser_screenshot {}`（返回 `[text, image/png base64]`，noeta 解析正确）。

契约测试（`test_browser_backend.py` / `test_browser_tools.py`）已改为断言校准后的 wire + `index:integer` schema，全绿（full suite 3174 passed / 0 failed）。

**B8 residual（仍需 Docker）**：names/args 已源准，但**运行时返回结构 / 行为**（`get_clickable_elements` 编号表的实际格式与够不够「可点」、`form_input_fill` 的回执文本、navigate 内联元素的真实样子、screenshot content 块顺序）只能对 live 容器跑通确认；R3（感知表示质量）也要那时才验。

### 2026-07-09 — B9 文档落库（英文源）

- **ADR**：`docs/adr/execution-environment-seam.md` 追加「Browser subsystem (2026-07-09)」段——立场（层3/层4、穿过 alt#5 针眼、不重开 MCP=Tier3、为何走 `/mcp` 非 `/v1/browser`、wire 源准、感知 v1、权限）+ 4 条 browser alternatives。
- **CONTEXT.md**：Vocabulary 加术语 **Browser tool pack**（含 `web` 子 agent、`BrowserBackend` seam、内部传输、非连接器 + `_Avoid_`），紧接 `SandboxProvider`。
- **known-limitations**：`docs/operations/limitations.md` 加「Sandbox browser is text-level and container-scoped in v1」（三条边界：无容器无浏览器 / 纯文本非视觉 / 随容器计费）。
- **zh 待办**：`docs/zh/operations/limitations.md`（及站点 zh 镜像）按既有工作流走单独 `translate-zh` pass，本轮只动英文源（English frozen before translate-zh）。
