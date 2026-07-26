# Plugin architecture: mechanism, first-party plugins, and the agent app as plugin host

> **Status: Active**

Durable decisions live in
[plugin-contribution-bundles.md](../adr/plugin-contribution-bundles.md); this
spec holds the construction plan.

## Goal

Give Noeta a plugin mechanism — discoverable bundles of typed contributions on
a runtime plane (merged into `Options`) and an app plane (host resources for
noeta-agent) — and refactor the agent app to consume presets + plugins instead
of hand-wired configuration, so the agent generalizes without opening the
Engine or the Composer.

## Non-goals

- No imperative in-loop event bus, no `on("context")` message filtering, no
  `setActiveTools()`-style runtime mutation API (rejected in the ADR).
- No user-uploaded plugin code in the multi-user server; users keep MCP
  connectors + skills.
- No plugin UI framework (panels, module federation) in v1. UI extensibility
  is limited to generic rendering of plugin-registered content kinds and
  auto-generated config forms.
- No plugin registry/marketplace; pip/git is the registry.
- No dynamic tool activation in this effort — separate spec if a real plugin
  needs it (fold-based, like `active_skills`).
- No composer or engine-loop changes anywhere in this effort.

## Context

Design was converged against a capability mapping of pi's extension mechanism
(`earendil-works/pi`, `packages/coding-agent/docs/extensions.md`), goose's
permission modes, Cline's checkpoints, and OpenClaw's channel-first product
shape. Conclusion: Noeta's typed seams already cover ~70% of pi's extension
capabilities (tool/provider registration, tool-call interception via Guard,
lifecycle via Observer, custom state via EventLog/ContentChannel, subagents
natively); the genuine gaps are packaging/discovery (this spec), tool-result
transformation (M5), and app-level surfaces (M4). Context filtering is
consciously rejected.

Existing constraints honored: guard-observer-hooks.md (exactly two hook roles;
Mutator cut — hence ToolResultTransform is a ToolRuntime stage, not a hook),
single-writer-invariant.md, the locked ThreeSegmentComposer, and the
`Options` (identity) vs `HostConfig` (host wiring) split.

## Decisions

- **D1 — Plugin unit.** A module exporting `noeta_plugin(api: PluginAPI)`.
  `PluginAPI` accumulates: tools, guards, observers, provider, content kinds,
  agents, MCP server specs, skill directories. Pure accumulation; no live
  handles into the engine.
- **D2 — Two planes.** Runtime plugins (`noeta.plugins` entry-point group) →
  `Options` → `AgentSpec` identity. App plugins (`noeta.app_plugins`) →
  noeta-agent host resources: API routers under `/api/v1/ext/<plugin>/`,
  channel adapters, scheduled goal triggers, commands.
- **D3 — Discovery & trust.** Server mode: entry points + explicit operator
  enable-list only. Local/dev: additionally `~/.noeta/plugins/*.py` and
  workspace `.noeta/plugins/*.py`; the workspace directory requires a recorded
  trust grant (`~/.noeta/trust.json`-style store).
- **D4 — Deterministic merge, strict collisions.** Contributions sorted by
  (plugin name, contribution name) before merge; load order never affects the
  compiled `AgentSpec`. Any name collision (tool, agent, content kind,
  command) or a second provider fails at client build with an error naming
  both sources. No override flag in v1.
- **D5 — ToolResultTransform (M5) is a ToolRuntime pipeline stage.** Pure
  function over a tool result, applied inside the tool execution boundary;
  the transformed result is what gets recorded (transform provenance noted in
  event metadata). Not a hook role. Gated on the first real plugin need
  (redaction or truncation).
- **D6 — Agent app becomes a plugin host.** Stage 1: replace the hand-copied
  prompt strings in the host service with preset consumption (`main-web` +
  `append`). Stage 2: build the per-process Client's `Options` via preset +
  operator-configured runtime plugins. Stage 3: mount app plugins. The
  Feishu channel adapter is the first external app plugin (internal repo) and
  the plane's validation target.
- **D7 — Repo layout.** Mechanism in noeta-sdk (+ host side in noeta-agent);
  first-party examples in `examples/plugins/`; internal plugins
  (bytedance-toolpack, provider-gateway, feishu-channel) and community plugins
  in external repos.
- **D8 — Terminology.** "Plugin" is the bundle; "extension surface" remains
  the seam vocabulary. Proposed CONTEXT.md entries (land with M1):
  - **Plugin**: a discoverable bundle of typed contributions to the open
    extension surfaces, merged deterministically into `Options` before
    compile; operator-level in the server product. _Avoid_: Extension (that is
    the seam vocabulary), MCP connector (configuration, not code).
  - **App Plugin**: a plugin on the host plane contributing routers /
    channels / schedules / commands to noeta-agent; never part of `AgentSpec`
    identity.

