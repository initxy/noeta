# How it works

This is the one page to read if you want to know what Noeta does under the hood.
Three ideas carry the whole design; everything else — crash recovery, audit,
replay, suspend and resume, swappable models — follows from them.

<NtArchitecture />

## 1. State is a fold over an event log

Every Task owns one append-only stream of events: the goal, each context the
model was shown, each model reply, each tool call and result, each suspend and
wake. There is no task table. When anything needs the current state, it folds the
stream from the start (or from the latest snapshot) and gets it.

- **Crash recovery is free.** A dead worker leaves nothing half-saved; the next
  worker folds the log and carries on.
- **Replay is exact.** The same log folds to byte-identical state on any machine.
- **Audit is built in.** The log *is* the history. Nothing is edited in place.

→ [The event log](event-log.md)

## 2. A Task is the only unit of work, and waiting is first class

A chat that runs for weeks, a nightly job, a delegated subagent: each is a Task
with its own log. There is no session, no workflow instance. A Task is `pending`,
`running`, `suspended` or `terminal` — and every kind of waiting (a human answer,
a timer, a subtask, an external event) is the same `suspended` status plus a
typed wake condition.

- A suspended Task holds no thread, connection or memory. It can wait for months.
- A matching wake is stored durably and resumes the Task exactly once, even if
  a worker dies in between.
- A worker holds a lease on the Task while it runs, and every write presents the
  lease, so a Task never has two writers.

→ [Tasks and waking](tasks-and-waking.md)

## 3. The kernel has no capabilities; everything is a plugin

File tools, web tools, memory, MCP, sandboxes, storage backends, guards and every
model adapter are built-in plugins under `noeta.builtins`. The kernel reaches
them only through the plugin loader's dynamic `ref` resolution; an import linter
fails the build on any static import. Your plugins go through exactly the same
loader, validation and merge path as Noeta's own.

Because the kernel cannot import a vendor adapter, it cannot grow a vendor
assumption either — see [Connect a model](../guides/models.md) for how
providers plug in.

→ [The plugin system](plugin-system.md)

## Two packages

| Package | What it is | Depends on |
| --- | --- | --- |
| `noeta-runtime` | The kernel: Engine, fold, snapshots, Worker, Dispatcher, lease, context composer. No capability code, no HTTP client. | stdlib only |
| `noeta-sdk` | What you install and import (`noeta.sdk`): `query`, `Client`, `Options`, `@tool`, presets, and `noeta.builtins`. | `noeta-runtime`, `httpx`, `psycopg` |

Both share the `noeta.` namespace. You install `noeta-sdk`; `noeta-runtime`
comes with it.

## One turn, end to end

1. Your code hands a goal to `query()` or a `Client`.
2. A worker leases the Task and folds its log into state.
3. The Engine loops *compose → decide → dispatch*: build what the model sees,
   let the policy pick the next action, run the tools, record every result as an
   event. Guards may veto an action before it runs.
4. The loop stops when the Task finishes or has to wait. The worker releases the
   lease. A waiting Task costs nothing until its wake arrives.

The deployment shape does not change the Engine: one process with SQLite, a
worker pool in a service, or several hosts sharing Postgres all fold the same
logs. See [Deploy](../guides/deploy.md).

## Read next

- [The event log](event-log.md) — state = fold(log), snapshots, and crash recovery.
- [Tasks and waking](tasks-and-waking.md) — the four statuses, subtasks, and exactly-once wake.
- [The engine](engine.md) — one step, the decision vocabulary, and guards versus observers.
- [Context and caching](context.md) — how each prompt is assembled to keep the provider cache warm.
- [The plugin system](plugin-system.md) — surfaces, the loader, and what stays locked.
