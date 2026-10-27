---
status: active
created: 2026-10-26
branch: next
---

# Noeta 1.0: one package, one writer per task, one extension interface

## Goal

Noeta 1.0 replaces the 0.6 kernel with a smaller one whose correctness does not depend on leases,
heartbeats or clocks, and whose single-machine and multi-machine deployments differ only in the
storage and scheduler implementations they plug in. It ships as one PyPI package, `noeta`, that
also carries what `noeta-agent` provided (a terminal UI, an HTTP server, the benchmark adapter),
so `noeta-agent` is retired.

Concretely, at 1.0:

- A task's log has exactly one writer at a time, enforced by the append itself.
- Everything outside the running task talks to it through its inbox.
- Hosts, the TUI and the web client use one client API, in-process or over HTTP.
- Every capability beyond the core loop is an extension built on one `Extension` interface.
- `make eval` and the Terminal-Bench sample are no worse than 0.6 on the same model.

## Non-goals

- Changing language. Python stays (hosts are Python; the bugs being fixed are ordering and
  crash-window bugs that a TypeScript port would carry over).
- Deterministic workflow replay (ADR-030). Fan-out and joins are subtasks plus the inbox; a
  workflow layer can be rebuilt on that later if a host needs it.
- Migrating 0.6 stores, in place or by export/import (owner: not migrated).
- New agent features (goal auto-continue, timer tool, stop hook, deadline budget). They land as
  extensions after 1.0.
- A desktop app.

## Context

Measured on `fix/audit-closure-2026-10-26` @717e437:

- 84k source lines (runtime 35k, sdk 49k). Seven files over 2,000 lines: `driver.py`,
  `host.py`, `_decision_handlers.py`, `worker.py`, `client.py`, `react.py`, `engine.py`.
- 59 event types, 72 ADRs. ADRs 065–072 all patch crash, cancel or idempotency corner cases.
- Configuration surface: 82 `Options` fields, 65 `HostConfig` fields, 58 `Protocol` classes,
  five resolvers, plus plugin bundles and activation identity.
- Hosts (host-app, host-app-2, noeta-agent) call only `create_task`, `send_goal`,
  `inject_goal`, `task_answer` and `subscribe`, plus one `workflow(` in noeta-agent. No host uses
  Postgres, `deliver_event` / `WaitExternal` or worker queues. The owner still wants
  multi-machine deployment later, so it has to be designed in, not dropped.

Root causes of the recurring bugs:

1. **Several writers per log.** `EventLogWriter.emit` (leased) and `system_emit` (no lease, no
   `expected_seq`) both append to a task's stream. Hosts append goals and cancels, and children
   append to their parent. Fold then has to reconcile them; the cross-process cancel that
   `TaskSuspended` overwrote is this bug.
2. **Leases decide correctness.** Idempotency is keyed by `(lease_id, key)`, an `InvalidLease`
   cancels a step, and the heartbeat has a cap. A liveness signal ends up deciding outcomes.
3. **Two copies of task status.** There is `dispatcher_tasks.status` and the folded status.
4. **Two idempotency mechanisms.** There is the `idempotency` table and the
   `IdempotencyKeyClaimed` events (ADR-070, 072), because the table is tied to leases.
5. **4 KB inline cap** (`EVENT_PAYLOAD_MAX_BYTES`). Events carry 19 kinds of `*_ref` fields into
   `content`. Readers fetch them back (`get_many`), the table grows without bound (the 8.7 GB
   host-app store), and every new event decides what to split out.
6. **Many extension mechanisms** that overlap.

## Decisions

### Packaging and versioning

- One distribution, `noeta` (free on PyPI), version 1.0.0, import root `noeta`.
- Optional extras:
  - `noeta[postgres]`: Postgres store and scheduler.
  - `noeta[server]`: HTTP and SSE server.
  - `noeta[tui]`: the Textual terminal UI and the `noeta` command.
  - `noeta[sandbox]`: container exec env.
  - `noeta[bench]`: the harbor adapter.
- `noeta-runtime` / `noeta-sdk` freeze at 0.6.x and get security fixes only.
- `noeta-agent` is archived after its parts are moved (Plan phase 4).
- Development happens on the branch `next` in this repo, in `packages/noeta/`, next to the 0.6
  packages, so tools, providers and tests can be moved file by file. The 0.6 packages are deleted
  at cutover.

### Concurrency model

