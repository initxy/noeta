# Sandbox browser subsystem: noeta-owned browser tools (layer 3) + a browser subagent (layer 4)

> **Status: Shipped** — the five noeta-owned browser tools (layer 3) and the `web` subagent
> (layer 4) are live, both gated on `Capabilities.browser` + a live sandbox.
> Durable decisions: [execution-environment-seam.md](../../adr/execution-environment-seam.md).

## Goal

Give an agent running inside a per-session Sandbox a **usable browser
capability**, landed in two layers:

1. **Layer 3 — the browser tool surface**: a set of browser tools whose
   **name/schema are owned by noeta** (`browser_navigate` / `browser_click` /
   `browser_type` / `browser_extract` / `browser_screenshot`), whose
   implementation forwards internally to the container's `/mcp` `browser_*`
   tools. The model's tool contract is pinned by noeta, so an AIO image upgrade
   that renames a tool does not perturb the stable prefix.
2. **Layer 4 — a browser subagent**: a `web` subagent (an `AgentSpec`: the browser
   tools + a browsing-specific prompt + its own context) that the main agent
   delegates web tasks to, collecting only a distilled result — isolating
   browsing's token growth inside the subagent.

## Non-goals

- **Do not mount AIO's `/mcp` as an MCP connector** (it does not enter
  `mcp_registry` and does not use the alias mechanism). The browser tools are a
  per-session tool pack that rides the exec_env construction-time injection path.
  Reasons in D2.
