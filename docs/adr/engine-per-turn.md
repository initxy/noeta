# The Engine is a per-turn value; task state lives beside it; only MCP connections are pooled

## Context

A turn needs an Engine: the tool set, the composer with its control-tool
schemas, the policy, the guards. Building one reads several inputs that
change while a process runs — the skill tiers, the workspace trust decision,
the project shell allowlist, an MCP server's tool list — and connects the
turn's MCP servers, which for a stdio server means starting a subprocess.

Until 0.6.23 the host cached built Engines in a 256-slot LRU keyed on the
task's bindings, and shared one Engine across every task with equal
bindings. That saved the connect, but it baked every mutable input into the
cached object and welded the MCP subprocess to it: a skill installed
mid-process stayed invisible until a restart; an LRU eviction shut the MCP
clients down under a turn still using them; nothing expired, nothing could
be invalidated, and `Client.shutdown()` left stdio servers running; the
stdio client had no lock, so two concurrent turns on one key interleaved on
one pipe; and the MCP provenance event fired only on a cache miss, so a task
that reused another's Engine recorded none. It also hid a second defect: the
edit tools' read-first record lived on the Engine's `ToolRuntime`, so it was
shared by every task on the cache key — one task's `Read` let another task
`Edit`.

A first cut removed the cache and rebuilt the Engine on every
`resolve_engine` call. That surfaced what the cached object had been quietly
carrying across turns — the read-first record, the ReAct compaction-trigger
calibration, the WebFetch page cache, the "which skills has this task seen"
roster, the last MCP provenance emitted — and scattered it into five
process-wide tables, each with its own lock, cap and (missing) cleanup. Two
of them were wrong: the read record was gone at every rebuild, so a file
read before an approval could not be edited after it; and the page cache,
keyed by URL alone, served a page fetched through one tenant's sandbox to
another's.

## Decision

**A turn is the unit. It opens with a user goal (or a background notice)
and runs — through every approval, answer and sub-agent return that resumes
it — until the task parks on the next-goal handle or ends.** The turn's
Engine is built once, from the task's folded bindings and everything the
build consults, and every resume within the turn gets that same Engine: the
tool set a turn started with is the tool set it finishes with. When the turn
settles, the Engine goes; the next turn builds afresh, so a skill installed,
a trust decision made, an allowlist edited or an MCP tool list changed in
between is in force from then on. There is no cache, no cache key and no
invalidation verb: the driver drops the held Engine when it opens a turn
(`send_goal`, a background notice; `seed_start` lets its seed-time Engine go
so the drive resolves against the tenancy a product binds between seed and
drive), the worker drops it when a turn settles (terminal, or parked on the
next-goal handle), and `cancel` / `close` forget it with everything else. Two
tasks with equal bindings hold distinct Engines that compose byte-identical
schemas. The build is deterministic in its inputs, so the prompt-cache prefix
moves only when an input actually changed.

**What a task needs across turns lives in one task-local registry, not on
the Engine.** `TaskLocalRegistry` (`noeta.runtime.task_local`) is host-owned,
keyed by task id — never by root: a sub-agent's reads are not its parent's —
with one lock, one LRU cap and one `forget` that the conversation-end verbs
call. It holds the turn's Engine, the read-first record (typed, threaded by
the host into each turn's `ToolRuntime`), and named slots the built-ins keep
opaque objects in. A pack reaches its slot through the `task_slot` the host
binds into its `plugin_config` entry — a get-or-create already bound to the
build's task — so the runtime names no built-in type: the ReAct policy keeps
its compaction-trigger calibration there (the host binds the slot onto the
react factory with `functools.partial`, leaving `PolicyFactoryBuilder`
unchanged), the web pack its page cache, the skills pack the roster it
composed, the host its last MCP provenance. A restart starts every task's
local state empty; nothing here is recorded or resumed.

**Only the MCP connection outlives the turn, in a host-owned pool keyed by
server identity and the host's scope.** `McpConnectionPool` (the `mcp`
built-in) keys a connection on `(transport, scope, alias, argv, env)` or
`(transport, scope, alias, url, headers)`, in memory only. Two tasks naming
one server in one scope share one connection; two tenants with different
credentials do not; and a host that resolves a scope per task
(`HostConfig.mcp_scope_resolver` — a tenant id, a workspace; the same
tenancy seam as `memory_root_resolver`, `None` = shared) keeps a stateful
server's state — a browser, a login — from crossing tenants even when the
spec is byte-identical. The build acquires and lists tools; the turn's Engine
releases when the turn settles; a connection with no holder expires after
`HostConfig.mcp_idle_ttl` (default 1800 s); `Client.reconnect_mcp(alias=None)`
retires connections so the next build reconnects while a turn still holding
one keeps it; `Client.shutdown()` closes them all. Both clients serialize
their JSON-RPC exchanges on an instance lock. A tool-name collision
(`McpConfigError`) releases the pooled connection intact — it is the
operator's wiring, not the connection's fault.

