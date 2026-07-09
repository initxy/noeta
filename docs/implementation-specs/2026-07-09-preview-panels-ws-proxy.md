# 前端预览面板（browser/terminal/code）+ 主端口 WebSocket 反代

> 状态：**shape 完成，待实现（2026-07-09）**。标「【替你定，可否决】」的是维护者确认大方向后我替他定的机械决策，可事后推翻。
> 前置：per-session Sandbox（Tier 2）已合入；`docs/implementation-specs/2026-07-09-sandbox-browser-subsystem.md`（层3 browser 工具 + 层4 `web` 子 agent）是**姊妹活**——那条给 agent 浏览器能力，本条给**人**一个实时看/接管容器的预览面。
> 决策已定：三个面板都做（共享一套反代）；browser 走 noVNC iframe；发现走新增 thin 端点 `GET /tasks/{id}/preview`；WS 传输**手搓 minimal RFC6455**（对比见 Decisions D2）。

## Goal

给运行在 per-session Sandbox 里的会话一个**人可看、可交互的实时预览面**，三个面板，全部通过 noeta **主端口**反代到会话自己的容器（无第二端口、VM/port-forward 下也可达）：

1. **browser** — noVNC，看/操作容器里那个 `web` 子 agent 正在驱动的真实浏览器。
2. **terminal** — 容器的 PTY，看 `shell_run` 实时输出、必要时人工敲命令。
3. **code** — code-server（VSCode Web），用真编辑器浏览/改 workspace。

三者的共同底座是一个**主端口 WebSocket 反代**：现有 `PreviewGateway` 是 buffered HTTP 一问一答，后端是 stdlib `ThreadingHTTPServer`，都不带 WS，必须新建。

## Non-goals

- **不改任何 model-facing 契约**：本条是纯**人机 UI + host 管线**，不新增/改动工具、schema、prompt、capability → stable-prefix 零扰动、老录制字节不变（硬约束）。
- **不做鉴权 / SSRF allowlist / 多用户隔离**：沿用 `PreviewGateway` 的 **v1 demo 红线**（localhost 绑定、无注入凭据到浏览器、token 门禁、容器即隔离边界）。硬化留后续（D6）。
- **不引 WebSocket 库**：手搓 minimal RFC6455 反代（D2），零新运行时依赖。
- **不做 CDP screencast**：browser 面板走 noVNC iframe（用户已定），CDP 路径存为后续视觉/低带宽备选。
- **不给非 sandbox 会话加任何东西**：无容器 ⇒ `GET /tasks/{id}/preview` 返 404 ⇒ 前端隐藏三个面板 → 非 sandbox 部署完全不受影响。
- **不做预览录制/回放**：预览是**实时**的，纯运行时状态，不进 event log、不持久、不 replay（与 `PreviewGateway` mount 同性质）。
- **不做面板与 agent 的操作冲突仲裁**：人和 `web` 子 agent 同时操浏览器会抢控制权，是**有意的人工介入**，v1 不仲裁（D5）。

## Context