- **Do not take on `browser-use` / Playwright as a dependency running a foreign
  agent loop**: noeta's own ReAct loop is layer 4's decision maker. Playwright
  remains available for free as a deterministic supplement ("the model writes a
  script via `shell_run`"), which is out of this spec's scope.
- **No vision / multimodal in v1**: `browser_screenshot` returns a workspace file
  ref (viewable in the existing file panel / Lightbox) and does **not** feed the
  screenshot back to the model as image content. Vision plus its switch is left to
  **increment 2** (the seam is reserved; see D4).
- **No `/v1/browser` coordinate-level computer-use path**: in practice
  `/v1/browser/actions` offers only pixel-coordinate actions
  (`CLICK(x,y)` / `MOVE_TO` / `SCROLL(dx,dy)` / `DRAG_TO` / `HOTKEY` / `TYPING`);
  the high-level element-level semantics exist only in MCP. The coordinate path is
  kept as a **fallback** for anti-scraping / vision-only sites, not v1's main route.
- **No change to existing tools' model-facing contracts** (name/schema/description)
  → the stable prefix is unchanged (a hard constraint, `CONTEXT.md` Stable Prefix).
- **No browser tools outside sandbox mode**: no container means no browser, and
  old recordings stay byte-identical.
- **The frontend preview panel + WS reverse proxy** gets its own spec.

## Context

- **The three-layer topology**: `noeta.tools` (materials) > `noeta.runtime`
  (kernel-services) > `noeta.execution`; the SDK `noeta.client` sits above tools;
  `apps/noeta-agent` (`noeta.agent`) is on top. The browser tool pack lands in
  `noeta.tools.browser` (the materials band, alongside fs, and may import
  `noeta.tools.mcp._http_client`).
- **Facts about the AIO Sandbox browser (research + documentation evidence)**:
  - Once the container is up, a headless Chromium is **resident**, with every
    service fronted on port 8080. The browser keeps state (tabs / cookies / current
    page) alive across calls — **unlike** `shell_run` (one-shot, no resident state).
  - The **high-level, element-level, LLM-friendly** capabilities
    (`browser_navigate` / element-based `click` / `type` / `extract`) exist **only
    on the browser server behind the `/mcp` aggregation endpoint** (which most
    likely wraps Playwright/CDP internally). The docs show the tool name written
    both as `navigate` and as `browser_navigate` — **which is precisely the
    evidence of the stable-prefix drift risk**.
  - The `/v1/browser/*` HTTP surface is **coordinate-level**:
    `/v1/browser/actions` (pixel actions), `/v1/browser/screenshot` (a full-window
    capture), `/v1/browser/info` (returns `cdp_url` + viewport),
    `/v1/browser/config` (sets resolution). There is **no** selector click, **no**
    element list, and **no** markdown extraction.
  - Auth: the same as the container's other APIs, `X-AIO-API-Key`
    (`SandboxAuth.connect_headers`, D8).
- **Mechanisms noeta already has and can reuse directly**:
  - `McpHttpClient` (`packages/noeta-runtime/noeta/tools/mcp/_http_client.py`): a
    synchronous single-threaded JSON-RPC over HTTP, `initialize` + `tools/call`,
    with SSE single-response parsing, static header injection and a total_cap.
    **The browser backend uses it directly as its internal transport.**
  - `AioSandboxExecEnv` (`tools/fs/exec_env.py`): the paradigm of pinning the AIO
    file/shell wire in **one adapter + fake-transport tests** — the browser adapter
    copies that shape.
  - The full per-session sandbox handle chain:
    `SandboxExecEnvManager.resolve(exec_env_ref) -> (backend, workdir)`
    (`packages/noeta-sdk/noeta/client/sandbox.py`), where the handle holds
    `base_url` + `auth`; `_build_engine` already resolves `session_exec_env` in
    sandbox mode (`host.py:1377-1394`).
  - Tool-set assembly: `build_session_inputs`
    (`packages/noeta-runtime/noeta/execution/builder.py:862`) assembles the tool set
    from capabilities + injections, and `_build_engine` (`host.py:1341`) feeds it
    the parameters. The fs tools are injected via `exec_env=`; MCP tools via
    `mcp_tools_override`; `open_app` is conditionally mounted via `app_gateway` —
    **the browser tools follow the fs injection paradigm**.
  - `AgentSpec.Capabilities` (`packages/noeta-runtime/noeta/agent/spec.py:71`):
    the `todo_write` / `delegation` / `skill_invocation` / `memory` / `mcp` bits;
    the four official agents' presets are in
    `packages/noeta-sdk/noeta/presets/__init__.py`. Adding a `browser` bit is
    enough.
- **The existing ADR posture (which this spec must address head-on)**:
  - `execution-environment-seam.md` **alt #5** rejected "mount AIO's `/mcp` as the
    backend" — because it would introduce container tool names/schemas, perturb the
    stable prefix, and overlap fs/shell. **This spec does not violate it**: we do
    not mount an MCP connector, we do not expose AIO's schema to the model (noeta
    owns the schema), and browser is a **net addition** with no fs/shell overlap.
  - The same ADR, **line 214**, explicitly forecasts "AIO's browser as a
    follow-on native refinement" — this spec delivers exactly that.
  - `mcp-connectors.md` plus the per-session spec marked "MCP into the container"
    as a Tier 3 deferral, on the grounds that it would "add MCP methods to the seam
    and perturb the MCP tool schema". **This spec touches neither**: the browser
    backend uses an independent `McpHttpClient` as an **internal transport** and
    **adds no MCP method to the `ExecEnv` seam**; the model-facing surface is
    noeta's schema and never emits AIO's. → It threads the needle, so an ADR should
    record the position.

## Decisions

### D1 [confirmed] Layer 3 = B3: noeta owns the schema and forwards internally to the container's `/mcp`

- The browser tools' name/schema/description are **defined and owned by noeta**
  (the stable prefix is pinned by noeta).
