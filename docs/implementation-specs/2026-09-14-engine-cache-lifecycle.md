# Engine cache lifecycle: skills visible next turn, bounded MCP lifetime

Status: draft (2026-09-14). Owner decisions marked **[owner]**.

## Goal

1. A skill installed, removed or edited while the process runs shows up on the
   task's **next turn**, with no restart. Granularity is the turn, never
   mid-turn (a mid-turn schema change re-primes the prompt cache and makes a
   resumed task compose different bytes).
2. A cached Engine has a lifecycle: it can be **invalidated** by the host, it
   **expires** when idle, and evicting it never breaks a turn that is still
   running on it.

Both land in `noeta-sdk` only. The LRU map itself lives in the runtime
(`GenericEngineResolver._engine_for_agent`), but the host owns the dict it
stores into (`_McpReapingEngineCache`) and the per-task partition string
(`SdkHost._engine_cache_scope`), which is enough leverage for everything below.

## What the audit found

A cached Engine is built once per cache key (agent, model, ask flag,
workspace, provider, permission mode, MCP aliases, effort, exec-env ref,
wrapper, delegation, subtask set, `_engine_cache_scope`). Inputs it reads
**once at build and never again**:

| Input | Read where | Stale when |
| --- | --- | --- |
| Skill tiers (menu, bodies, script tools, `allowed-tools` guard facts) | `build_skills_session_pack` → `load_workspace_skills` | a skill is installed / removed / edited |
| Workspace-tier trust gate result | `_workspace_skills_trusted` (trust store is a file outside the workspace) | `grant_trust` / revoke |
| MCP server specs, `tools/list` result, live clients | `_resolve_live_mcp_tools` | server config changes, server restarts, tool set changes |
| Project shell allowlist | `load_project_shell_allowlist(workspace_dir)` | the allowlist file is edited |
| Sub-agent directory | `_subagent_directory` | host registry change (static today) |

Not baked in (already per turn or per task): workspace instructions and the
environment block (content-init hooks run every drive), memory recall
(turn-intake, host side), memory store contents (path only is fixed).

Lifecycle facts:

- **No TTL, no invalidation.** `_MAX_CACHED_ENGINES = 256`; the only removal
  is LRU overflow. `Client.shutdown()` stops workers and tears down the
  sandbox but never clears the cache, so every cached Engine's MCP stdio
  subprocesses outlive `shutdown()` and die only with the process
  (`McpStdioClient` has no `atexit` / `__del__`).
- **Eviction reaps immediately.** `_McpReapingEngineCache.popitem` calls
  `client.shutdown()` (terminate → kill) on the evicted Engine's clients. A
  lease resolves its Engine once (`run_leased_task` → `resolve_engine`) and
  holds it for every step of the turn, so an eviction that lands mid-turn
  leaves that turn with dead MCP tools (`McpError: server closed stdout`).
  Unlikely at 256 slots on a single-tenant host; the per-tenant
  `skill_menu_rank` scope (0.6.23) multiplies distinct keys, so it gets
  likelier.
- `Engine` (`noeta.core.engine`) is a plain class — weak-referenceable.

## Decisions

### D1. Skills generation, folded into the cache scope

`SdkHost` keeps `_skills_generation: int` (0 at start). `refresh_skills()`
bumps it and drops every cached Engine whose scope carries a `skills:` tag
(see D4 for why dropping is safe). `_engine_cache_scope` appends
`skills:<generation>` for an agent that activates `skill_invocation`, only
when the generation is non-zero, so a host that never refreshes composes the
exact scope strings it does today. Exposed as `Client.refresh_skills()`; a
product's install UI calls it after writing the skill.

### D2. Directory fingerprint per turn (host switch)

`HostConfig.skills_watch: bool` (default **off** — **[owner]** confirm). When
on, `_engine_cache_scope` fingerprints the skill tiers on every
`resolve_engine` (once per turn, never per step) and appends
`skills:<generation>.<fp16>`. The tier list is the same one the pack indexes
(`skills_dir` override / workspace `.noeta/skills` + `.agents/skills`,
global tiers when enabled, `extra_skill_dirs`, plugin packs), computed by a
pure helper in the skills built-in reached through a lazy `skills_impl()`
accessor (the host must not import `noeta.builtins` statically). Fingerprint
= sha256 over the sorted relative paths of every file under the tiers plus
each `SKILL.md`'s bytes, first 16 hex. Local: one `os.walk` per tier.
Sandbox: one `tree_snapshot(tiers, content_name="SKILL.md")` round trip —
the switch's documented cost per turn.