**Mutable inputs are therefore read per turn, and "next turn" is the unit of
change.** A skill installed, edited or removed shows in every task's next
`skill` roster; the skills built-in additionally records a "new skills" note
on the turn the roster grew (a `turn_intake` reminder over the task-local
roster slot: silent on a task's opening turn and after a restart — Claude
Code's behaviour). An MCP server's tool-list change shows next turn because
`tools/list` runs per build on the pooled connection. MCP provenance is
emitted once per task and again when the enabled aliases change; a skipped
server is reported once per outage, both tracked in the task's local state.

## Rationale

- **The expensive thing, the mutable things and the task's things had three
  different lifetimes.** Skill index, allowlist, trust and the composer /
  policy objects cost milliseconds and must be re-read per turn; only the MCP
  connect is slow and stateful and must span turns; the read record, the
  calibration and the page cache are the task's and must span turns without
  being shared. Caching the whole Engine forced all three onto one lifetime.
  A per-turn build, a per-task registry and a per-server pool put each where
  it belongs.
- **Within a turn, reuse is correctness, not economy.** The spec's own
  non-goal is "no mid-turn tool-set change: the turn is the unit". Rebuilding
  on an approval resume would let a skill installed while the user was
  deciding change the `skill` schema in the middle of one goal — and it
  rebuilt three times for one message. Holding the turn's Engine also gives
  the ask-answer path (`_answer_codec`) its Engine for free and keeps the MCP
  lease's span equal to the turn.
- **One registry beats five tables.** Every cross-turn table that the first
  cut spawned had the same shape — keyed by task, bounded, unlocked or
  self-locked, never cleaned at conversation end — and two of them were
  wrong. The repo already had the right precedent in
  `FileCheckpointRegistry` (host-owned, keyed by task, reset by the driver at
  the turn boundary): the registry generalizes it.
- **The read record is per task, not per root.** The precondition guards
  "the model that edits has seen these bytes"; a sub-agent's read puts
  nothing in its parent's context. The old shared record was neither.
- **Per-turn reading makes staleness impossible rather than managed.** A
  fingerprint or generation counter would have re-derived "did anything
  change?" from outside; reading the input is the same cost and cannot be
  wrong.
- **Connections keyed by server plus scope.** Claude Code holds one
  connection per server for its process's life — but its process *is* one
  session. A multi-session host needs the pool to get the same shape, and a
  multi-tenant host needs the partition, which is why the scope is a host
  seam rather than a fixed dimension.

## Alternatives considered

1. **Keep the cache and add a lifecycle** — a skills generation counter and
   directory fingerprint in the cache key, `weakref.finalize` reaping, an
   idle TTL on the cache dict, an explicit invalidation verb. Rejected: six
   mechanisms to approximate what reading the inputs per turn gives
   outright, and the MCP subprocess would still ride the Engine's lifetime.
2. **Rebuild on every `resolve_engine` call, state in process-wide tables.**
   The first cut. Rejected: it contradicted the turn-as-unit non-goal, tripled
   the builds per message, and scattered task state into tables that lost the
   read record and leaked pages across tenants.
3. **Connect MCP per turn as well, no pool.** Rejected: a stdio server takes
   hundreds of milliseconds to seconds to start, and a stateful one (a
   browser, a database handle, a login) would lose its state at every turn
   boundary — a visible regression.
4. **Pool connections per session instead of per server.** Rejected: a
   server the resolver hands out identically to N sessions would still run N
   subprocesses; keying on the spec's identity plus a host-chosen scope lets
   tenants diverge and everyone else share.
5. **Refresh an MCP tool list only on reconnect.** Rejected: `tools/list` on
   a live connection is one local exchange, and running it per build is what
   makes a server-side tool change visible next turn without the server-push
   half the mcp-connectors ADR declines.
6. **Thread the task slot through `PolicyFactoryBuilder`.** Rejected for
   now: a new keyword on the protocol breaks third-party builders; binding it
   onto the react factory with a partial gives the built-in the slot and
   leaves the protocol alone.

## Consequences

- `GenericEngineResolver` carries no `_engines`, no lock table, no
  `_engine_cache_scope`; `resolve_engine` is get-or-build over the task-local
  registry, `forget_turn_engine` is the drop, `forget_turn_carriers` forgets
  the task's local state. `SdkHost` carries `_task_locals`, builds the
  `ToolRuntime` itself (the read record must outlive the Engine), and binds
  `task_slot` into `plugin_config["skills"]` / `["web"]` and onto the react
  factory.
- `HostConfig.mcp_idle_ttl`, `HostConfig.mcp_scope_resolver`,
  `Client.reconnect_mcp()`, `Client.shutdown()` closing the pool,
  `SdkHost.reconnect_mcp` / `shutdown_mcp` are the new surface.
  `build_mcp_tools(pool=…, pool_scope=…)` is the task-start path; without a
  pool it behaves as before for discovery.
- Per turn the host pays one skill-tier walk (local: milliseconds; sandbox:
  one `tree_snapshot` round trip) plus object construction; a first message
  pays it twice (the by-name seed build plus the drive's). A resume within
  the turn pays nothing.
- A parked conversation holds no Engine and no MCP lease, so the idle TTL and
  `reconnect_mcp` mean what they say on a long-lived server.
- A model switched on `send_goal` drives the *next* turn (the pre-prelude
  fold, as before); the switching turn now runs on one Engine throughout,
  approval resumes included.
- The `skill_menu_rank` per-task memo stays, for prefix stability alone.
- Cross-turn state on a Policy or Tool instance is gone by construction. A
  host-supplied `Options.policy` that keeps state across turns must key it by
  task itself.
