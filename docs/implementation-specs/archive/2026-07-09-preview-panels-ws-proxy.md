# Frontend preview panels (browser/terminal/code) + a main-port WebSocket reverse proxy

> **Status: Shipped** — the WebSocket reverse-proxy transport
> (`noeta/agent/host/preview_ws.py`) and the session preview route are live. The
> frontend panel layout was subsequently reshaped by the server-platform migration,
> so read the UI sections here as intent rather than as the current screen.
>
> Note also the "Revision — post-review hardening" section at the end: the shipped
> design serves previews from a **dedicated port**, superseding the "main-port
> reverse proxy" in this title and in D1.

## Goal

Give a session running inside a per-session Sandbox a **live preview surface a
human can watch and interact with** — three panels, all reverse-proxied through
noeta's **main port** to the session's own container (no second port; reachable
under a VM / port-forward too):

1. **browser** — noVNC: watch and operate the real browser the `web` subagent is
   driving inside the container.
2. **terminal** — the container's PTY: watch `shell_run` output live and type
   commands by hand when needed.
3. **code** — code-server (VSCode Web): browse and edit the workspace in a real
   editor.

The shared foundation for all three is a **main-port WebSocket reverse proxy**:
the existing `PreviewGateway` is a buffered HTTP request/response, and the backend
is a stdlib `ThreadingHTTPServer` — neither carries WS, so it must be built.

## Non-goals

- **No change to any model-facing contract**: this is purely a **human UI + host
  pipeline**; no tool, schema, prompt or capability is added or changed → zero
  stable-prefix perturbation and old recordings stay byte-identical (a hard
  constraint).
- **No authentication / SSRF allowlist / multi-user isolation**: it inherits
  `PreviewGateway`'s **v1 demo red line** (localhost binding, no credentials
  injected into the browser, token gating, the container as the isolation
  boundary). Hardening is left for later (D6).
- **No WebSocket library**: a hand-written minimal RFC 6455 reverse proxy (D2),
  with zero new runtime dependencies.
- **No CDP screencast**: the browser panel uses a noVNC iframe (the user decided);
  the CDP path is kept as a later visual / low-bandwidth fallback.
- **Nothing added for non-sandbox sessions**: no container ⇒
  `GET /tasks/{id}/preview` returns 404 ⇒ the frontend hides all three panels →
  non-sandbox deployments are entirely unaffected.
- **No preview recording / playback**: the preview is **live**, pure runtime state;
  it does not enter the event log, is not persisted, and is not replayed (the same
  nature as a `PreviewGateway` mount).
- **No arbitration of conflicts between a panel and the agent**: a human and the
  `web` subagent operating the browser simultaneously will contend for control, and
  that is **deliberate human intervention**; v1 does not arbitrate (D5).

## Context

- **The backend = stdlib `http.server.ThreadingHTTPServer` +
  `BaseHTTPRequestHandler`** (`apps/noeta-agent/noeta/agent/backend/app.py`). SSE
  streams by writing chunks straight to `self.wfile`; there is **no native WS**.
  But `BaseHTTPRequestHandler` can reach the raw socket (`self.connection` /
  `self.rfile` / `self.wfile`) and runs **one thread per connection** — so
  recognising `Upgrade: websocket` in `do_GET`, completing the 101 handshake by
  hand, and running a blocking bidirectional pump is feasible (socket hijack).
- **`PreviewGateway`**
  (`apps/noeta-agent/noeta/agent/host/preview_gateway.py`) = the open_app HTML
  preview: `route(method,path,query,...) -> PreviewResponse` (**a fully buffered
  request/response**) + a token→{workspace_dir, app_rel, proxy_to, task_id} mount
  registry + the main server routing `/preview/<token>/` in via `_maybe_preview`.
  It serves **workspace files + an /api proxy to a model-declared target** — a
  different thing from "reverse-proxy to the container's **own 8080**", and
  structurally unable to hold a persistent socket. → This work **builds a new**
  `SandboxPreviewGateway` rather than extending it.
