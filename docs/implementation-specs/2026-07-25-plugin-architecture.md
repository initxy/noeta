# Plugin architecture: mechanism, first-party plugins, and the host contract

> **Status: Active**

Durable decisions live in
[plugin-contribution-bundles.md](../adr/plugin-contribution-bundles.md); this
spec holds the construction plan. Scope revised 2026-07-26: the agent product
is splitting into its own repository
([2026-07-26-sdk-only-repo-split.md](2026-07-26-sdk-only-repo-split.md)), so
the app-side milestones (former M3/M4) execute there; this spec keeps the
SDK-side work.

## Goal

Give Noeta a plugin mechanism — discoverable bundles of typed contributions
merged deterministically into `Options` — plus a first-party example corpus,
so any host (the split-out noeta-agent product first) assembles a
general-purpose agent through the SDK's public surface without the Engine or
the Composer opening up.

## Non-goals

- No imperative in-loop event bus, no `on("context")` message filtering, no
  `setActiveTools()`-style runtime mutation API (rejected in the ADR).
- No user-uploaded plugin code in multi-user hosts; users keep MCP connectors
  + skills.
- No app-plane implementation in this repo — each host owns its app-plane
  plugin API in its own repository (the noeta-agent repo defines routers /
  channels / scheduled triggers / commands / sandbox-provider selection).
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
transformation (M5), and app-level surfaces (each host's own). Context
filtering is consciously rejected.

Existing constraints honored: guard-observer-hooks.md (exactly two hook roles;
Mutator cut — hence ToolResultTransform is a ToolRuntime stage, not a hook),
single-writer-invariant.md, the locked ThreeSegmentComposer, and the
`Options` (identity) vs `HostConfig` (host wiring) split.

## Decisions

- **D1 — Plugin unit.** A module exporting `noeta_plugin(api: PluginAPI)`.
  `PluginAPI` accumulates: tools, guards, observers, provider, content kinds,
  agents, MCP server specs, skill directories. Pure accumulation; no live
  handles into the engine.
- **D2 — Two planes, split ownership.** Runtime plugins (`noeta.plugins`
  entry-point group) → `Options` → `AgentSpec` identity; the SDK owns this
  group, the loader, and the packaging convention (one pip package may declare
  several groups). App-plane groups and APIs are defined by each host in its
  own repository.
- **D3 — Discovery & trust.** Server-style hosts: entry points + explicit
  operator enable-list only. Local/dev: additionally `~/.noeta/plugins/*.py`
  and workspace `.noeta/plugins/*.py`; the workspace directory requires a
  recorded trust grant (`~/.noeta/trust.json`-style store).
- **D4 — Deterministic merge, strict collisions.** Contributions sorted by
  (plugin name, contribution name) before merge; load order never affects the
  compiled `AgentSpec`. Any name collision (tool, agent, content kind) or a
  second provider fails at client build with an error naming both sources. No
  override flag in v1.
- **D5 — ToolResultTransform (M5) is a ToolRuntime pipeline stage.** Pure
  function over a tool result, applied inside the tool execution boundary;
  the transformed result is what gets recorded (transform provenance noted in
  event metadata). Not a hook role. Gated on the first real plugin need
  (redaction or truncation).
- **D6 — (moved 2026-07-26)** The agent-app-as-plugin-host work (presets
  consumption, operator plugin config, app-plugin plane) moves to the
  noeta-agent repository's own spec, per the repo split.
- **D7 — Repo layout.** Mechanism in noeta-sdk; first-party examples in
  `examples/plugins/`; the reference host in `examples/reference-host/` (see
  the split spec); internal plugins (bytedance-toolpack, provider-gateway,
  feishu-channel) and community plugins in external repos.
- **D8 — Terminology.** "Plugin" is the bundle; "extension surface" remains
  the seam vocabulary. Proposed CONTEXT.md entries (land with M1):
  - **Plugin**: a discoverable bundle of typed contributions to the open
    extension surfaces, merged deterministically into `Options` before
    compile; operator-level in server-style hosts. _Avoid_: Extension (that is
    the seam vocabulary), MCP connector (configuration, not code).
  - **App Plugin**: a plugin on a host's own plane (routers / channels /
    schedules / commands); never part of `AgentSpec` identity; defined by each
    host.

