# Web workspace file panel + live HTML app preview: same-origin serving dissolves CORS

## Context

The web frontend originally had only the conversation area — you couldn't see what files a session had written into the workspace, nor run the HTML the model produced as a real web page. Two capabilities are added here:

- File panel: a read-only column on the right side of the frontend, following the current session, showing the workspace file tree and a file preview.
- Live HTML app preview: an "App" tab in the right dock that runs the HTML the model wrote into the workspace as a real web page and lets it call an external API at runtime.

Both build on workspace becoming a first-class entity and the session-welded `workspace_dir` (see workspace-and-session-path.md), and both reuse tool shapes already in the tool catalog (see tool-and-agent-catalog.md). The frontend deliberately stays **zero-heavy-dependency**.

## Decision

### File panel

- **Scope = a read-only right-side column with the workspace file tree + a file preview, following the current session.** v1 non-goals: clickable file links in the conversation body / tool cards (full linkage deferred to v2), jump-to-line, in-preview editing, rich rendering, a multi-purpose tab shell, lazy-loading directories.
- **Layout: a push-in third grid column, collapsed by default, toggled by a button in the chat header, drag-resizable, degrading to an overlay on narrow screens; the panel follows the current session.** Collapsed state and width are stored in localStorage (reusing the `composer-prefs` mechanism); on the landing page (no session open) the button is disabled.
- **Data goes through two new task-level endpoints, resolved through the session-welded `workspace_dir`, reusing the `WorkspaceRoot` sandbox.** `GET /tasks/{id}/files` (a flat list of paths, skipping noise like `.git` / `node_modules`, capped at 2000 entries) and `GET /tasks/{id}/file?path=` (single-file content); out-of-bounds requests (e.g. `?path=../../etc/passwd`) are rejected.
- **The frontend builds the file tree on the fly from the flat list, without lazy-loading; hand-written, no tree library.** Read-only and ≤2000 nodes, so no virtual scrolling or drag-and-drop is needed; folders first + alphabetical, root level expanded.
- **Preview: plain text + `prism-react-renderer` highlighting (this feature's only net-new dependency).** Large files are sliced, binaries are not previewed, images show a placeholder (raw image support is added later in web-image-attach.md), md shows source, read-only; the highlight gate only highlights when ≤1500 lines and <200KB.
- **Live updates: refresh when the currently-previewed file changes** (an SSE event hitting the file being previewed triggers a re-fetch, with the hit test reusing the structured path in the tool-call arguments); the file tree refreshes at the end of a turn, and manual refresh is the fallback.
- **Shell: a very thin generic "right dock" + a single file component, with no pre-built tabs / routing / plugin mechanism.** Separating the shell from a placeholder component costs almost nothing, yet downgrades "adding a second feature" from a layout rewrite to adding one component; building an extension contract before the second feature exists is YAGNI.

### Live HTML app preview

- **The right dock gains an "App" tab that renders the model's output for real inside an iframe** (distinct from the file panel's source view); `panelType` grows from a single "File" into two tabs "File | App"; calling `open_app` automatically opens the panel and switches to the "App" tab.
- **The root mechanism that dissolves CORS: the HTML and `/api/*` are served from the same origin (the core reversal).** Rather than "a sandboxed iframe fetches the real site directly," one origin serves both the HTML (`/`) and a forwarding endpoint (`/api/*`), so `fetch("/api/xxx")` inside the HTML is a same-origin request (no CORS triggered), and the forwarding happens on the **server** (server-to-server has no CORS; credentials — of which there are none in v1 — are held by the server).
- **noeta ships its own preview gateway; apps mount at `/preview/<token>/`, no process is spawned.** `open_app` just adds one route to the gateway (serve a workspace subdirectory + forward `/api/*` to the target site); the mount is torn down when the session ends, with no orphan process. `<token>` is an unguessable per-app path, invisible across sessions.
- **Single-port revision (2026-06-18): the preview is now served by the noeta main server under `/preview/<token>/`, and the iframe uses a relative address.** The original "separate-port preview gateway" was reversed — in real deployments (VM / port forwarding / reverse proxy) only the main port is reachable, and the browser refuses to connect to a separate port. **Isolation now relies on `sandbox`**: the iframe is same-origin with noeta but uses `sandbox="allow-scripts"` (**without** `allow-same-origin`), so it runs in an opaque / null origin — it can execute JS but can't touch noeta's UI / cookies / storage; its `/api` fetch comes from a null origin, and the gateway returns `Access-Control-Allow-Origin: *`. Cost: the app can't use cookies / localStorage (acceptable for v1).
- **HTML carrier = the session workspace directory, with a transparent `/api` prefix forwarding convention.** The model writes the frontend into a workspace subdirectory with the existing `write` (multiple files naturally bypass the 64KB limit and become real files viewable as source / diff / with live refresh); the HTML always calls `/api/xxx`, and the gateway strips the `/api` prefix and forwards to `proxy_to/xxx`; **the gateway does not parse OpenAPI — it forwards transparently.**
- **A single tool `open_app({ dir, proxy_to })`, passed inline + no auth (the v1 demo boundary).** `proxy_to` is passed inline by the model and accepts no credentials; `open_app` has side effects that fold/resume cannot replay (like the fs tools); v1 does not build `close_app`, relying on auto-teardown when the session ends.
- **Live refresh reuses the file panel's change detection**: the model changes a file under `app/` → the iframe auto-reloads.

### Red lines (unbreakable)

- **The iframe content must be isolated from noeta's main UI** (after the single-port revision, via the sandbox's null origin; the original scheme relied on a separate port).
- **Credentials never pass through the model and never enter the HTML.** v1 sidesteps this with no auth; if auth is introduced later, credentials can only live in noeta's config and be injected server-side by the gateway into the forwarded request.
- **Transparent forwarding is a demo boundary with no SSRF allowlist** — for local single-machine personal use only; before pointing at a non-demo target, add an allowlist first.
- **`open_app` only mounts a static directory + transparent forwarding; it does not execute server-side code the model wrote.**