- The runner, the loop's I/O shell, providers, tools and the client are `asyncio`. Cancel is
  task cancellation, not a polled `should_abort`. Parallel tool calls are `asyncio.gather`.
  Streaming and `watch` are async iterators. The TUI (Textual) and the server (ASGI) are asyncio
  too.
- A sync facade for hosts that are not async (a private event-loop thread) is built during host
  migration (phase 5), only if a host needs it.
- Blocking work (sqlite calls, file reads, subprocess waits that have no async API) runs in
  `asyncio.to_thread`.

### Module layout (import direction is top to bottom only; enforced by `lint-imports`)

```
noeta.client      Client API (in-process or HTTP); the only surface hosts, TUI and web use
noeta.server      HTTP + SSE on top of noeta.client           [server]
noeta.tui         Textual app + `noeta` command                [tui]
noeta.ext.*       extensions: permissions, subtasks, compaction, memory, skills, mcp,
                  background_shell, todo, web, ask_user, hooks
noeta.tools       Read, Write, Edit, Bash, Grep, Glob (ported from 0.6)
noeta.providers   anthropic, openai_compat, openai_responses (ported, with 0.6 quirk handling)
noeta.runner      claims a task, runs the loop, writes the log
noeta.loop        pure core: state + event -> state; state -> next action; build_request
noeta.store       Store interface + sqlite (+ postgres)
noeta.execenv     ExecEnv interface + local (+ container)
noeta.types       messages, events, inbox messages, ids
```

`noeta.loop` does no I/O. It is tested without storage, with recorded model responses.

### Storage model

Four tables. Postgres uses the same schema with `jsonb` / `bytea`.

| table | columns | purpose |
|---|---|---|
| `events` | `task_id, seq, type, at, data` (JSON) | the task log; PK `(task_id, seq)` |
| `inbox` | `task_id, seq, kind, at, data, dedupe_key` | messages to a task; unique `(task_id, dedupe_key)` |
| `tasks` | `task_id, parent_id, root_id, status, waiting_for, ready_at, owner, owner_epoch, queue, last_seq, inbox_read_seq, created_at, updated_at` | queryable task state and scheduling |

- **Payloads are stored inline at any size**, images included (decided during phase 1: no blob
  table until a measured need). Full model request bodies are not stored; a request hash and
  usage are.
- **`tasks` is updated in the same transaction as every append.** There is one truth: the row is
  a projection of the log, written atomically with it, never by anyone else (except the
  scheduler columns, see below).
- **The append is conditional.**
  `append(task_id, events, expected_seq, owner_epoch) -> new_seq` fails with `Conflict` unless
  `tasks.last_seq == expected_seq` and `tasks.owner_epoch == owner_epoch`.
- **The inbox is the only way in.** `post(task_id, kind, data, dedupe_key=None) -> inbox_seq` is
  idempotent on `dedupe_key`. This replaces `send_goal` / `inject_goal` keys, `system_emit`,
  child-to-parent appends and `deliver_event`.
- Envelope metadata (actor, trace and causation ids) lives inside `data` where it is needed,
  not as eight columns on every row.

### One writer per task

- A runner holds a claim `(task_id, owner_epoch)` from the scheduler. Every write it makes is a
  conditional append. When it gets a `Conflict`, it stops at once without writing anything more.
- **No side effect without a successful append first.** `ToolStarted` is appended before a tool
  runs, and a model call's intent before the call. A runner that lost its claim therefore cannot
  start new work; only an in-flight call can finish, and its result append fails.
- Crash recovery reads the log:
  - a tool with `ToolStarted` but no `ToolFinished` gets a synthesized "interrupted; outcome
    unknown — check before retrying" result and is never re-run;
  - an interrupted model call is simply made again.

  This is ADR-065's rule, with nothing else needed.
- Leases live only in the claim columns of `tasks` and only answer one question: "is this
  owner still alive, or should the task go to another runner?" A wrong answer costs a wasted
  retry, never a double write.

### Claims and wake-ups (part of the store)

- Claims live in the store, not a separate scheduler interface:
  - `claim(worker, queues, lease_seconds)` takes one claimable task and bumps `owner_epoch`;
  - `renew` extends the lease;
  - `release` hands the task back.
- `Store.on_change(callback)` reports committed changes. A worker's serve loop and the
  runner's cancel watch wait on it, with polling as the fallback for other processes.
- **Postgres:** `SELECT … FOR UPDATE SKIP LOCKED` on `tasks`, with `LISTEN/NOTIFY` feeding
  `on_change`.
- Multi-machine = the Postgres store + N processes running `noeta worker`. Nothing above
  `noeta.store` knows the difference.