This is what makes a skill the agent installed **itself** (a `Bash` call
mid-turn, `npx skills add …`) visible next turn without host cooperation;
D1 covers the host's own install path without paying the walk.

### D3. "New skills" note on the turn they appear

Claude Code tells the model "New skills discovered in …" on the turn after
an install; the enum alone is easy to miss. Channel: the **turn-intake
seam** (`intake_reminder_providers`) — a recorded `origin="system"` turn,
resume-safe, the same channel memory auto-recall uses. The skills built-in
contributes a `reminder_provider` bound by the host to the tier list and to
an in-process `task_id → roster names last composed` map; it names the
skills added since the task's last turn (removed skills are silent — they
simply leave the enum). The map is process-local and non-durable: after a
restart the first turn announces nothing, matching Claude Code. Only when
`skills_watch` is on or the generation changed since the task's last
turn. **[owner]** wording; whether removed skills should be named too.

### D4. Reap MCP clients when the Engine is released, not when it is evicted

`_McpReapingEngineCache` registers `weakref.finalize(engine, reap, clients)`
on `__setitem__` instead of reaping on removal. Removal (LRU overflow,
`refresh_skills`, `invalidate_engines`, `clear`) only drops the cache's
strong reference; the finalizer fires when the last holder lets go — for an
idle Engine that is immediately, for one mid-turn it is the end of that
turn. `weakref.finalize` also runs at interpreter exit, so a client is never
leaked by a dropped reference. Caveat: a reference cycle through the Engine
delays the reap to the next cyclic GC pass; acceptable, documented. All
existing eviction tests keep passing with `gc`-free semantics because
`del cache[key]` on an unreferenced Engine drops its refcount to zero.

### D5. Idle expiry, inside the cache dict

`HostConfig.engine_idle_ttl: Optional[float]` seconds (default **[owner]**;
proposal 1800 s, `None` = never). The dict records a monotonic last-use
stamp on `get` / `move_to_end` / `__setitem__` and sweeps expired entries on
`__setitem__` (the resolver calls both under `_engines_lock`, so the sweep
is race-free without touching the runtime). The stamp is process-local
housekeeping, never enters context or the ledger, so the kernel's
clock-free rule is untouched. Expiry drops the reference (D4 reaps).

### D6. Explicit invalidation verb

`SdkHost.invalidate_engines(scope_tag: str | None = None)` /
`Client.invalidate_engines()`: drop every entry (tag `None`) or those whose
scope carries the tag. The blunt verb for "MCP server config changed",
"trust granted", "project allowlist edited" — the next turn rebuilds and
reconnects. `Client.shutdown()` calls it after the workers stop, closing the
MCP leak.

### Non-goals

- Mid-turn hot reload of the roster or file watching.
- A runtime change to the LRU (`_MAX_CACHED_ENGINES`, key shape): stays as
  is; this effort is sdk-only and ships as a patch.
- Re-fitting the menu budget mid-task (see the 0.6.23 amendment).

## Acceptance criteria

1. Install a skill into `.noeta/skills` between two turns of one task with
   `skills_watch=True`: the second turn's `skill` schema lists it; the
   turn-intake note names it; the first turn's recorded request is untouched.
   With the switch off and no `refresh_skills()`, the roster is unchanged
   (today's behaviour, byte-identical scope strings).
2. `client.refresh_skills()` between turns has the same effect with the
   switch off; the old Engine's MCP clients are shut down once no turn holds
   the Engine (test: hold a reference, refresh, assert alive; drop, assert
   shut down).
3. LRU overflow while a turn holds the evicted Engine: the turn's MCP tool
   calls keep working; the clients shut down when the turn's reference goes.
4. `engine_idle_ttl=0.01`: an Engine untouched past the TTL is gone on the
   next build; its clients are reaped.
5. `Client.shutdown()` leaves no cached Engine and no live MCP client.
6. Sandbox: `skills_watch` costs exactly one `tree_snapshot` per turn
   (fake exec-env counter), none per step.
7. `make check` green; docs: CONTEXT.md (Skill entry, Engine cache), an ADR
   `engine-cache-lifecycle.md` (D4/D5 are long-term), `sdk-options.md` en+zh,
   CHANGELOG.

## Open questions for the owner

- `skills_watch` default (proposal: off; Aisthon/Cerebon turn it on).
- `engine_idle_ttl` default (proposal: 1800 s).
- Should `refresh_skills()` also be reachable from inside a task (a control
  tool the agent calls after installing)? Proposal: no — D2 covers it.