- **Container reachability**: `LocalDockerSandboxProvider` starts the AIO image
  with `-p 127.0.0.1:<port>:8080`, so `SandboxHandle.base_url` is
  `http://127.0.0.1:<port>` — **the same base_url + auth `ExecEnv` uses**. The
  reverse proxy forwards `/sandbox-preview/<token>/<sub>` to the container's
  `http://base/<sub>` (HTTP) or `ws://base/<sub>` (WS).
- **The AIO preview surfaces** (on 8080, per the official docs; exact subpaths
  pinned at Docker time, see R2/R3): browser = noVNC `/vnc/index.html` (an HTTP
  page whose live image rides websockify over **WS**); terminal = `/v1/shell/ws`
  (a **WS** PTY); code = code-server `/code-server/` (an HTTP page with internal
  **WS**); plus the `/proxy/{port}/` and `/absproxy/{port}/` HTTP proxies.
- **auth**: the container's APIs use `SandboxHandle.auth.connect_headers()`
  (`X-AIO-API-Key`). The reverse proxy adds it on the **upstream leg**'s handshake
  / requests; the browser side never sees the key.
- **The frontend = a Vite MPA** (vanilla ES + a little JSX). The right-hand
  `RightDock.jsx` is already a **persistent tab panel** (`Files | App`, with
  `panelType` stored in panel prefs; the comment says explicitly that `panelType`
  is a "generic-shell hook" — reserved for more panels). The "App" tab renders a
  `sandbox` iframe pointing at `/preview/<token>/`, driven by the `open_app` side
  effect (through the SSE event stream), with file edits triggering a reload
  (`app-preview.js`). → The three new panels are simply new `panelType`s in
  `RightDock`; browser/code = iframes, terminal see R3.
- **The thin backend's existing surface**: `POST /tasks`, `GET /tasks` (roots
  only), the SSE stream, `/file`, `/files`, `/content`, `/preview/<token>/`,
  `/tasks/{id}/artifacts`, `/tasks/{id}/images`. There is **no** session/sandbox
  information endpoint → one must be added, `GET /tasks/{id}/preview` (D4).

## Decisions

### D1 [confirmed] Build all three panels sharing one reverse proxy; browser = a noVNC iframe

- The reverse proxy (WS + HTTP pass-through) is the bulk of the work; once it
  exists all three panels are incremental. The browser panel uses a noVNC iframe
  (AIO ships the `/vnc` page + websockify), which needs the least frontend code
  and shows the real browser (including non-headless UI).

### D2 [decided on your behalf, overridable] Hand-write a minimal RFC 6455 reverse proxy, zero new dependencies

- A reverse proxy is the **simplest subset** of WebSocket: it only **forwards
  bytes** and does not interpret message semantics. So no message reassembly, no
  UTF-8 validation, **no permessage-deflate offered in the handshake** (neither
  side compresses), and backpressure comes free from "one thread per connection +
  blocking writes".
- What actually has to be written: `read_frame(sock)->(fin,opcode,payload)` +
  `write_frame(sock,fin,opcode,payload,mask)` (masking = a 4-byte XOR) + the
  server-side accept handshake (`Sec-WebSocket-Accept`) + the client-side upstream
  handshake (a random `Sec-WebSocket-Key`). opcode (data/ping/pong/close) + payload
  + FIN are **forwarded verbatim**, never interpreted. ~150–200 lines.
- **Pinned in one module + fake-socket contract tests** (mirroring the shape and
  test style of `McpHttpClient` / `AioSandboxExecEnv`), consistent with the
  repository's preference for hand-writing a stdlib transport over adding a
  library; adding a new runtime dependency to a shipped agent runs against that
  preference.
- **Must not be forgotten**: `Sec-WebSocket-Protocol` subprotocol negotiation
  (noVNC = `binary`, ttyd = `tty`, code-server depending) is **forwarded on both
  legs**; `X-AIO-API-Key` goes only on the "server→container" leg.
- **The cost**: ~200 lines of protocol code must be correct (64-bit lengths,
  masking, partial reads across TCP segments); mitigation = fake-socket unit tests
  + transparent forwarding that never touches semantics + not offering compression
  + clients (noVNC/ttyd/code-server) being well-behaved implementations + a demo
  boundary that does not defend against hostile clients.
