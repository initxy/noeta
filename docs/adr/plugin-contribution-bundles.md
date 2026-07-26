# Plugins are typed contribution bundles, not in-loop event hooks

## Context

Noeta's extension surfaces are already open as typed seams: Tool / LLMProvider /
Policy / Guard / Observer / ContentKindSpec are `Options` fields, host wiring is
`HostConfig`. What the system lacked was a **unit of packaging and discovery**:
a way for code outside the repo to contribute to those seams without the host
application hand-wiring every object — the role "extensions" play in pi,
"plugins" in Cline, MCP servers in goose.

Copying the dominant design (pi's imperative `ExtensionAPI`: `on("tool_call")`,
`on("tool_result")`, `on("context")`, `setActiveTools()`) collides with three
Noeta invariants:

- **Event-sourced replay**: anything that changes model input or task state
  must be recorded, or fold/resume diverges. pi delegates this discipline to
  extension authors by convention; Noeta encodes it in types.
- **Stable-prefix KV cache**: a hook that filters messages per LLM call defeats
  the locked `ThreeSegmentComposer` bet.
- **Multi-tenant server**: extension code runs with process-wide power; in a
  multi-user host that power belongs to the operator, never to session users.

## Decision

A **Plugin** is a Python module exporting a `noeta_plugin(api: PluginAPI)`
factory. The `PluginAPI` is an accumulator of **typed contributions to the
existing extension surfaces** — tools, guards, observers, a provider, content
kinds (`ContentKindSpec`), agents (`AgentDefinition`), MCP server specs, skill
directories. Loading collects contributions and **deterministically merges**
them into `Options` before `compile_options`; plugin contributions are part of
the resulting `AgentSpec` identity.

Plugins exist on **two planes**, mirroring the `Options` / `HostConfig` split:

- **Runtime plugins** (entry-point group `noeta.plugins`, or plugin
  directories) contribute to agent identity via `Options`.
- **App plugins** (entry-point group `noeta.app_plugins`) contribute host-level
  resources to the noeta-agent product: API routers, message-channel adapters,
  scheduled goal triggers, commands. They never enter `AgentSpec` identity.

Discovery and trust:

- In the multi-user server, entry points plus an explicit operator enable-list
  are the only sources. Plugins are **operator-level**; user-level
  extensibility remains MCP connectors and skills.
- Directory discovery (`~/.noeta/plugins/`, workspace `.noeta/plugins/`) serves
  local and development use; the workspace directory is trust-gated and off by
  default in server mode.

Merging is order-independent (contributions sorted by plugin name, then
contribution name); a name collision between plugins, or with the core, fails
at client build time — no silent override.

Tool-result rewriting (truncation, redaction), when it lands, is a
**ToolRuntime pipeline stage**: a pure transform applied inside the tool
execution boundary, whose output is what gets recorded. It is not a third hook
role — Guard/Observer stays exactly two roles
(guard-observer-hooks.md), and the single-writer invariant is untouched
because the tool execution path remains the sole producer of its result.

## Rationale

- **The seams already exist; only packaging was missing.** A plugin mechanism
  built as collect-and-merge over `Options` adds zero new power to the engine
  and therefore zero new consistency obligations. The engineering constraint
  "no seam without a real substitution need" is preserved: plugins substitute
  nothing; they populate.
- **Determinism survives by construction.** Because contributions compile into
  `AgentSpec` identity, two hosts loading the same plugin set produce
  byte-identical agents — replay, resume, and KV-cache reuse behave exactly as
  with hand-written `Options`.
- **The ergonomics that made pi's ecosystem work are kept** — a single file is
  a plugin, a factory receives one API object, examples are the documentation —
  while the contract underneath is declarative, so plugin authors cannot break
  invariants they never learned about.
- **Two planes keep identity clean.** Channel adapters and cron triggers must
  not perturb `AgentSpec`; the split reuses the reasoning that separated
  `HostConfig` from `Options`.

## Alternatives considered

1. **Imperative in-loop event bus (pi's `ExtensionAPI`)** — `on("tool_call")` /
   `on("tool_result")` / `on("input")` / `setActiveTools()`. Rejected: it
   re-opens the Mutator role explicitly cut in guard-observer-hooks.md, turns
   replay-safety into a documentation promise, and hands session-time mutation
   power to third-party code in a multi-tenant process.
2. **A context-filter hook (`on("context")`)**. Rejected: directly conflicts
   with the locked composer and the stable-prefix KV-cache constraint.
   Structured injection through `ContentKindSpec` is the sanctioned path. pi
   pays the cache-invalidation cost willingly (sub-1k-token prompt); Noeta made
   the opposite bet and keeps it.
3. **Config-only plugins (declarative manifests, no code)**. Rejected: cannot
   express a Guard, a provider, or a transform; the config-only niche is
   already served by MCP connectors and skills.
4. **A separate plugin repo or registry service from day one**. Rejected:
   pip/git is the registry; first-party examples live in-repo because a worked
   example corpus is the most effective plugin documentation (pi's
   `examples/extensions/` demonstrated this).
5. **User-uploaded plugin code in the server product**. Rejected: arbitrary
   code across tenants. Users extend through MCP connectors and skills;
   operators extend through plugins.

## Consequences

- The loader, `PluginAPI`, and merge land in noeta-sdk; the app-plugin host
  surface lands in noeta-agent. The engine and composer are untouched.
- `PluginAPI` mirrors `Options`: whenever a new extension surface is added to
  `Options`, deciding whether plugins may contribute to it is part of that
  surface's design.
- Changing the enabled plugin set changes `AgentSpec` identity — intended, and
  must be documented so operators expect the cache-prefix turnover.
- `examples/plugins/` becomes a maintained reference corpus; internal and
  community plugins live in external repos and install via pip/git.
- CONTEXT.md gains **Plugin** / **App Plugin** as stable terms; "plugin"
  remains an avoided word for MCP connectors (a connector is configuration,
  not code).
- Dynamic tool activation stays out of this decision; if a real plugin needs
  it, it gets its own design following the fold model (like `active_skills`),
  not a runtime mutation API.
