# noeta-sdk

The thin client (`noeta.sdk`, `noeta.client`) plus `noeta.presets` and the built-in plugin
catalogue `noeta.builtins`. Root `CONTEXT.md` holds the cross-package vocabulary; this file holds
rules that matter only when editing this package.

## Built-in layout

`packages/noeta-sdk/noeta/builtins/<name>/__init__.py` is a zero-execution `MANIFEST`; `impl/` is
the code its `ref`s point at. Importing `noeta.builtins` must import no impl module. `storage` is
declaration-only (zero contributions); its sqlite / postgres backends are reached through
`noeta.sdk.storage`. `todo_write`, `ask_user_question`, `delegation` are the control-tool
built-ins. `noeta.client` may not import a built-in, which is why host-side policy modules such as
`packages/noeta-sdk/noeta/client/webfetch_policy.py` live beside the host.

## Options

The declarative recipe (`packages/noeta-sdk/noeta/client/options.py`), compiled purely and
additively by `compile_options`; it mirrors the Claude Agent SDK parameter table.
`allowed_tools` is a replacement allowlist (`None` = full built-in set, `()` = none); control
tools are not in it — they mount on activation.
`cwd` and `provider` are wiring, excluded from identity and equality. `can_use_tool` rulings are
recorded as ordinary approval events. `agents` is a flat dict; a `__`-prefixed name compiles into
the registry but stays off the parent's spawnable roster. `max_turns` and `budget.max_iterations`
are mutually exclusive.

## HostConfig

`packages/noeta-sdk/noeta/client/host_config.py` holds every non-identity host injection: storage
triple, queue, tenancy resolvers (`memory_root_resolver`, `mcp_scope_resolver`, `write_roots`,
`skill_menu_rank_resolver`), sandbox factories, hooks, egress allowlist. It never enters
`AgentSpec` identity. Per-task resolvers are the multi-tenant seam: every turn's Engine is built
with what its task resolves to, so one tenant's resources never serve another.

## Disabled built-ins

`PluginSet.disabled_builtins` records what the caller turned off; a name absent from the set is not
a disable (`builtins=False` means no catalogue, not no capabilities) —
`packages/noeta-sdk/noeta/client/plugin_set.py`.

## Activation names

`BUILTIN_ACTIVATIONS` (in `options.py`) lists the feature bundles (`memory`, `browser`,
`skill_invocation`, `mcp`, `todo_write`, `ask_user_question`, `delegation`) plus identity-inert
built-in names. `delegation` is derived from an inline child roster; activating it is how a flat
child agent is granted the right to spawn. `spawnable` itself is derived, never activatable.

## Skills

A local workflow template at `<tier>/skills/<name>/SKILL.md`; identity is the frontmatter `name`,
not the directory. Tier merge, low to high: built-in / plugin / `extra_skill_dirs` < `~/.agents`
< `~/.noeta` < workspace `.agents` < workspace `.noeta` (see `docs/adr/044-skill-directory-tiers.md`).
The `skill` tool schema stays fixed; the listing is recorded once per task as `skill_listing`
residents (`roster`, then `added-<k>` for skills installed later), so new installs never move the
stable prefix. Budget, short-summary degradation and rank order are in
`packages/noeta-sdk/noeta/builtins/skills/impl/listing.py`. A selected skill's body is a resident
placed by its activation anchor (runtime `AGENTS.md`, Anchored placement); residents render from
state, not history, so compaction never drops it. Workspace trust is judged on the host-side
`trust_subject`, never a container path (`packages/noeta-sdk/noeta/client/host.py`). Bundled resources are never inlined: the renderer
prepends the skill's base directory and the model reads files on demand with `Read`.

## Memory

Cross-task, file-based, model-managed memory (`packages/noeta-sdk/noeta/builtins/memory/impl/`);
deliberately not a skill. Mutation only via `memory_write` (rewrite merges per frontmatter field
with what is on disk) and `memory_archive` (retire, never delete). The resident index is the
`memory` content kind, frozen per task; later changes are announced by a recorded `turn_intake`
note. Auto-recall is a track-A `reminder_provider`: tier-1 hits (the message names the page)
record the full body once per task as a resident; tier-2 hits ride only an index line. Pages the
model already read in still-visible history are skipped. Only `main` activates `memory`. Root
resolution is one chain for every consumer: `memory_root_resolver` > `memory_dir` >
`global_memory_dir` > `~/.noeta/memories`.

## Memory consolidation

An asynchronous curation pass: the reserved `__consolidation__` agent runs as an ordinary root
task on the worker pool, fed a digest of recent activity, and can merge, archive and fill gaps but
never delete or inject into a live task. `run_consolidation`
(`packages/noeta-sdk/noeta/client/consolidation.py`) debounces on a marker file written at enqueue
time; there is no scheduler. Registering the agent is host wiring, not identity.
See `docs/adr/035-memory-consolidation.md`.

## Provider adapters

`packages/noeta-sdk/noeta/builtins/providers/impl/`. Anthropic wraps host injections (origin
`system` / `memory`) in `<system-reminder>` merged into the adjacent user turn, and keeps its
prompt-cache breakpoint on the last recorded block — never on a compose-time reminder, whose bytes
the next step does not resend. OpenAI adapters render injections as system-role messages led by
`HOST_INJECTED_PREAMBLE`. The model catalog is operator-extensible (`HostConfig.extra_models`,
`register_models`); collisions refuse loudly.

## Web egress gate

`packages/noeta-sdk/noeta/client/webfetch_policy.py`: `HostConfig.webfetch_allowed_hosts` names
hosts a `WebFetch` reaches without approval; under a gating permission mode the others go through
per-call approval in the same slot as `Bash`'s allowlist. Judged on the URL's real, normalized host (userinfo discarded), matched
whole. It gates by host, never by address — an agent with `Bash` can `curl` anything, so a real
egress boundary belongs in the network or sandbox. Only non-fetchable schemes are refused.

## MCP

Connections are pooled per (server, scope) and released when the turn's Engine is dropped
(`packages/noeta-sdk/noeta/builtins/mcp/impl/pool.py`); `mcp_scope_resolver` is the tenancy seam.
Deferred tool schemas: `docs/adr/053-mcp-deferred-schemas.md`.

## Browser tool pack

The `browser` built-in's session pack: noeta-owned `browser_*` tools over the narrow
`BrowserBackend` protocol, mounted only when the session's `backends` bag has a live `"browser"`
entry and the agent activates `browser`. Not an MCP connector. Names and schemas are noeta's, so
the stable prefix never depends on the container image. Main carries no browser tools; it
delegates page work to the `web` subagent, opted in via `sandbox_browser_options()`.

## Sandbox wiring

The host implements `SandboxProvider` (`packages/noeta-sdk/noeta/client/sandbox_provider.py`) to
provision and reap per-root-task containers; config carries addressing, never a secret.
`SandboxExecEnvManager` (`packages/noeta-sdk/noeta/client/sandbox.py`) drives the host's
`SandboxProvider` and turns handles into live `ExecEnv` / `BrowserBackend`, cached per
`exec_env_ref`. In sandbox mode the skill indexer, skill scripts, workspace loaders and web
fetch/search also route through the session's ExecEnv; memory and MCP stay on the host.