- **后端 = stdlib `http.server.ThreadingHTTPServer` + `BaseHTTPRequestHandler`**（`apps/noeta-agent/noeta/agent/backend/app.py`）。SSE 靠往 `self.wfile` 直接写 chunk 实现流式；**没有原生 WS**。但 `BaseHTTPRequestHandler` 能拿到裸 socket（`self.connection` / `self.rfile` / `self.wfile`），且**每连接一线程**——所以在 `do_GET` 里识别 `Upgrade: websocket` → 手动完成 101 握手 → 阻塞式双向 pump 是可行的（socket hijack）。
- **`PreviewGateway`**（`apps/noeta-agent/noeta/agent/host/preview_gateway.py`）= open_app 的 HTML 预览：`route(method,path,query,...) -> PreviewResponse`（**整体 buffer 的一问一答**）+ token→{workspace_dir, app_rel, proxy_to, task_id} mount registry + 主 server 经 `_maybe_preview` 把 `/preview/<token>/` 路进来。它服务的是 **workspace 文件 + /api 代理到模型声明的目标**——与「反代到**容器自己的 8080**」是两码事，且结构上装不下持久 socket。→ 本条**新建** `SandboxPreviewGateway`，不扩它。
- **容器可达性**：`LocalDockerSandboxProvider` 用 `-p 127.0.0.1:<port>:8080` 起 AIO 镜像，`SandboxHandle.base_url` 就是 `http://127.0.0.1:<port>`——**与 `ExecEnv` 用的同一个 base_url + auth**。反代把 `/sandbox-preview/<token>/<sub>` 转到容器 `http://base/<sub>`（HTTP）或 `ws://base/<sub>`（WS）。
- **AIO 预览面**（8080，官方文档，精确子路径 Docker-time 钉，见 R2/R3）：browser=noVNC `/vnc/index.html`（页 HTTP，实时画面走 websockify **WS**）；terminal=`/v1/shell/ws`（**WS** PTY）；code=code-server `/code-server/`（页 HTTP + 内部 **WS**）；另有 `/proxy/{port}/`、`/absproxy/{port}/` HTTP 代理。
- **auth**：容器各 API 走 `SandboxHandle.auth.connect_headers()`（`X-AIO-API-Key`）。反代把它加在**上游那条腿**的握手/请求里；浏览器侧永不见 key。
- **前端 = Vite MPA**（vanilla ES + 少量 JSX）。右侧 `RightDock.jsx` 已是**持久化 tab 面板**（`Files | App`，`panelType` 存 panel prefs，注释明说 `panelType` 是「generic-shell hook」——为多面板预留）。「App」tab 渲染一个 `sandbox` iframe 指向 `/preview/<token>/`，由 `open_app` side-effect（走 SSE 事件流）驱动、文件编辑触发 reload（`app-preview.js`）。→ 三个新面板就是 `RightDock` 里的新 `panelType`，browser/code=iframe，terminal 见 R3。
- **thin 后端现有面**：`POST /tasks`、`GET /tasks`（仅 roots）、SSE 流、`/file`、`/files`、`/content`、`/preview/<token>/`、`/tasks/{id}/artifacts`、`/tasks/{id}/images`。**无** session/sandbox 信息端点 → 需新增 `GET /tasks/{id}/preview`（D4）。

## Decisions

### D1【已确认】三个面板都做，共享一套反代；browser=noVNC iframe

- 反代（WS+HTTP 透传）是主要工作量,一旦建好三个面板都是增量。browser 用 noVNC iframe(AIO 自带 `/vnc` 页 + websockify),前端代码最少、看到的是真浏览器(含非 headless UI)。

### D2【替你定，可否决】WS 反代手搓 minimal RFC6455，零新依赖

- 反代是 WebSocket 的**最简子集**:只**转发字节**,不理解消息语义。因此无需消息重组、无需 UTF-8 校验、**握手里不 offer permessage-deflate**(两边不压缩)、背压靠「每连接一线程 + 阻塞写」天然获得。
- 真正要写的:`read_frame(sock)->(fin,opcode,payload)` + `write_frame(sock,fin,opcode,payload,mask)`(掩码=4 字节 XOR)+ 服务端 accept 握手(`Sec-WebSocket-Accept`)+ 客户端上游握手(随机 `Sec-WebSocket-Key`)。opcode(data/ping/pong/close)+payload+FIN **原样透传**,不解释。~150–200 行。
- **钉在一个 module + fake-socket 契约测试**里(镜像 `McpHttpClient` / `AioSandboxExecEnv` 的形状与测法),与仓库「宁可手搓 stdlib transport 也不引库」的取向一致;给上线 agent 加新运行时依赖与该取向相悖。
- **必须记得**:`Sec-WebSocket-Protocol` 子协议协商(noVNC=`binary`、ttyd=`tty`、code-server 视情况)在**两条腿都转发**;`X-AIO-API-Key` 只上「服务端→容器」腿。
- **代价**:~200 行协议代码必须写对(64 位长度、掩码、跨 TCP 段半包读);缓解=fake-socket 单测 + 客户端是 noVNC/ttyd/code-server 这些规矩实现 + demo 边界不抗敌意客户端。
- **备选(否决)**:引 `websocket-client`(同步)做上游腿——降实现风险,但多一个上线依赖,且库能多给的(消息 API/全合规/扩展/async)恰是反代不需要的。