- The implementation layer = one `AioBrowserBackend` (`noeta.tools.browser`,
  mirroring `AioSandboxExecEnv`'s shape): it holds `base_url` + `auth_headers` and
  internally uses `McpHttpClient(url=base_url+"/mcp", headers=auth())` to
  `tools/call browser_*`. **The AIO browser wire (which `browser_*` tools exist,
  their parameter names, their return structure) is pinned in this one adapter +
  its fake-transport contract tests**; if AIO renames or re-signs something, only
  this one place breaks and the tests catch it immediately, while the model-facing
  surface is untouched.
- **No runtime `tools/list` is needed**: noeta hard-codes the noeta-tool → AIO-tool
  mapping (unlike an MCP connector, which must discover dynamically), which is more
  deterministic. Only one lazy `McpHttpClient.start()` handshake is required.
- **Driving CDP directly is excluded** (async + a heavy dependency, against noeta's
  pure-stdlib synchronous discipline); the `/v1/browser` coordinate path is kept as
  a fallback (Non-goals).

### D2 [confirmed] Not an MCP connector — a per-session tool pack (riding the exec_env injection path)

- The browser tools **do not enter `mcp_registry`, take no alias, and do not enter
  the "enabled alias clean list"**. They behave like the fs tools: in
  `_build_engine`, when the session has a sandbox handle, the handle is used to
  build an `AioBrowserBackend`, passed to `build_browser_tools(backend=...)`, and
  `build_session_inputs` merges it into the tool set.
- This **does not touch the MCP layer** (it does not invent a "per-session
  dynamically synthesised connector", a concept the static registry does not
  support), and it **does not reopen the MCP = Tier 3 case**: the MCP client is
  only the backend's internal transport implementation, not a model-facing
  connector.

### D3 [confirmed] Layer 4 = the `web` subagent, opt-in per spec

- Add an official `web` subagent (an `AgentDefinition` in `presets/__init__.py`):
  - tools = the full browser tool set + `read` / `write` (to save evidence and
    results) + read-only `shell` / `webfetch` (matching explore's read-only base).
  - `capabilities=Capabilities(browser=True, skill_invocation=True)`.
  - prompt (`presets/prompts/web`): browsing-specific — emphasising the
    browser-use style loop of "use `browser_extract` to get numbered elements →
    operate on elements", returning a **distilled summary** to the parent when the
    task is done.
- **Who may delegate to it**: main / general-purpose (`delegation=True` with `web`
  in the allow-list). explore / plan do not get it.
- Whether the main agent mounts the browser tools **directly**: `main`'s
  `Capabilities.browser` defaults to **True** [decided on your behalf,
  overridable] (the main loop can also open a browser, but heavy work is best
  delegated to the `web` subagent to isolate tokens).

### D4 [confirmed] Perception v1 = text / element level; screenshot stored as a file ref; vision + switch left to increment 2

- The v1 tool set: `browser_navigate(url)` / `browser_click(ref)` /
  `browser_type(ref, text)` / `browser_extract()` (returning page text + a
  **numbered list of interactive elements**, the browser-use representation) /
  `browser_screenshot()`. Possibly also `browser_wait` / `browser_get_tabs` /
  `browser_navigate_back` (depending on what the container's MCP actually offers;
  pin against a live container at implementation time).
- **Text in, text out**: `click` / `type` locate by the **element number / ref**
  that `extract` returned, never by pixel coordinates.
- `browser_screenshot`'s result = **store the PNG in the workspace and return a
  file ref** (viewable in the existing file panel / Lightbox); it is **not** fed
  back to the model as image content.
- **Increment 2 (seam reserved, not done this round)**: feed the screenshot back to
  the model as visual content, controlled by a capability/config switch (default
  off). Because the tool schema is identical in both modes (whether vision is fed
  is runtime behaviour and does not enter the prefix), this switch **does not
  perturb the stable prefix**.

### D5 [decided on your behalf, overridable] Permissions: the browser tools are high risk and use the shell approval machinery

- The browser tools can egress to any site, so `risk_level="high"`, going through
  `PermissionGuard` / the approval predicate (the same effective_permission logic
  as `shell_run`, `host.py:1425-1452`). `bypassPermissions` lets them through; under
  default/acceptEdits, navigation/operations not on the allowlist go through HITL.
- **Alternative** (optional): fold "navigate to host X" into a host allowlist
  resembling the shell allowlist. Not done this round; recorded as a follow-up.

### D6 [confirmed] Sandbox mode only + conditional tool-set membership + stable-prefix safety

- The browser tools appear **if and only if**: the session has a sandbox handle
  (`exec_env_ref` present) **and** the agent spec has `Capabilities.browser=True`.
  Both are durable session state / static spec, so live and resume agree → the
  prefix is deterministic.
- **Resume follows the fs-tool paradigm, not the MCP paradigm**: the browser tools'
  schema is noeta-owned and **static**, so on resume the sandbox is resolved from
  `exec_env_ref` and the browser backend + tools are rebuilt as usual (exactly like
  the fs tools rebuilding from session state); the MCP machinery of "record the
  alias, pass empty on resume" is **not** needed. The recorded tool spec (noeta's
  own schema) is the durable truth, and resume is byte-identical.
- Non-sandbox / `browser=False` → no browser tools, and old recordings stay
  byte-identical.

### D7 [confirmed] Per-session resolution: `SandboxExecEnvManager` vends one more backend

- The manager already holds the per-session handle (`base_url` + `auth`). Add
  `resolve_browser(exec_env_ref) -> AioBrowserBackend` (or have `resolve` return
  the handle too and let `_build_engine` build the backend itself). This **reuses**
  the existing handle cache / attach / reconnect chain and adds no lifecycle.
- `_build_engine` (`host.py:1386-1394`) already resolves `session_ref`; resolve the
  browser backend at the same point and pass it down through
  `build_session_inputs(..., browser_backend=...)`. `None` (no sandbox /
  `browser=False`) → nothing mounted, a byte-equivalent fallback.

### D8 [confirmed] auth / wire reuse v1's D8

- The browser backend's auth uses `SandboxHandle.auth.connect_headers()` (fetched
  fresh per call, D8), matching `AioSandboxExecEnv`; the key travels only on the
  wire and never lands in a log / event / durable record.