### Three trade-offs corrected at implementation

These three were finalized during implementation and originally existed only in issue comments:

- **The gateway sandbox root is pinned to the app subdirectory (`workspace_dir/app_rel`), not the workspace root.** A real socket test found that if the workspace root were the sandbox root, `app/../x` still lands inside the workspace and would be allowed, leaking other files from the same session. Pinned to the app subdirectory, a `../` escape is out-of-bounds and rejected.
- **`open_app` is mounted onto all roster agents' sessions via online injection, rather than allowed by a spec allowlist.** Going via the allowlist would mean writing `open_app` into main's spec → changing agent identity → re-pinning all recordings, too costly; instead it is allowed via "inject only on the online path" (`app_gateway is not None`), so offline (fold/resume, or tests) the gateway is None and recorded output is unchanged. Side effect: online, explore / plan also carry `open_app` (a read-only, low-risk tool, acceptable). To tighten, switch back to spec allowlist + re-pin.
- **The mount is torn down only on the hard delete `DELETE /tasks`, not on close (close allows continued chat).** After close the session can still continue, and tearing down the mount too early would break the preview in continued chat; reclamation relies on the `mount_limit=64` cap + process-exit cleanup, not on a close-triggered teardown.

## Rationale

- **The file panel uses task-level endpoints rather than reusing `/workspaces/{id}/files`**: task-level endpoints solve both bare sessions (which have a `workspace_dir` whether or not they ever entered the registry) and content reading; they are symmetric with the existing `/tasks/{id}/content` and `/artifacts`; and they don't disturb the `/workspaces/{id}/files` the `@` selector depends on (see mcp-connectors.md).
- **Hand-written tree + the lightest highlighter**: this holds the frontend's zero-heavy-dependency stance; prism-react-renderer is the lightest trustworthy read-only highlighter in today's React ecosystem (shiki is heavy / needs WASM, react-syntax-highlighter bloats easily).
- **The preview's live refresh reuses the structured path data from "clickable-link source A"**: v1 doesn't use clickable links, but the path in the tool-call arguments happens to be borrowed for the hit test.
- **The app preview sidesteps CORS via same-origin serving (the core reversal)**: the cross-origin problem is dissolved outright, so all the "null origin / CORS header / browser extension" patches become unnecessary; the gateway is entirely noeta's own code (it does not run server-side code the model wrote), so unlike a background process it doesn't need spawn + a separate port + lifecycle management — just mounting / unmounting a route.
- **Single-port revision**: a separate port is refused by the browser under VM / forwarding / reverse proxy, whereas the main port is always reachable; isolation switches from "different port" to "the sandbox's null origin," with equivalent security (the iframe can't touch noeta cookies / storage).
- **HTML carrier uses the workspace directory rather than an inline string**: it bypasses the 64KB limit, allows multiple files, becomes real files reusable by the file panel and live refresh, and can be persisted.

## Alternatives considered

1. **Do clickable file links in the conversation in v1.** Rejected: regex-scanning the body for paths has too many false positives; this round makes the panel self-sufficient with the file tree.
2. **Reuse `/workspaces/{id}/files` as the data source.** Rejected: it looks up by registry id, so a bare session with no id can't be found, and it only lists, doesn't read.
3. **An overlay drawer as default / a full-featured tree library / a heavy highlighter / an all-in-one file-manager component / building a multi-purpose tab shell in v1.** Rejected: the overlay covers the conversation area and forces back-and-forth switching; a tree library's virtual scrolling etc. are useless in a read-only scenario; a heavy highlighter bloats; an unmaintained component forces the data source to accommodate it; an extension contract built before the second feature exists is most likely wrong (YAGNI).
4. **A sandboxed iframe fetches the real site directly (the site enables CORS / a browser extension / self-configuring CORS for a null origin).** Rejected: either the real site can't be changed, or it only works on my machine, or it's awkward. Same-origin serving dissolves CORS outright.
5. **Spawn a process per app (the background-process route).** Rejected: the gateway is entirely noeta's own code and doesn't run the model's server-side code, so spawning only buys the burden of process lifecycles, port allocation, and orphan reclamation.
6. **A separate port (the original D3, overturned by the single-port revision).** Rejected: refused by the browser under VM / forwarding / reverse proxy. Changed to main port + sandbox.
7. **A tool that receives a big inline HTML string.** Rejected: it hits 64KB, can't do multiple files, and can't reuse the file panel and live refresh.
8. **Hard-bind the app preview to a dedicated agent.** Rejected: the mechanism is only `open_app` + `write` / `read`, usable by any agent; dispatching a dedicated agent is an orchestration choice, not a mechanism prerequisite.

## Consequences

- The backend host layer carries `GET /tasks/{id}/files`, `/tasks/{id}/file`, and the preview gateway's `/preview/<token>/` mount + `/api` transparent forwarding + CORS, reusing the `WorkspaceRoot` sandbox. `open_app` and its gateway-binding logic land in `noeta.tools.app`.
- The frontend, in `apps/web`: the file panel component with a hand-written file tree + prism highlighting + two-tier refresh, the "App" tab's iframe sandbox, and storing panel preferences via `composer-prefs`.
- The demo boundary is a hard constraint: transparent forwarding has no SSRF allowlist and no auth, for local single-machine use only; before opening externally, an allowlist and server-side credential injection must be added first.
- The cost of single-port + sandbox null origin is that the app can't use cookies / localStorage; scenarios needing those are currently unsupported.