### D3【替你定，可否决】新建 `SandboxPreviewGateway`(product 层),不扩 `PreviewGateway`

- `PreviewGateway` 是 buffered `route()->PreviewResponse`,装不下持久 socket。新建 `SandboxPreviewGateway`(`apps/noeta-agent/.../host/`,与之并列):
  - **registry**:`token -> {base_url, auth, root_task_id}`,容器 allocate 时注册、release 时注销(镜像 `unmount_task`);token=`secrets.token_urlsafe`,不可猜。
  - **一个通用反代**:`/sandbox-preview/<token>/<sub>` → 若 `Upgrade: websocket` 走 WS 反代(hijack),否则 HTTP 透传(转 `base_url/<sub>`,上游带 auth)。三个面板都走它(navigate/vnc/terminal/code 只是不同 `<sub>`)。
- **HTTP 透传用流式**优先(code-server/noVNC 资源可能大);v1 允许退化为 buffer(可接受),但 WS 是不可退化的核心。

### D4【替你定，可否决】发现走新增 thin 端点 `GET /tasks/{id}/preview`

- 返 `200 {token, panels:{browser:<sub>, terminal:<sub>, code:<sub>}}`,或 `404`(该 task 的 session 无 sandbox)。后端 task→root→`exec_env_ref`→registry 查 token。
- 选它而非塞进 SSE 事件流:sandbox 是 **session 基建、非工具 side-effect**,不该污染 event log;显式端点贴合现有 thin REST 面(`/file`/`/files`/`/artifacts`)。前端开面板时 fetch,404 就隐藏面板。

### D5【替你定，可否决】面板可交互;浏览器控制权与 `web` 子 agent 竞争不仲裁

- noVNC/ttyd/code-server 默认交互式,做只读反而多花功夫,且「人能接管/协助」是特性。浏览器面板人一操作会与 `web` 子 agent 抢控制权——**有意的人工介入**,v1 不仲裁,文档标注即可。

### D6【已确认】沿用 demo 安全红线,zero-regression 加法

- 与 `PreviewGateway` 同一条 v1 红线:localhost 绑定、浏览器→noeta 腿**不鉴权**仅靠不可猜 token、**永不注入凭据到浏览器**、容器即隔离边界、session 结束注销。硬化(浏览器腿鉴权、SSRF、多用户)留后续。
- 纯加法:无 sandbox ⇒ 端点 404 ⇒ 面板隐藏;无任何 model-facing 改动 ⇒ stable-prefix 与老录制字节不变。

## Implementation plan