- The AIO browser wire contract is pinned in the single `AioBrowserBackend` file +
  a fake `McpHttpClient` test (matching `AioSandboxExecEnv`'s fake-transport tests).

## Implementation plan

1. **The browser backend (runtime/materials)**:
   `noeta.tools.browser._backend.AioBrowserBackend` — holds `base_url` +
   `auth_headers` and forwards `browser_*` through an internal `McpHttpClient`;
   methods `navigate/click/type/extract/screenshot(+wait/tabs/back)`; the wire is
   pinned here plus contract tests. Define a narrow `BrowserBackend` Protocol (the
   injection point, so tests can substitute).
2. **The browser tool pack (runtime/materials)**:
   `noeta.tools.browser.__init__.build_browser_tools(backend, *, mode/permission)` —
   returns a dict of `Tool`s with noeta-owned schemas (`browser_navigate` etc.);
   `invoke()` calls the backend; the `screenshot` result is stored in the workspace
   and a ref returned.
3. **The capability bit**: add `browser: bool = False` to `AgentSpec.Capabilities`
   (`agent/spec.py`); `resolver.py` forwards `browser_enabled`.
4. **Assembly wiring**: `build_session_inputs` gains a `browser_backend` parameter
   (`builder.py:862`); when `browser_backend` is present and
   `capabilities.browser` is set → merge the browser tools in (following the
   fs→script→MCP→control append order, placing browser after fs and before MCP,
   fixing one position so ordering is stable).
5. **SDK resolution**: `SandboxExecEnvManager` vends the browser backend (D7);
   `_build_engine` builds it from the session handle and passes it down
   (`host.py`).
6. **The layer-4 subagent**: add the `web` `AgentDefinition` to
   `presets/__init__.py` + the `presets/prompts/web` prompt; add `web` to main's
   delegation roster; `web` holds `browser=True` while `main` keeps
   `browser=False` (main has no browser tools and delegates every page interaction
   to `web` — direction A, preventing main from calling `browser_*` directly and
   bypassing delegation).
7. **Permissions**: the browser tools get `risk_level="high"` and are wired to the
   approval predicate (D5).
8. **Docs + ADR + CONTEXT**: an ADR recording the position "browser is a
   noeta-owned native tool; the MCP client is internal transport only" (added to
   `execution-environment-seam.md` or as a new ADR); add the terminology to CONTEXT
   (browser tool pack / browser subagent); update known-limitations (v1 has no
   vision, the coordinate path is not built, browser idle time is billed with the
   container).