- **Alternative (rejected)**: pull in `websocket-client` (synchronous) for the
  upstream leg — it lowers implementation risk but adds a shipped dependency, and
  what the library adds (a message API, full compliance, extensions, async) is
  exactly what a reverse proxy does not need.

### D3 [decided on your behalf, overridable] Build a new `SandboxPreviewGateway` (product layer) rather than extending `PreviewGateway`

- `PreviewGateway` is a buffered `route()->PreviewResponse` and cannot hold a
  persistent socket. Build a new `SandboxPreviewGateway`
  (`apps/noeta-agent/.../host/`, alongside it):
  - **registry**: `token -> {base_url, auth, root_task_id}`, registered when the
    container is allocated and deregistered on release (mirroring `unmount_task`);
    the token is `secrets.token_urlsafe`, unguessable.
  - **one generic reverse proxy**: `/sandbox-preview/<token>/<sub>` → if
    `Upgrade: websocket`, take the WS reverse-proxy path (hijack), otherwise pass
    HTTP through (to `base_url/<sub>`, with auth upstream). All three panels use
    it (navigate/vnc/terminal/code are just different `<sub>`s).
- **Prefer streaming for the HTTP pass-through** (code-server / noVNC assets can be
  large); v1 may degrade to buffering (acceptable), but WS is the non-degradable
  core.

### D4 [decided on your behalf, overridable] Discovery through a new thin endpoint, `GET /tasks/{id}/preview`

