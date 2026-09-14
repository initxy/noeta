# The Engine is a per-turn value; task state lives beside it; only MCP connections are pooled

Status: IMPLEMENTED 2026-09-14 (working tree), pending `make check`, live
run and a lockstep release; archive on release.
Supersedes two earlier shapes of the same date: the cache-lifecycle plan
(D1–D6), which the owner rejected ("先不用考虑历史兼容"), and the first cut
that rebuilt the Engine on every `resolve_engine` call, which review found
to (a) contradict its own "the turn is the unit" non-goal, (b) lose the
edit tools' read-first record across approvals and turns, and (c) leak
WebFetch pages across sandboxes / tenants through a URL-only process-wide
cache. The owner's decisions on 2026-09-14 ("按你推荐的来"): one Engine per
user goal; one host-owned per-task registry for what spans turns; a
host-resolved scope on the MCP pool.
Distilled into `docs/adr/engine-per-turn.md`, the `mcp-connectors` /
`workspace-and-session-path` / `execution-environment-seam` /
`model-driven-skill-invocation` amendments, and CONTEXT.md.
Owner: initxy

## Goal

1. A skill installed, removed or edited while the process runs shows up on
   every task's **next turn**, with a note the model cannot miss. The same
   holds for the project shell allowlist, the workspace trust decision, and
   an MCP server's tool list. Within a turn — across an approval, an answer,
   a sub-agent return — the tool set does not change.
2. An MCP connection has a lifecycle: shared across tasks within a
   host-chosen scope, idle-expired, closed on `Client.shutdown()`,
   reconnected on request, and never closed under a turn that still uses it.
3. What a task needs across turns — the files it has read, its compaction
   calibration, its page cache, the roster it last saw — has one home,
   keyed by task, forgotten when the conversation ends.
4. Nothing the model or a user can observe regresses.

## What the audit found

The Engine cache (`GenericEngineResolver._engines`, 256-slot LRU keyed on
thirteen binding dimensions) baked five inputs with independent lifetimes
into one object and welded the MCP subprocess to it — see the ADR's Context
for the list. Two further facts came out of the review of the first cut:

- The Engine *did* hold per-task state: `ToolRuntime`'s
  `InMemoryFileReadRegistry`, the edit tools' read-first record. On the
  cached Engine it was shared across every task on the key (task A's read
  let task B edit); on the per-call rebuild it was gone at every resume
  (`Read` → `Edit` awaiting approval → approve → "File has not been read
  yet"). Neither was the intended per-task semantic.
- Moving cross-turn state into process-wide tables produced five of them
  (`react._BASELINES`, `fetch.SHARED_PAGE_CACHE`, `SdkHost._mcp_stream_notes`,
  the skills `SkillRosterLedger`, and the read record with nowhere to go),
  each with its own lock, cap and cleanup or lack of one — and the page
  cache, keyed by URL, served one container's page to another.

## Decisions

### D1. One Engine per turn, held in the task-local registry.

A turn opens with `seed_start` / `seed_send_goal` / a background notice and
settles when the task is terminal or parked on `NEXT_GOAL_WAKE_HANDLE`.
`GenericEngineResolver.resolve_engine` is get-or-build over
`TaskLocalRegistry.held_engine`; `forget_turn_engine(task_id)` drops the
held Engine. The driver calls it in `seed_send_goal` and
`seed_notify_background_exit` (turn open) and after `interrupt`'s park; the
worker calls it after a stepped release that settled the turn
(`_settle_turn_engine`: terminal, or `wake_on ==
HumanResponseReceived(next_goal_handle)`) and in `_settle_stopped_turn`;
`forget_turn_carriers` (cancel / close) forgets the task's whole record.
`seed_start` lets its seed-time Engine go before returning its
`SeededTurn`, because a product binds per-task tenancy
(`memory_root_resolver` / `skill_menu_rank_resolver` / `mcp_scope_resolver`)
between seed and drive and the drive must resolve against it.

### D2. `TaskLocalRegistry` (`noeta.runtime.task_local`).

Host-owned (`SdkHost._task_locals`), keyed by task id (never root), one
lock, one LRU (`DEFAULT_MAX_TASK_LOCALS` = 4096), `forget(task_id)`. Holds
the turn Engine, the typed read registry (`read_registry(task_id)`), and
named slots (`slot(task_id, name, factory)` / `peek(task_id, name)`).
`bind_slot(task_id)` returns the `TaskSlot` callable a build hands its
packs as `plugin_config[<plugin>]["task_slot"]`.

- `SdkHost._tool_runtime(agent, task_id)` builds each turn's `ToolRuntime`
  (background runner, file-checkpoint gate, the agent's
  `tool_result_transforms`, the task's read registry) and passes it as
  `Engine(tool_runtime=…)` — the Engine refuses `tool_runtime` together with
  `tool_result_transforms`, so the transforms ride the runtime.
- react: `TriggerBaselines` (`(task_id, model)` table, bounded) replaces the
  module table; `build_react_policy_factory(task_slot=…)` reads the task's
  from `TRIGGER_BASELINES_SLOT`; the host binds the slot with
  `functools.partial`, so `PolicyFactoryBuilder` is unchanged. A bare policy
  keeps a private table.
- web: `PageCache` per task in `PAGE_CACHE_SLOT`, read by the session pack
  off `plugin_config["web"]["task_slot"]`; `build_web_tools(page_cache=…)`;
  `SHARED_PAGE_CACHE` is gone; a bare tool keeps a private cache.
- skills: `SkillRoster` (one task's latest / announced rosters) in
  `ROSTER_SLOT`, written by the pack off `plugin_config["skills"]["task_slot"]`,
  read by `new_skills_reminder_provider(peek)`.
- MCP provenance / skip dedupe: the host's `_note_mcp_stream` keeps its note
  in the task's `mcp.stream_note` slot.

### D3. MCP pool keyed by server identity plus a host scope.

`connection_key(spec, scope)` = `("stdio", scope, alias, argv, env)` /
`("http", scope, alias, url, headers)`; `pool.acquire(spec, scope)`;
`build_mcp_tools(pool=…, pool_scope=…)`. `HostConfig.mcp_scope_resolver`
(`task_id -> str | None`, default `None` = shared) rides the Client onto
`SdkHost.mcp_scope_resolver`; `_mcp_scope_override(task_id)` follows the
`_memory_root_override` fallbacks. `invalidate(alias)` retires the alias in
every scope. A `McpConfigError` from discovery releases the pooled client
without retiring it. Everything else in the pool (holders, idle TTL,
retire-and-reconnect-once, `reconnect_mcp`, `shutdown`, the exchange locks)
is as the first cut built it.

### D4. "New skills" note, MCP provenance once per change — unchanged in
behaviour, re-homed in the registry (D2).

### Non-goals

- Server-push MCP (`list_changed`, sampling, elicitation) — still declined.
- Mid-turn roster or tool-set changes — the turn is the unit, now enforced by
  holding the turn's Engine.
- Avoiding the by-name seed build: a first message still builds twice (the
  seed Engine that writes `TaskCreated`, then the drive's).
- Backward compatibility of the removed private surface (`_engines`,
  `_engine_cache_scope`, `_McpReapingEngineCache`, `_MAX_CACHED_ENGINES`,
  `SHARED_PAGE_CACHE`, `_BASELINES`, `SkillRosterLedger`, `roster_sink`):
  both packages ship together.

## Acceptance criteria

1. Install a skill into `.noeta/skills` between two turns of one task: the
   second turn's `skill` schema lists it, the turn's intake note names it,
   the first turn's recorded request is untouched; a second task in the same
   process sees it on its own next turn. Removing a skill drops it from the
   schema with no note. (`tests/test_engine_per_turn.py`)
2. `Read` in turn 1, `Edit` in turn 2: applied. `Read` → `Edit` awaiting
   approval → approve: applied. Task A's `Read` does not let task B `Edit`.
   (`tests/test_task_local.py`)
3. Within a turn every `resolve_engine` returns the turn's Engine (an
   approval resume builds nothing); the next goal builds afresh; a parked
   task holds no Engine; `close` forgets the task's record.
   (`tests/test_task_local.py`)
4. Each task's WebFetch page cache is its own and spans its turns; the ReAct
   calibration survives a rebuilt policy for the same task and model.
   (`tests/test_task_local.py`, `tests/test_react_policy_prune_summarize.py`)
5. Two tasks with equal bindings hold distinct Engines that compose
   byte-identical tool schemas; their MCP tools call through ONE pooled
   client per `(server, scope)`; tasks in different scopes get different
   connections; `reconnect_mcp()` retires every scope.
   (`tests/test_engine_per_turn.py`, `tests/test_mcp_lifecycle.py`)
6. `reconnect_mcp()` while a turn holds the connection: the turn's later MCP
   calls succeed; the connection closes when the turn's Engine goes; the next
   turn connects fresh. `mcp_idle_ttl=0.01`: an unused connection is closed
   on the next pool operation. `Client.shutdown()` leaves no live client; a
   build that fails after connecting leaves none; a `McpConfigError` leaves
   the connection live and unretired.
7. Concurrent `call_tool` on one stdio client from two threads: every call
   gets its own reply.
8. Provenance: one `McpProvenanceRecorded` across three turns of one task;
   a second when the alias set changes; one `McpServerSkipped` for a server
   that stays dead across turns.
9. `make check` green; docs: CONTEXT.md (Engine, Skill, Memory entries), ADR
   `engine-per-turn.md` + `mcp-connectors.md` / `workspace-and-session-path.md`
   / `execution-environment-seam.md` / `model-driven-skill-invocation.md`
   amendments, `sdk-options.md` / `sdk-client.md` /
   `how-to/multi-tenant-memory.md` en+zh, CHANGELOG. Lockstep release of
   both packages.