## Task breakdown

| # | Task | Layer/band | Depends / parallel |
|---|---|---|---|
| B1 | `AioBrowserBackend` + the `BrowserBackend` Protocol + AIO browser wire contract tests (fake `McpHttpClient`) | runtime/materials | the foundation, first; depends on the existing `McpHttpClient` |
| B2 | The `build_browser_tools` tool pack (noeta-owned schema + `invoke` calling the backend + screenshot stored as a ref) | runtime/materials | depends on B1 |
| B3 | The `Capabilities.browser` bit + `resolver` forwarding | runtime | parallel with B1/B2 |
| B4 | `build_session_inputs(browser_backend=)` conditionally merging into the tool set (ordering preserved) | runtime | depends on B2/B3 |
| B5 | `SandboxExecEnvManager` vending the browser backend + `_build_engine` passing it down | SDK | depends on B1/B4; mirrors the existing exec_env chain |
| B6 | The layer-4 `web` subagent (AgentDefinition + prompt + delegation allow-list; `web` holds browser=True, main keeps browser=False) | runtime/presets | depends on B2/B3 |
| B7 | Permissions (high risk + approval wiring) | SDK/runtime | depends on B2/B4 |
| B8 | Real-container e2e (gated on `NOETA_TEST_AIO_SANDBOX_URL` / local Docker): start a container → `browser_navigate`/`extract`/`click`/`screenshot` end to end; pin exact tool names/signatures against a live `tools/list` | — | depends on B1–B7 |
| B9 | Docs + ADR + CONTEXT + known-limitations | — | wrap-up |

## Dependencies / sequencing

- **B1 is the wire seam, so it lands first**; B3 runs in parallel with B1/B2.
- **B2→B4→B5** is the main chain: tools → assembly → per-session resolution.
- **B6/B7** depend on the tools being in place and can run in parallel with B5.
- **B8** depends on the whole chain and is the only place that can pin the exact
  tool names/signatures (the docs disagree on names, so it must be checked against
  a live container).
- Every step preserves "no sandbox / `browser=False` ⇒ byte-equivalent fallback".

## Acceptance criteria

1. **Zero regression**: with non-sandbox / `Capabilities.browser=False`, the full
   existing suite is green; old recordings fold/replay byte-identically (with no
   browser tools, the tool set / prefix match the pre-spec state).
2. **noeta controls the stable prefix**: the browser tool schema is noeta-owned
   static bytes; simulate AIO renaming `browser_navigate` to `navigate` (a fake
   backend) → **the model-facing tool schema is unchanged**, and only the backend
   contract test changes.
3. **B3 forwards correctly**:
   `browser_navigate/click/type/extract/screenshot` map correctly through
   `AioBrowserBackend` to the container's `/mcp` `browser_*` (with the fake
   transport asserting on the wire).
4. **Per-session isolation + resume**: two concurrent sandbox sessions have
   independent browser state; after resuming a sandbox session the browser tools
   are present as usual (rebuilt from `exec_env_ref`), and the tool-set bytes match
   the live ones.
5. **Perception v1**: `browser_extract` returns text + numbered interactive
   elements; `browser_click(ref)` / `type(ref,...)` locate by number;
   `browser_screenshot` stores into the workspace and returns a ref, and does
   **not** enter the model's multimodal input.
6. **The layer-4 subagent**: main/general-purpose can `spawn_subagent("web", ...)`;
   `web` has only the browser tools + the read-only base; browsing happens in the
   subagent's context and the parent receives only a summary. explore/plan cannot
   delegate to `web`.