- Returns `200 {token, panels:{browser:<sub>, terminal:<sub>, code:<sub>}}`, or
  `404` (that task's session has no sandbox). The backend resolves task → root →
  `exec_env_ref` → the registry to find the token.
- Chosen over stuffing it into the SSE event stream: the sandbox is **session
  infrastructure, not a tool side effect**, and should not pollute the event log;
  an explicit endpoint fits the existing thin REST surface (`/file` / `/files` /
  `/artifacts`). The frontend fetches it when opening a panel and hides the panel
  on 404.

### D5 [decided on your behalf, overridable] Panels are interactive; contention with the `web` subagent for browser control is not arbitrated

- noVNC / ttyd / code-server are interactive by default, making them read-only
  would cost extra effort, and "a human can take over / assist" is a feature. When
  a human operates the browser panel they contend with the `web` subagent for
  control — **deliberate human intervention**; v1 does not arbitrate and simply
  documents it.

### D6 [confirmed] Inherit the demo security red line; a zero-regression addition

- The same v1 red line as `PreviewGateway`: localhost binding, the browser→noeta
  leg **unauthenticated** and gated only by an unguessable token, **no credentials
  ever injected into the browser**, the container as the isolation boundary, and
  deregistration when the session ends. Hardening (authenticating the browser leg,
  SSRF, multi-user) is left for later.
- Purely additive: no sandbox ⇒ the endpoint 404s ⇒ the panels hide; no
  model-facing change ⇒ the stable prefix and old recordings stay byte-identical.

## Implementation plan

1. **The WS reverse-proxy transport (product/host)**: `preview_ws.py` —
   `accept_handshake(handler) -> bool` (compute `Sec-WebSocket-Accept`, send 101,
   negotiate the subprotocol), `connect_upstream(url, headers, subprotocol) -> sock`
   (the client handshake), `pump(a, b)` (bidirectional forwarding, either `select`
   over both sockets or two threads; opcode + FIN + payload verbatim, close
   collecting both legs), plus the `read_frame` / `write_frame` frame codec. The
   fake-socket contract tests pin this module.
2. **`SandboxPreviewGateway` (product/host)**: the registry (mount/unmount_root) +
   `route_http(...)` (HTTP pass-through with upstream auth) +
   `handle_ws(handler, token, sub)` (reverse-proxying to `ws://base/<sub>` using
   #1, with auth on the upstream headers). Mirrors `PreviewGateway`'s lock /
   registry / limit shape.
3. **Lifecycle wiring (SDK ↔ product)**: when a container is allocated
   (`SandboxExecEnvManager`), the host uses the `SandboxHandle` to register a
   preview mount in the gateway (keyed by root_task_id); on release / `shutdown` it
   deregisters. Reuses the existing handle and adds no lifecycle.
4. **Backend routing (product/backend `app.py`)**: `/sandbox-preview/<token>/` in
   `do_GET` — `Upgrade: websocket` → hijack + `gateway.handle_ws` (**key point**:
   set a flag so the handler cannot send a normal response after the upgrade);
   otherwise `gateway.route_http`. Add the `GET /tasks/{id}/preview` discovery
   endpoint.
5. **Frontend (apps/web)**: add the `browser` / `terminal` / `code` `panelType`s to
   `RightDock` (browser/code = a sandbox iframe pointing at
   `/sandbox-preview/<token>/...`; terminal see R3); a panel picker; fetch
   `/tasks/{id}/preview` on open and hide on 404; persist the panel choice in panel
   prefs.
6. **Security / lifecycle / docs**: the demo red-line comments; add a preview entry
   to `docs/operations/limitations.md`; make sure an old token 404s after
   deregistration.
7. **Docker-time e2e (gated, needs a container)**: pin the exact AIO preview
   subpaths (the noVNC websockify path + `?path=` prefix traversal, whether the
   terminal has an HTML page, code-server's ws, whether `X-AIO-API-Key` covers the
   preview surfaces) and run each panel against a real container.

## Task breakdown

| # | Task | Layer | Depends / parallel |
|---|---|---|---|
| W1 | The WS reverse-proxy transport `preview_ws.py` (handshake + frame codec + pump) + fake-socket contract tests | product/host | the foundation, first; no external dependency |
| W2 | `SandboxPreviewGateway` (registry + HTTP pass-through + WS reverse proxy) + tests | product/host | depends on W1 |
| W3 | Lifecycle wiring (register on allocate / deregister on release, reusing SandboxHandle) | SDK ↔ product | depends on W2; mirrors the existing exec_env chain |
| W4 | Backend routing (`/sandbox-preview/*` upgrade + pass-through, plus `GET /tasks/{id}/preview`) | product/backend | depends on W2/W3 |
| W5 | The three frontend panels (RightDock panelType + picker + discovery fetch + hiding) | apps/web | depends on W4's endpoint shape |
| W6 | Security / lifecycle / known-limitations documentation | — | wrap-up |
| W7 | Docker-time e2e: pin the exact AIO preview subpaths + run each panel (gated) | — | depends on W1–W5; needs Docker |

## Dependencies / sequencing

- **W1 is the seam and lands first** (the reverse-proxy transport + fake-socket
  tests). W2 depends on W1.
- **W2→W3→W4** is the main chain: gateway → lifecycle → routing/endpoint; W3
  mirrors the existing per-session sandbox chain.
- **W5** can start in parallel once W4's `/tasks/{id}/preview` shape is fixed.
- **W7** depends on the whole chain and is the only place that can pin the exact
  AIO preview subpaths (the docs are vague, so it must be done against a live
  container) — the same nature as the browser subsystem's B8, and it needs Docker.
- Every step preserves "no sandbox ⇒ the endpoint 404s, the panels hide,
  byte-equivalent fallback".

## Acceptance criteria

1. **Zero regression + unchanged stable prefix**: on a non-sandbox deployment
   `GET /tasks/{id}/preview` returns 404 and the three panels hide; no tool /
   schema / prompt change → old recordings fold/replay byte-identically.
2. **The WS reverse proxy is correct**: a browser's WS through
   `/sandbox-preview/<token>/<sub>` reaches the container's WS, with frames
   forwarded both ways (fake-socket contract tests assert the codec + the mask
   direction); `Sec-WebSocket-Protocol` negotiates on both legs; the handshake
   **does not offer** a compression extension; the upstream handshake carries
   `X-AIO-API-Key` and the browser-side response contains **no** key.
3. **HTTP pass-through**: noVNC / code-server static pages and assets load through
   the prefix; upstream carries auth.
4. **Discovery + lifecycle**: a sandboxed task's endpoint returns the token + the
   three panel subpaths, and a non-sandbox task 404s; the token is unguessable;
   after the session ends and it is deregistered, the same token's
   reverse-proxy / pass-through 404s.
5. **Frontend**: the three panels render through the prefixed iframe (or the
   terminal's xterm.js, R3); the picker choice is persisted; nothing appears
   without a sandbox.
6. **Security**: the browser side never receives injected credentials; the
   container is the isolation boundary; localhost binding; the demo red line is
   written into known-limitations.
7. **(Docker-gated) real-container e2e**: start an AIO container and open each
   panel to see the live browser / terminal / editor; the exact AIO subpaths are
   pinned against live.

## Risks

- **R1 Correctness of the hand-written WS codec**: 64-bit lengths, masking and
  partial reads across TCP segments are error-prone. Mitigation = fake-socket unit
  tests covering the frame codec + transparent forwarding that never touches
  semantics + not offering compression + well-behaved clients
  (noVNC/ttyd/code-server) + a demo boundary that does not defend against hostile
  clients.
- **R2 The noVNC websockify path / prefix traversal**: the noVNC page may connect
  its WS by absolute path by default (escaping the token prefix, like open_app's
  absolute `/api` path problem). Mitigation = noVNC's standard `?path=` parameter
  points the WS at `sandbox-preview/<token>/websockify`; the exact path is pinned
  at Docker time (W7).
- **R3 The terminal may be WS-only with no HTML page**: then the terminal panel
  needs an in-app **xterm.js** connected to `/v1/shell/ws` rather than an iframe.
  Mitigation = iframe-first (use an iframe if a page exists), confirmed at Docker
  time; if there is no page, only this one panel adds xterm.js.
- **R4 `BaseHTTPRequestHandler` socket hijack**: after the upgrade the handler must
  be prevented from sending a normal response (set `_response_started` / return a
  sentinel), otherwise the socket is polluted. Mitigation = explicitly take over
  `self.connection`, with `handle_ws` running its own read/write loop and closing
  the connection when done.
- **R5 Interaction contention**: a human and the `web` subagent operating the
  browser / sharing the shell simultaneously. Deliberate intervention; v1 does not
  arbitrate and documents it.
- **R6 The demo security boundary**: the browser leg is unauthenticated and gated
  only by a token — the same red line as `PreviewGateway`, acceptable only for a
  local single user; non-demo hardening (authenticating the browser leg / SSRF /
  multi-user) is left for later.
- **R7 Idle cost / lifecycle**: the preview is resident and billed with the
  per-session container (an existing limitation, nothing new); deregistration
  depends on the root terminal + `shutdown` as a backstop.

## Files / areas to inspect

- **New**: `apps/noeta-agent/noeta/agent/host/preview_ws.py` (the WS handshake +
  frame codec + pump), `.../host/sandbox_preview_gateway.py` (the registry +
  pass-through + WS reverse proxy); on the frontend, the three panel components
  under `apps/web/src/app/` + the `RightDock.jsx` panelType extension + the
  discovery fetch.
- **Reused / mirrored**:
  `apps/noeta-agent/noeta/agent/host/preview_gateway.py` (the registry / lock /
  limit / single-port routing pattern),
  `packages/noeta-runtime/noeta/tools/mcp/_http_client.py` and
  `packages/noeta-runtime/noeta/tools/fs/exec_env.py` (the hand-written stdlib
  transport + fake-transport test paradigm).
- **Changed**: `apps/noeta-agent/noeta/agent/backend/app.py` (`do_GET` gains the
  `/sandbox-preview/*` upgrade + pass-through and `GET /tasks/{id}/preview`; see
  `_maybe_preview` / `send_preview` / the SSE streaming writes),
  `packages/noeta-sdk/noeta/client/sandbox.py` + `.../host.py` (hook preview mount
  registration/deregistration at allocate/release, reusing `SandboxHandle`),
  `apps/web/src/app/RightDock.jsx` + `ChatApp.jsx` + panel prefs.
- **Reference**:
  `docs/implementation-specs/archive/2026-07-09-sandbox-browser-subsystem.md` (the
  sister effort + the same approach of pinning the wire at Docker time),
  `docs/implementation-specs/archive/2026-07-08-per-session-sandbox.md` (the
  existing per-session handle chain),
  `docs/adr/execution-environment-seam.md` (the sandbox / preview position, the
  demo red line).

## W7 — Docker-time e2e findings (2026-07-09, pinned live)

Pinned against a live AIO container (`all-in-one-sandbox:latest`, host port →
container 8080):

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