## Plan

Dependency order; each milestone ends with `make check` green.

- [ ] **M0 — Records.** ADR `plugin-contribution-bundles.md` (done, alongside
      this spec); add to `docs/adr/index.md` (done).
- [ ] **M1 — SDK mechanism.** `PluginAPI` + loader (entry points, directories,
      trust store, enable-list) + deterministic merge into `Options` +
      collision errors + tests (identity property, order invariance, trust
      gating) + `docs/how-to/write-a-plugin.md` + `docs/reference/plugins.md`
      + CONTEXT.md terms (D8).
- [ ] **M2 — First-party example plugins** in `examples/plugins/`, each with
      tests:
      - `approval-modes` — goose-style `chat` / `approve` / `smart_approve`
        (keyed on tool `risk_level`) / `auto`, plus per-tool
        always/ask/never overrides. Doubles as the graduated-permission UX
        improvement.
      - `protected-paths` — path allow/deny Guard.
      - `git-checkpoint` — Observer that snapshots the workspace around
        mutating tool calls, with a restore path.
- [ ] **M3 — Agent app stages 1–2.** Consume presets instead of hand-written
      prompt strings (compare composed system prompt before/after; document
      intentional diffs); operator plugin configuration (settings/env +
      admin-visible list); runtime plugins merged into the per-process Client
      build.
- [ ] **M4 — App-plugin plane.** `AppPluginAPI` (routers, channel adapter
      seam, scheduled goal triggers via the existing timer wake, commands
      surfaced in the web UI); `cron-goals` example in-repo; `feishu-channel`
      built externally against it as validation.
- [ ] **M5 — ToolResultTransform** (gated on first real need): ToolRuntime
      stage + recording semantics + a redaction example plugin.
- [ ] **Release.** Minor bump proposal for sdk/runtime/agent (0.4.0) — the
      maintainer's explicit call per `docs/releasing.md`.

## Acceptance criteria

- [ ] A plugin installed via entry point and one via a trusted workspace
      directory both load; an unlisted (server) or untrusted (workspace)
      plugin does not, with a clear message.
- [ ] `Options` assembled via plugin merge compiles to an `AgentSpec`
      byte-identical to the equivalent hand-written `Options`, and is
      invariant under plugin load order (tests assert both).
- [ ] Collisions fail at client build, naming both contributors.
- [ ] `approval-modes`: the four modes produce the expected verdicts (chat
      denies tools; approve requires approval on all; smart_approve allows
      low-risk and requires approval otherwise; auto allows), with per-tool
      overrides winning; unit-tested.
- [ ] noeta-agent builds its `Options` from presets + configured plugins; the
      composed system prompt matches the previous hand-written wiring or the
      diff is documented in this spec's progress log.
- [ ] An example app plugin mounts a router and a scheduled goal fires
      end-to-end on a dev instance.
- [ ] Docs published (how-to + reference), ADR indexed, CONTEXT.md terms
      added; `make check` green at every milestone.

## Risks

- **Identity churn**: changing the plugin set changes `AgentSpec` identity and
  turns over the cache prefix — intended, but must be documented for
  operators.
- **Directory plugins are arbitrary code**: mitigated by the trust gate and
  server-mode default-off; never fully safe — say so in docs.
- **Prompt drift in M3**: preset consumption may not reproduce the
  hand-written prompts byte-for-byte; handle by comparing and documenting,
  not by silently accepting.
- **Startup failure isolation**: a broken plugin must fail the client build
  loudly, never a mid-session turn.
- **Scope creep on the app plane** (UI panels): pinned by Non-goals.

## Open assumptions (flag on review)

- **A1**: collision = hard error, no override flag in v1.
- **A2**: the transformed tool result replaces the original in the EventLog
  (provenance in metadata); the original is not stored.
- **A3**: plugin UI limited to content kinds + config forms in v1.
- **A4**: 0.4.0 minor bump — maintainer's call, not assumed.
- **A5**: term is "Plugin" (English, singular) everywhere; pi's word
  "extension" is not used for the bundle.

## Progress log

- 2026-07-25 — Spec created from the design conversation (pi / goose / Cline /
  OpenClaw survey; capability mapping; repo-layout decision to keep
  runtime + sdk + agent + web in this repo). ADR written and indexed
  alongside. No implementation started.