### Events (complete list for the core)

Event types:

- `TaskCreated`: agent, model, tool set, parent, extension config.
- `InputTaken`: the inbox range consumed and the resulting messages. This is how exactly-once
  consumption is recorded.
- `ModelCalled`: request hash, model, attempt.
- `ModelReplied`: assistant content (text, thinking with provider family, tool calls), usage,
  stop reason.
- `ToolStarted` / `ToolFinished`: call id, result content, `is_error`.
- `Compacted`: summary and the boundary seq.
- `Waiting`: one of approval, question, children, timer, external, or idle (turn ended, awaiting
  input).
- `TaskEnded`: done, failed or cancelled, plus the answer or error.
- `ExtensionRecorded`: extension name, kind, data. Extensions persist their state here and fold
  it themselves; the core never adds an event type for an extension.

Inbox kinds: `message` (with `when: now | next_turn`), `cancel`, `answer`, `child_ended`, `timer`,
`external`.

### Cancel, approval, subtasks

- **Cancel:** `post(cancel)` plus `scheduler.wake`. A runner watching the task interrupts the
  in-flight model or tool call, takes the cancel at the next boundary and appends
  `TaskEnded(cancelled)`. A cancel posted to a task with no runner is taken by whoever claims it
  next. Terminal is irreversible because nothing but the owner writes the log.
- **Approval:** an extension's `before_tool` returns `ask`. The runner appends
  `Waiting(approval)` and releases the task. `answer` in the inbox wakes it.
  **Deny wins:** every extension is asked; any deny denies (ADR-068).
- **Subtasks:** a child is an ordinary task with `parent_id`. The parent appends
  `Waiting(children=[…])`. A child's `TaskEnded` posts `child_ended` to the parent's inbox
  (keyed by child id). This is the same path on one machine or many.

### Extension interface (the only extension mechanism)

```python
class Extension:
    name: str
    def tools(self, ctx) -> Sequence[Tool]: ...
    def context(self, ctx) -> Sequence[ContextPart]: ...   # system / resident / reminder parts
    def before_model(self, ctx, request) -> ModelRequest: ...
    def before_tool(self, ctx, call) -> Allow | Deny | Ask | Rewrite: ...
    def after_tool(self, ctx, call, result) -> ToolResult: ...
    def on_event(self, ctx, event) -> None: ...             # observers, metrics, UI
    def reduce(self, state, recorded) -> state: ...         # fold of its own ExtensionRecorded
    def commands(self) -> Sequence[Command]: ...            # slash commands for TUI / server
```

- All methods are optional.
- `ctx` gives read access to the task state and `record(kind, data)` to append an
  `ExtensionRecorded`.
- Agents are configured with `Agent(model, system_prompt, tools, extensions, …)`.
- A preset bundles a list of extensions; the default preset reproduces 0.6 behavior.
- Host-supplied functions (memory root, MCP servers, model resolution) are constructor arguments
  of the extension that needs them, not global resolvers.

### Building the request

- `build_request(state, parts) -> ModelRequest` is one pure function.
- It owns prompt-cache breakpoint placement, clearing of old tool results (ADR-066 batching),
  and the compaction trigger (Claude Code margin).
- Snapshot tests pin the exact request for a set of recorded logs.
- Compaction is an extension that runs the summarize call and records `Compacted`.

### Client API (hosts, TUI, server, web)

```python
client = noeta.connect("sqlite:///tasks.db")       # or "postgres://…", or "http://host:port"
task = client.create(agent, input=…, dedupe_key=…)
client.send(task, text, when="now" | "next_turn", dedupe_key=…)
client.answer(task, request_id, answer)
client.cancel(task)
async for ev in client.watch(task, after=…): …       # recorded events, live
client.tasks(parent_id=…, status=…, limit=…)          # backed by the tasks table
```

Live model output (deltas) is a separate, best-effort channel: `LocalApp.deltas(sink)` in process,
`event: delta` on the server's SSE stream, `RemoteClient.on_delta` remotely. Deltas are never
recorded; the recorded reply is the truth.

```python
```