7. **Permissions**: under default mode, an unauthorised navigation goes through
   approval; `bypassPermissions` lets it through.
8. **Real-container e2e (gated)**: start an AIO container and have the `web`
   subagent open a page, extract, click and screenshot end to end; the exact tool
   names/signatures are pinned against live.
9. **Docs**: the ADR records the browser position (noeta-owned + the MCP client as
   internal transport only, and the boundary against alt #5 / Tier 3); CONTEXT gains
   the terminology; known-limitations is updated.

## Risks

- **R1 AIO browser wire drift**: an image upgrade renaming or re-signing
  `browser_*` breaks `AioBrowserBackend`. Mitigation = the wire is pinned in one
  place + contract tests catch it immediately; pin the image tag (not `:latest`);
  the model-facing surface is unaffected because noeta owns the schema (which is
  exactly the payoff of choosing B3). **Now calibrated against the release source
  (see "Implementation notes" below)**, so R1 drops from "an approximate guess" to
  "an image-upgrade regression" — the names/arguments are no longer guesses.
- **R2 The high-level capabilities exist only in MCP, so the backend is tightly
  coupled to the container's `/mcp`**: if some capability is missing from MCP too
  (existing only at `/v1/browser`'s coordinate level), that action is absent in v1.
  Mitigation = v1 only promises the element-level actions MCP has; coordinates and
  vision are left to increments.
- **R3 The quality of the perception representation**: if `browser_extract`'s
  element list is not sufficiently "numbered and clickable", the model will struggle
  to operate precisely. Mitigation = verify the extract return against a live
  container at implementation time; if it falls short, post-process it into the
  browser-use numbered representation on the backend side (one place to fix, with
  no schema impact).
- **R4 Token growth**: repeated `extract` text accumulates across a browsing loop.
  Mitigation = the layer-4 subagent isolates the context and returns only a
  summary; `extract` output goes through the existing output_cap / artifact
  overflow.
- **R5 Browser idle cost**: the browser is resident with the container and holds
  resources while the session is suspended. Mitigation = it follows the per-session
  container lifetime (an existing limitation, nothing new).
- **R6 The permission surface**: the browser can egress to any site, which is a new
  outbound surface. Mitigation = high risk + approval (D5); the container is still
  the isolation boundary.

## Files / areas to inspect

- **New**: `packages/noeta-runtime/noeta/tools/browser/` (`_backend.py` =
  `AioBrowserBackend` + the `BrowserBackend` Protocol; `__init__.py` =
  `build_browser_tools` + the individual `Tool`s; `descriptions/` for the tool
  descriptions, matching `tool-description-canonical.md`).
  `packages/noeta-sdk/noeta/presets/prompts/web` (the subagent prompt).
- **Reused / mirrored**:
  `packages/noeta-runtime/noeta/tools/mcp/_http_client.py` (`McpHttpClient` as the
  internal transport), `packages/noeta-runtime/noeta/tools/fs/exec_env.py` (the
  `AioSandboxExecEnv` adapter + the fake-transport test paradigm).
- **Changed**: `packages/noeta-runtime/noeta/agent/spec.py:71`
  (`Capabilities.browser`), `packages/noeta-sdk/noeta/presets/__init__.py` (the
  `web` AgentDefinition + the delegation allow-list + main's browser default),
  `packages/noeta-runtime/noeta/execution/builder.py:862` (`build_session_inputs`
  gains `browser_backend` + conditional merging + stable ordering),
  `packages/noeta-runtime/noeta/execution/resolver.py` (forwarding
  `browser_enabled`).
- **SDK**: `packages/noeta-sdk/noeta/client/sandbox.py`
  (`SandboxExecEnvManager` vending the browser backend),
  `packages/noeta-sdk/noeta/client/host.py:1341-1569` (`_build_engine` building the
  backend and passing it down).
- **Docs/ADR/CONTEXT**: `docs/adr/execution-environment-seam.md` (add the browser
  position, echoing alt #5 / line 214), `docs/adr/mcp-connectors.md` (note that
  browser uses the MCP client as internal transport, not as a connector),
  `CONTEXT.md` (terminology), known-limitations.
- **Reference**:
  `docs/implementation-specs/archive/2026-07-08-per-session-sandbox.md` (the
  existing per-session handle chain, mirrored point by point),
  `docs/adr/tool-and-agent-catalog.md` + `docs/adr/mcp-connectors.md` (subagent
  capability / delegation / recording-determinism boundaries).

## Implementation notes

### 2026-07-09 — the AIO browser wire calibrated against the release source (the Docker-free part of B8)

With no Docker available locally, `AioBrowserBackend`'s wire constants were
**pinned accurately** from the release source AIO Sandbox actually packages,
`@agent-infra/mcp-server-browser` (`bytedance/UI-TARS-desktop`,
`packages/agent-infra/mcp-servers/browser/src/{server,tools}.ts`). The original B1
constants had been guessed from Playwright-MCP and differed from the real
container in three places, now corrected:

- **The element reference is a numeric `index`, not a string `ref`.** The container
  locates by the number `browser_get_clickable_elements` gives
  (`browser_click {index:number}`); `ref` was a wrong guess. → The model-facing
  schema changed to `index: integer` in step (nothing was committed this round, so
  changing the schema cost nothing, and it fits the `[7]` numbering `extract`
  returns to the model better anyway).
- **There is no `browser_type`**: the real tool is
  `browser_form_input_fill {index, value, clear}`, and submission uses
  `browser_press_key {key:"Enter"}`. noeta's `browser_type` fans out in the backend
  into fill (+Enter) — which is exactly what this D1 seam is for (noeta owns the
  `browser_type` name; the wire belongs to the container).
- **There is no `browser_extract`**: the real tools are `browser_get_markdown`
  (page text) + `browser_get_clickable_elements` (numbered elements). noeta's
  `extract` splices the two into "page text + a `# Interactive elements` numbered
  table" (the backend post-processing sanctioned by the spec's R3).
- **What did match**: `browser_navigate {url}` (whose **return value inlines the
  numbered elements**, so noeta's navigate passes it straight through) and
  `browser_screenshot {}` (returning `[text, image/png base64]`, which noeta parses
  correctly).

The contract tests (`test_browser_backend.py` / `test_browser_tools.py`) now
assert the calibrated wire + the `index:integer` schema, all green (full suite
3174 passed / 0 failed).

**B8 residual (still needs Docker)**: the names/args are now source-accurate, but
the **runtime return structures / behaviour** (the actual format of
`get_clickable_elements`' numbered table and whether it is sufficiently
"clickable", `form_input_fill`'s receipt text, what navigate's inlined elements
really look like, the ordering of screenshot content blocks) can only be confirmed
against a live container; R3 (perception representation quality) can only be
verified then too.

### 2026-07-09 — B9 documentation landed (English source)

- **ADR**: `docs/adr/execution-environment-seam.md` gains a "Browser subsystem
  (2026-07-09)" section — the position (layers 3/4, threading alt #5's needle, not
  reopening MCP = Tier 3, why `/mcp` rather than `/v1/browser`, the source-accurate
  wire, perception v1, permissions) + 4 browser alternatives.
- **CONTEXT.md**: Vocabulary gains the term **Browser tool pack** (covering the
  `web` subagent, the `BrowserBackend` seam, internal transport, not-a-connector +
  `_Avoid_`), placed right after `SandboxProvider`.
- **known-limitations**: `docs/operations/limitations.md` gains "Sandbox browser is
  text-level and container-scoped in v1" (three boundaries: no container means no
  browser / text-only rather than visual / billed with the container).
- **zh follow-up**: `docs/zh/operations/limitations.md` (and the site's zh mirror)
  go through a separate `translate-zh` pass per the existing workflow; this round
  touched the English source only (English frozen before translate-zh).