## Plan

Dependency order; each milestone ends with `make check` green.

- [x] **M0 — Records.** ADR `plugin-contribution-bundles.md` written and
      indexed (amended 2026-07-26 for split ownership).
- [x] **M1 — SDK mechanism.** `PluginAPI` + loader (entry points, directories,
      trust store, enable-list) + deterministic merge into `Options` +
      collision errors + tests (identity property, order invariance, trust
      gating) + `docs/how-to/write-a-plugin.md` + `docs/reference/plugins.md`
      + CONTEXT.md terms (D8).
- [x] **M2 — First-party example plugins** in `examples/plugins/`, each with
      tests:
      - `approval-modes` — goose-style `chat` / `approve` / `smart_approve`
        (keyed on tool `risk_level`) / `auto`, plus per-tool
        always/ask/never overrides.
      - `protected-paths` — path allow/deny Guard.
      - `git-checkpoint` — Observer that snapshots the workspace around
        mutating tool calls, with a restore path.
- [ ] **M3 / M4 — moved.** Former agent-app milestones execute in the
      noeta-agent repository after the split (its own spec); the SDK-side
      prerequisites (public-surface audit, reference host) are Phase 1 of the
      split spec.
- [ ] **M5 — ToolResultTransform** (gated on first real need): ToolRuntime
      stage + recording semantics + a redaction example plugin.
- [ ] **Release.** Minor bump proposal for runtime/sdk (0.4.0) — the
      maintainer's explicit call per `docs/releasing.md`; this release is the
      hard gate for the repo split's Phase 2.

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
- [ ] The reference host loads a plugin end-to-end (contract test — shared
      with the split spec's Phase 1).
- [ ] Docs published (how-to + reference), ADR indexed, CONTEXT.md terms
      added; `make check` green at every milestone.

## Risks

- **Identity churn**: changing the plugin set changes `AgentSpec` identity and
  turns over the cache prefix — intended, but must be documented for
  operators.
- **Directory plugins are arbitrary code**: mitigated by the trust gate and
  server-mode default-off; never fully safe — say so in docs.
- **Startup failure isolation**: a broken plugin must fail the client build
  loudly, never a mid-session turn.
- **Cross-repo timing**: hosts consume the mechanism only through released
  wheels once the split lands — sequencing owned by the split spec.

## Open assumptions (flag on review)

- **A1**: collision = hard error, no override flag in v1.
- **A2**: the transformed tool result replaces the original in the EventLog
  (provenance in metadata); the original is not stored.
- **A3**: plugin UI limited to content kinds + config forms in v1 (host-side
  concern after the split).
- **A4**: 0.4.0 minor bump — maintainer's call, not assumed.
- **A5**: term is "Plugin" (English, singular) everywhere; pi's word
  "extension" is not used for the bundle.

## Progress log

- 2026-07-25 — Spec created from the design conversation (pi / goose / Cline /
  OpenManus / OpenClaw survey; capability mapping; layout decision to keep the
  product in-repo). ADR written and indexed alongside.
- 2026-07-26 — Owner decided to split the agent product into its own repo
  (see 2026-07-26-sdk-only-repo-split.md). M3/M4 handed to the new repo;
  goal/non-goals/D2/D6/D7 revised; reference host added as the integration
  bed; sandbox-provider plugins confirmed as app-plane (host-owned);
  knowledge-source sync adapters noted as a future host-plane contribution
  target.
- 2026-07-26 — M1 + M2 landed. SDK mechanism shipped in
  `noeta.client.plugins` (`PluginAPI` + `load_plugins` + `merge_plugins` +
  the host-plane accessors + trust store), re-exported through `noeta.sdk`,
  with `tests/test_plugins.py` covering the identity property, order
  invariance, collisions, and trust gating. All three first-party example
  plugins (`approval-modes`, `protected-paths`, `git-checkpoint`) are in
  `examples/plugins/`, each packaged with a `pyproject.toml` entry point, a
  README, and a test. Docs published: `docs/how-to/write-a-plugin.md` +
  `docs/reference/plugins.md`, both registered in the VitePress nav (en);
  CONTEXT.md gained the D8 terms (Plugin, App Plugin). Remaining: M5
  (ToolResultTransform, still gated) and the release bump.