1. **WS 反代传输(product/host)**:`preview_ws.py`——`accept_handshake(handler) -> bool`(算 `Sec-WebSocket-Accept`、回 101、协商子协议)、`connect_upstream(url, headers, subprotocol) -> sock`(客户端握手)、`pump(a, b)`(双向转发,`select` 两 socket 或双线程,opcode+FIN+payload 原样、close 收两腿)、`read_frame`/`write_frame` 帧编解码。fake-socket 契约测试钉此处。
2. **`SandboxPreviewGateway`(product/host)**:registry(mount/unmount_root)+ `route_http(...)`(HTTP 透传,上游带 auth)+ `handle_ws(handler, token, sub)`(用 #1 反代到 `ws://base/<sub>`,上游 header 带 auth)。镜像 `PreviewGateway` 的锁/registry/limit 形状。
3. **生命周期接线(SDK↔product)**:容器 allocate(`SandboxExecEnvManager`)时,host 用 `SandboxHandle` 在 gateway 注册 preview mount(键 root_task_id);release/`shutdown` 时注销。复用现有 handle,不新增生命周期。
4. **后端路由(product/backend `app.py`)**:`do_GET` 里 `/sandbox-preview/<token>/`——`Upgrade: websocket` → hijack + `gateway.handle_ws`(**要点**:置 flag,升级后不让 handler 再发常规响应);否则 `gateway.route_http`。加 `GET /tasks/{id}/preview` 发现端点。
5. **前端(apps/web)**:`RightDock` 加 `panelType` `browser`/`terminal`/`code`(browser/code=sandbox iframe 指向 `/sandbox-preview/<token>/...`;terminal 见 R3);面板 picker;打开时 fetch `/tasks/{id}/preview`,404 隐藏;面板选择存 panel prefs。
6. **安全/生命周期/文档**:demo 红线注释;`docs/operations/limitations.md` 加预览面条目;确保注销后旧 token 404。
7. **Docker-time e2e(gated,需容器)**:钉精确 AIO 预览子路径(noVNC websockify 路径 + `?path=` 穿透、terminal 有无 HTML 页、code-server ws、`X-AIO-API-Key` 是否覆盖预览面),真容器逐面板跑通。

## Task breakdown

| # | 任务 | 层 | 依赖 / 并行 |
|---|---|---|---|
| W1 | WS 反代传输 `preview_ws.py`(握手 + 帧编解码 + pump)+ fake-socket 契约测试 | product/host | 基座,先做;无外部依赖 |
| W2 | `SandboxPreviewGateway`(registry + HTTP 透传 + WS 反代)+ 测试 | product/host | 依赖 W1 |
| W3 | 生命周期接线(allocate 注册 / release 注销,复用 SandboxHandle) | SDK↔product | 依赖 W2;镜像 exec_env 现链 |
| W4 | 后端路由(`/sandbox-preview/*` upgrade+透传 + `GET /tasks/{id}/preview`) | product/backend | 依赖 W2/W3 |
| W5 | 前端三面板(RightDock panelType + picker + 发现 fetch + 隐藏) | apps/web | 依赖 W4 端点形状 |
| W6 | 安全/生命周期/known-limitations 文档 | — | 收尾 |
| W7 | Docker-time e2e:钉精确 AIO 预览子路径 + 逐面板跑通(gated) | — | 依赖 W1–W5;需 Docker |

## Dependencies / sequencing

- **W1 是缝,先落**(反代传输 + fake-socket 测试)。W2 依赖 W1。
- **W2→W3→W4** 是「gateway→生命周期→路由/端点」主链;W3 镜像 per-session sandbox 现链。
- **W5** 依赖 W4 的 `/tasks/{id}/preview` 形状定死后即可并行开工。
- **W7** 依赖整链通,且是唯一能钉死精确 AIO 预览子路径的地方(文档模糊,必须对 live 容器)——与 browser 子系统 B8 同性质,需 Docker。
- 每步保持「无 sandbox ⇒ 端点 404、面板隐藏、字节等价回退」。

## Acceptance criteria

1. **零回归 + stable-prefix 不变**:非 sandbox 部署 `GET /tasks/{id}/preview` 返 404、三面板隐藏;无任何工具/schema/prompt 改动 → 老录制 fold/replay 字节不变。
2. **WS 反代正确**:浏览器经 `/sandbox-preview/<token>/<sub>` 的 WS 打到容器 WS,帧双向转发(fake-socket 契约测试断言编解码 + 掩码方向);`Sec-WebSocket-Protocol` 两腿协商;握手**不 offer** 压缩扩展;上游握手带 `X-AIO-API-Key`,浏览器侧响应里**无** key。
3. **HTTP 透传**:noVNC / code-server 的静态页与资源经前缀加载;上游带 auth。
4. **发现 + 生命周期**:sandboxed task 端点返 token + 三面板子路径,非 sandbox 返 404;token 不可猜;session 结束注销后同 token 的反代/透传 404。
5. **前端**:三面板经前缀 iframe(或 terminal 的 xterm.js,R3)渲染;picker 选择持久化;无 sandbox 时不出现。
6. **安全**:浏览器侧从不收到注入凭据;容器为隔离边界;localhost 绑定;demo 红线写进 known-limitations。
7. **(Docker-gated)真容器 e2e**:起 AIO 容器,逐面板打开看到实时浏览器/终端/编辑器;精确 AIO 子路径已对 live 钉。

## Risks

- **R1 手搓 WS codec 正确性**:64 位长度、掩码、跨 TCP 段半包读易错。缓解=fake-socket 单测覆盖帧编解码 + 透明转发不碰语义 + 不 offer 压缩 + 客户端(noVNC/ttyd/code-server)行为规矩 + demo 边界不抗敌意客户端。
- **R2 noVNC websockify 路径 / 前缀穿透**:noVNC 页默认可能用绝对路径连 WS(逃出 token 前缀,像 open_app 的 `/api` 绝对路径问题)。缓解=noVNC 标准的 `?path=` 参数把 WS 指到 `sandbox-preview/<token>/websockify`;精确路径 Docker-time 钉(W7)。
- **R3 terminal 可能只有 WS、无 HTML 页**:那 terminal 面板需 in-app **xterm.js** 接 `/v1/shell/ws`,而非 iframe。缓解=iframe-first(有页就 iframe),Docker-time 确认;没页则只此一面板加 xterm.js。
- **R4 `BaseHTTPRequestHandler` socket hijack**:升级后必须阻止 handler 再发常规响应(置 `_response_started`/返回哨兵),否则污染 socket。缓解=显式接管 `self.connection`,`handle_ws` 内自持读写循环、用完关连接。
- **R5 交互竞争**:人与 `web` 子 agent 同时操浏览器/共享 shell。有意介入,v1 不仲裁,文档标注。
- **R6 demo 安全边界**:浏览器腿不鉴权、仅 token 门禁——与 `PreviewGateway` 同红线,仅本地单用户可接受;非 demo 硬化(浏览器腿鉴权/SSRF/多用户)留后续。
- **R7 idle 成本 / 生命周期**:预览随 per-session 容器常驻计费(既有 limitation,无新增);注销依赖 root 终态 + `shutdown` 兜底。

## Files / areas to inspect

- **新增**:`apps/noeta-agent/noeta/agent/host/preview_ws.py`(WS 握手 + 帧编解码 + pump)、`.../host/sandbox_preview_gateway.py`(registry + 透传 + WS 反代);前端 `apps/web/src/app/` 下三面板组件 + `RightDock.jsx` panelType 扩展 + 发现 fetch。
- **复用/镜像**:`apps/noeta-agent/noeta/agent/host/preview_gateway.py`(registry/锁/limit/单端口路进模式)、`packages/noeta-runtime/noeta/tools/mcp/_http_client.py` 与 `packages/noeta-runtime/noeta/tools/fs/exec_env.py`(手搓 stdlib transport + fake-transport 测试范式)。
- **改**:`apps/noeta-agent/noeta/agent/backend/app.py`(`do_GET` 加 `/sandbox-preview/*` upgrade+透传、`GET /tasks/{id}/preview`;参考 `_maybe_preview`/`send_preview`/SSE 流式写)、`packages/noeta-sdk/noeta/client/sandbox.py` + `.../host.py`(allocate/release 处挂 preview mount 注册/注销,复用 `SandboxHandle`)、`apps/web/src/app/RightDock.jsx` + `ChatApp.jsx` + panel prefs。
- **参照**:`docs/implementation-specs/2026-07-09-sandbox-browser-subsystem.md`(姊妹活 + Docker-time 钉 wire 的同款做法)、`docs/implementation-specs/2026-07-08-per-session-sandbox.md`(per-session handle 现链)、`docs/adr/execution-environment-seam.md`(sandbox / preview 立场、demo 红线)。
```

## W7 — Docker-time e2e findings (2026-07-09, pinned live)

Pinned against a live AIO container (`all-in-one-sandbox:latest`, host port → container 8080):

| Surface | Container path | Notes |
|---------|----------------|-------|
| noVNC page | `/vnc/` or `/vnc/index.html` (200) | standard noVNC UI (`initSetting('path', 'websockify')` — honors `?path=`/`autoconnect`/`resize` query params) |
| websockify WS | `/websockify` (container **root**, not under `/vnc/`) | `Sec-WebSocket-Protocol: binary` negotiated through the proxy; first relayed frame is the `RFB 003.008` banner |
| terminal page | `/terminal` — **no trailing slash** (200) | xterm.js HTML page exists → **R3 resolved**, iframe works, no in-app xterm.js needed. The page builds its PTY WS as `new URL('v1/shell/ws', '.')`, so `/terminal/` (with slash) would aim at `terminal/v1/shell/ws` = 404 upstream |
| terminal WS | `/v1/shell/ws` (container root) | 101 through the proxy; first frame is a `{"type": "session_id", ...}` text frame; carries `?session_id=` on reconnect |
| code-server | `/code-server/` | 302 `./?folder=/home/gem` (relative — followed upstream by `route_http`'s urllib) |

**R2 confirmed live**: noVNC's default WS URL (`ws://<host>/websockify`) escapes the
token prefix — this was the "VNC cannot connect" failure. Fix shipped in
`SandboxPreviewGateway.preview_info()`: the browser panel path is now
`vnc/index.html?autoconnect=true&resize=scale&path=sandbox-preview/<token>/websockify`,
and the terminal panel path is `terminal` (no trailing slash). `app.py`'s WS branch
now passes the raw request target (query intact) to `try_handle_ws` so
`?session_id=` reaches the container. **R4 (subprotocol)** did not materialize —
`binary` negotiates cleanly on both legs.

## Revision — post-review hardening (2026-07-09)

A code review of the shipped increment surfaced four transport/security fixes,
all landed on this branch:

1. **Dedicated preview origin (supersedes the "main-port reverse proxy" in the
   title and D1).** The panel iframes require `allow-same-origin` (noVNC
   localStorage, code-server's service worker), and that flag makes iframe
   content same-origin with whatever host serves it. Proxied through the main
   port, container-controlled JS would therefore run with the noeta origin —
   cookies, `POST /tasks/{id}/approve`, the whole control plane — defeating the
   sandbox boundary it is supposed to visualize. `make_preview_server` now
   binds the gateway to its own port (`NOETA_AGENT_SANDBOX_PREVIEW_PORT`,
   default ephemeral) that serves `/sandbox-preview/<token>/...` and nothing
   else; discovery (`GET /tasks/{id}/preview`, still on the main port) gained a
   `port` field the frontend uses to build absolute iframe URLs. With every
   panel fetch now same-origin on that port, the `Access-Control-Allow-Origin:
   *` responses were removed entirely (they also let any site that learned a
   token read preview content cross-origin).
2. **WS upgrade order**: `try_handle_ws` dials the container BEFORE sending
   101, so an unreachable upstream surfaces as a real HTTP 502 instead of a
   101 followed by an abrupt close that noVNC/xterm.js cannot interpret.
3. **Frame-size cap**: `read_frame` rejects a declared payload over 64 MiB
   (`_MAX_FRAME_BYTES`) — the 8-byte extended length field otherwise lets a
   compromised endpoint grow the per-frame buffer until host memory exhausts.
4. **Pump socket tuning**: both pump legs get TCP keepalive and an
   `SO_SNDTIMEO` send bound, so a frozen browser tab (or vanished peer) fails
   the write and tears the pump down instead of wedging its thread + two FDs
   forever. Reads stay unbounded — an idle-but-healthy VNC session
   legitimately goes minutes between frames.