- 0.6 → 1.0 mapping for hosts:
  - `send_goal` → `send(when="next_turn")`
  - `inject_goal` → `send(when="now")`
  - `task_answer` (the last turn's answer) → `Waiting.detail["answer"]` / `TaskEnded.answer`
    (`answer` is the reply to an approval or question, 0.6's `approve` / `deny` / `answer`)
  - `subscribe` → `watch`
  - `create_task` → `create`
- `connect("http://…")` gives the same API through `noeta.server`. The TUI uses this API either
  in-process (local) or against a server (remote / multi-machine).

### TUI and other front ends

- **The TUI is a client, not an agent extension.** It talks to the agent only through
  `noeta.client`, so it runs in-process (local) or against `noeta.server` (remote,
  multi-machine), and any other front end can use the same API.
- **Extensions add UI only as declared data.** Extensions contribute:
  - slash commands;
  - tool-result renderers keyed by tool name, which return a small UI-neutral tree (markdown,
    code, diff, table, progress, key-value);
  - status items.

  No extension ships Textual widgets, so the same contribution renders in the TUI, the web
  client and over ACP.
- **Technology (owner, 2026-10-27): Textual, full-screen; the ACP adapter comes first.**
- **ACP adapter.** `noeta acp` speaks the Agent Client Protocol over stdio. Editors and existing
  ACP front ends (Zed, JetBrains, Neovim, Toad) can drive noeta without our own UI. It is built
  before the TUI so the event-to-UI mapping is proven on a real client first.
- **Own TUI.** The TUI is built on Textual, full-screen, in the `[tui]` extra.
  - Transcript: streaming markdown; tool calls as collapsible blocks with diffs; approval cards
    (yes / no / always); a side drawer for the subtask tree and background shells.
  - Input: multi-line; `@` fuzzy file pick; `/` commands; history.
  - Status bar: model, context used, cost, permission mode.
  - Modes: `noeta -p "…"` prints and exits (scripts, benchmark); `--json` streams events;
    `--resume`; `--server URL`.
  - Budgets: under 500 ms from command to prompt (lazy imports); stream deltas batched to
    about 30 frames per second; a virtualized transcript.
- The TypeScript TUI alternatives (pi-tui, Ink with a custom renderer, OpenTUI) remain possible
  later as separate clients over `noeta.server`; nothing in the core assumes the TUI's language.

### What `noeta-agent` contributes

- **Moved in:**
  - `bench/` (harbor adapter, run scripts, summarize) → `bench/` in this repo;
  - the mock LLM → test fixtures;
  - sandbox provisioning → `noeta.execenv` container backend.
- **Not moved for 1.0:** the FastAPI + React workbench, the preview gateway and the projects
  store. The web client can come back later on `noeta.server`.

### 0.6 data

- Not migrated. A host on 1.0 starts with an empty store; its 0.6 store stays readable with 0.6
  installed. Live 0.6 tasks finish on 0.6 before a host switches.

### ADRs

- 1.0 starts a new ADR set. 0.6 ADRs are marked `superseded` by the 1.0 ADR that replaces them.
- **Rules carried over** (re-stated in the 1.0 ADRs):
  - 016 provider-neutral;
  - 024 task as the only primitive;
  - 026 tool descriptions canonical;
  - 033 / 055 ExecEnv seam;
  - 036 anchored content / stable prefix;
  - 037 workspace write authorization;
  - the compaction rules 049, 050, 060, 061, 063, 064, 066;
  - skill listing 013, 020, 044, 047, 048, 062;
  - 053 MCP deferred schemas;
  - 035 / 054 memory;
  - 065 recovery rule;
  - 068 deny wins.
- **Dropped:** the lease, queue, attempt, idempotency-claim, plugin-bundle, engine-per-turn and
  workflow ADRs.

## Plan

Phase 0 — shape
- [x] Owner settles the open decisions in this spec
- [ ] Merge `fix/audit-closure-2026-10-26` and release it as the last 0.6 patch, so hosts have
      the fixes during the rewrite

Phase 1 — core on one machine
- [x] `noeta.types`, `noeta.loop` (pure) with recorded-response tests
- [x] `noeta.store` sqlite + conformance suite (conditional append, inbox dedupe, same-transaction
      `tasks` projection, claims)
- [x] `noeta.runner`: claim, loop, cancel watch, crash recovery
- [x] Port providers and the six file/shell tools with their tests
- [x] Prototype gate, races and crashes: racing runners never double-write; a kill at every append
      boundary recovers without re-running a finished tool; multi-process kill -9 soak
- [x] Prototype gate, behavior: the `make eval` scenarios that need only the six tools (grep, read,
      edit, parallel reads, two injection, honest failure, concise, proceed-when-clear) match
      0.6 on the same model; the rest are phase 2's gate

Phase 2 — extensions (default preset = 0.6 behavior)
- [x] permissions + declarative hooks (deny wins), ask_user
- [x] subtasks (Task tool, background children)
- [x] compaction + clearing old tool results
- [x] background_shell + Monitor, todo, web fetch/search
- [x] memory, skills, MCP
- [x] container ExecEnv; prompts moved unchanged and the full `make eval` green (live e2e for 1.0 not written yet)

Phase 3 — multi-machine
- [x] Postgres store, passing the same conformance suites
- [ ] Multi-process tests: N workers, kill -9 at random points, cancel from another process,
      child on one machine and parent on another
- [ ] CI Postgres leg runs in `make check`'s CI job (`make check` now runs the 1.0 package via `check-next`)

Phase 4 — surfaces
- [x] `noeta.client` (in-process + HTTP), `noeta.server` (`noeta.connect(url)` added with the
      review fixes; sync facade deferred to phase 5)
- [ ] `noeta acp` adapter, checked against Zed and Toad (built and tested against a fake editor;
      not yet run in a real editor)
- [x] `noeta.tui` + `noeta` command
- [x] Move `bench/` from noeta-agent (1-task smoke: git-leak-recovery solved)
- [ ] Terminal-Bench sample ×3 on 1.0 vs 0.6
- [ ] Archive noeta-agent

Phase 5 — cutover
- [ ] Distill the surviving Decisions into the 1.0 ADR set; mark replaced 0.6 ADRs `superseded`
- [ ] Docs rewrite, migration guide for hosts
- [ ] Sync client facade, if host-app needs one
- [ ] Migrate host-app on a branch and run it against its own scenarios
- [ ] Delete the 0.6 packages from `main`, release `noeta` 1.0.0 (owner tags)

## Acceptance criteria

- Conformance suites pass on sqlite and Postgres for store and scheduler.
- **Contention:** with two to eight workers on one task set, there are no duplicate tool runs and
  no lost inbox messages across 1,000 randomized kill -9 runs.
- **Recovery:** killing the runner at every append boundary in a recorded scenario resumes
  correctly and never re-runs a finished tool.
- `make eval` shows no regression versus 0.6 on the same model. The Terminal-Bench sample40 is
  at least 0.6's score, mean of 3 runs.
- Source size of `packages/noeta` excluding tests is ≤ 30k lines, and no file is over 800 lines.
- `noeta` (TUI) can resume a task created by a host process, and vice versa.
- host-app runs on 1.0 on a branch with its existing scenarios passing.

## Risks

- **Second-system bloat.** Mitigation: the line budget above, an extension instead of a kernel
  change for every feature request during the rewrite, and the phase 1 prototype gate before
  phase 2 starts.
- **Behavior drift in the default preset.** Mitigation: prompts are moved byte-for-byte; `make
  eval` and recorded-request snapshots are compared with 0.6.
- **Hosts stall on 0.6 during the rewrite.** Mitigation: 0.6 keeps getting security fixes;
  host-app migrates in phase 5 only.
- **TUI scope creep.** Mitigation: 1.0 TUI = chat, tool view, approvals, resume; nothing else.

## Progress log
2026-10-26 spec drafted from the owner's go-ahead on a 1.0 rewrite with multi-machine kept in scope and noeta-agent folded in.
2026-10-26 owner: one package `noeta`; web workbench not in 1.0; 0.6 stores not migrated; TUI asked for a fuller design and technology comparison.

2026-10-27 owner: Textual full-screen TUI, ACP adapter first; start. Spec active on `next`. ADRs are distilled at cutover, not written up front (AGENTS.md). Core is asyncio with a sync client facade.
2026-10-27 claims and change notifications folded into the store; no separate Scheduler interface (one deep module instead of two that share a table).
2026-10-27 phase-1 core green: 48 tests incl. a crash at each of 13 write points (before and after commit), 4 runtimes racing 12 tasks, multi-process kill -9 (60 tasks x 3 tool steps, ~10 kills/run, no step ran twice). The eval gate is split: six-tool scenarios in phase 1, the rest need phase-2 extensions.
2026-10-27 providers (517 offline tests, 6/6 live) and the six tools + LocalExecEnv (400 tests) ported by subagents and merged; MalformedToolArguments stays transient as in 0.6.
2026-10-27 Postgres store merged early (phase 3): 73/73 against postgres:16 incl. kill -9 across 5 seeds, no duplicate step; LISTEN/NOTIFY feeds on_change; ambiguous commit raises CommitUnknown (a Conflict).
2026-10-27 eval gate, 9 six-tool scenarios x3 on auto_model/alwaysday1_max: 1.0 0.982 vs 0.6 1.000 (one injection_in_file trial 0.5; within tolerance). 1.0 sent 373k input tokens vs 850k, but its tool set is smaller, so not yet a fair cost comparison.
2026-10-27 core-loop items from the 0.6 behavior inventory: failed interactive turns park (turn_failed), empty replies never recorded, cut replies run their complete calls, step limit ends with a report round, read-only concurrency cap 10, retries recorded, store outages no longer end the worker. Runner split into noeta/runner/ (runtime, task, model, calls, texts). Package source 9.9k lines, largest file 644.
2026-10-27 core hooks for phase 2 (park/resume, spawn, post, cancel cascade, view.complete/env, on_context_overflow); six extension groups dispatched to subagents (permissions, subtasks, compaction, ops, knowledge, mcp+container).
2026-10-27 phase-4 surfaces built early on `next`: `noeta -p` (live run fixed a bug in 10 s), `noeta acp` via the official agent-client-protocol SDK, Textual TUI (headless pilot tests + screenshots), `noeta serve` + RemoteClient; TUI `--server`. The runner now always sends max_tokens (output ceiling; unknown models 16,384).
2026-10-27 merged subtasks (+TaskView.post so a background child's end wakes an idle parent) and permissions/hooks/AskUserQuestion (no core edits). Core follow-ups: unattended tasks fail a turn that would wait for a human (needs_human); Task declares run_last so a reply's ordinary calls run first. default_agents = main + general-purpose/explore/plan; permission mode is per task and inherited.
2026-10-27 bench moved; harbor adapter runs `noeta -p --json --permission-mode bypassPermissions`; smoke git-leak-recovery reward 1.0 in 3.5 min.
2026-10-27 merged knowledge (instructions, skills, memory), ops (todo, background shells, web), compaction, mcp + container ExecEnv. Core: state.task_id set on load; ToolContext.post may target another task; unadvertised tools; Rewrite before Ask; summary is a plain user turn; compaction resets model attempts. default_agents wires all of them in 0.6's order. Package: 23.3k lines, largest file 644.
2026-10-27 full make eval, 14 scenarios x3, 1.0 vs the 0.6 baseline (same model): overall 0.934 vs 0.952, no regression; delegate_parallel_review times out at 240 s on both (scored by the first reply, by design). Full suite against postgres:16: 1241 passed. `make check` runs the 1.0 package (check-next); naming lint: MCP's header is a connection id, ACP's protocol names exempted.
2026-10-27 released 0.6.42 (merged PR #75 with owner's go-ahead; both wheels verified from PyPI).
2026-10-27 fresh-context reviews: core/front ends (4 P1, 8 P2) and extensions (4 P1, 5 P2), all with repros; two fix branches in flight. Spec drift fixed here: no blob table (payloads inline), deltas are a separate channel, `tasks()` filters by parent/status, sync facade moved to phase 5.
2026-10-27 review fixes merged: core (store roots filter and error mapping, unique call ids, answers re-gated, parked-call Waiting.detail, turn close rules, release on shutdown, front ends serve only their own trees, ACP turn gating by inbox seq, SSE/remote reconnect, Delta call_seq/attempt) and extensions (hardened env capture through the ExecEnv, `Extension.resident` + `TaskView.residents()` so residents return right after a summary, background job ownership, `.noeta/` writes ask, todo reminder after compaction, 0.6 sub-agent sets, inherited always-rules, private log dir). 1285 passed (sqlite), 1309 (postgres:16); make check green.
2026-10-27 host-app migration assessed (read-only): overall L, about 3-5 weeks in host-app plus about 2 weeks of Noeta additions (sync facade, per-turn config overlay on Say, error class on turn_failed, store prune, tasks(updated_after) + store-wide observer, opt-in request capture, provider_for(task), rewind or drop take_back, public estimate_tokens). One-branch cutover (0.6 and 1.0 cannot be installed together). Owner to decide scope. Fixed the task_answer mapping above.
2026-10-27 re-review fixes merged (typed answers to AskUserQuestion from TUI/ACP, descendants' approvals/questions surface in -p/TUI/ACP, repo git filters never run, foreign-agent tasks open read-only, held messages reopen a stopped turn, identical rewrites ignored). After these: make eval 0.982 vs 0.6 baseline 0.952; 1304 passed; make check green.
2026-10-27 owner: drop 0.6 now, no host-app gate. Repository restructure continues in `2026-10-27-noeta-1-0-layout.md`; 0.6 deleted from the branch there.
