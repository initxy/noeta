# Many machines

This page explains how noeta runs on many machines at once: one Postgres store, any number of
`noeta worker` processes and `noeta serve` in front of them, and why a task can continue on any
of them after any step. It also says what the exec env must look like from every worker, what
stays local to one process, and what happens when a worker dies. The commands to set this up
are in [Run it as a server](../guides/server.md).

Nothing in the kernel is special to many machines. A task is a [log](./event-log.md) with
[one writer](./recovery.md): whoever holds the task's claim folds its log and appends to it, and
every append is fenced by the claim's epoch. One process on sqlite and fifty workers on Postgres
run the same code; the store is what changes.

<Diagram name="multi-deploy" caption="Front ends talk to noeta serve; any number of workers share one Postgres store" />

## The pieces

| Piece | What it does | Code |
|---|---|---|
| Postgres store | The task rows, event logs, inboxes and snapshots, in one schema (`noeta` by default); the only shared state | `noeta/backends/stores/postgres.py` (with `postgres_conn.py`, `postgres_feed.py`, `postgres_snapshots.py`) |
| `noeta worker` | Claims ready tasks and runs them, `--concurrency` at a time (default 8); runs the channels too | `noeta/core/runner/runtime.py` |
| `noeta serve` | The client API over HTTP and WebSocket, with a bearer token; runs a worker too unless `--no-worker` | `noeta/host/http.py` |
| Front ends | The terminal UI (`noeta --server URL`), editors (`noeta acp --server URL`), the web app, your code (`RemoteClient`) | `noeta/host/remote.py` |
| Exec env | Where tools read files and run commands: a shared filesystem, or a remote container | `noeta/backends/execenvs/` |

```sh
export NOETA_STORE=postgres://noeta:…@db.internal/noeta
noeta worker --concurrency 16            # on every worker machine
noeta serve --no-worker --host 0.0.0.0   # the API, on one or more machines
```

A front end attached with `--server` needs no model or key of its own: its tasks run on the
server's model, in the server's directory, unless `--model` or `--cwd PATH` (a directory on the
server) say otherwise.

## Why any worker can run any task

- **Claims, not assignments.** A worker takes a claimable task with `FOR UPDATE SKIP LOCKED`, so
  two workers never take the same one and nobody hands out work. The claim index holds only rows a
  claim can take, so idle waiting tasks cost the scan nothing.
- **Fenced writes.** A claim bumps the task's epoch. Every append first locks the task row, then
  checks the status, the epoch and the expected last seq, so a worker that froze and woke up after
  its lease ran out can never write again. Leases only decide when a silent owner may be
  replaced. They use the database's clock, so machines whose clocks drift still agree on who holds
  a lease.
- **The log carries everything.** The conversation, every plugin's state, the prompt overrides,
  the plugins the task runs with and the provider its model goes to are all in the log. A worker
  that claims the task folds them (from a snapshot plus the events after it) and sends the same
  request bytes the previous worker would have sent ([context](./context.md#why-the-bytes-stay-the-same)).
- **Waiting costs nothing.** A task that waits for a person, a child or a timer gives its claim
  up; whichever worker is free when the answer arrives takes it ([waiting](./waiting.md)).

Each write is one transaction. A deadlock or serialization failure aborts it whole and it is run
again with its checks; a connection that drops before `COMMIT` is sent is retried once on a fresh
one; a drop while `COMMIT` is in flight raises `CommitUnknown` and is never re-run blindly (a claim
whose commit was unseen is found again by asking the store which tasks it says this worker runs).

## Live output across machines

<Diagram name="multi-live-output" caption="Live text is best effort; recorded events are announced with their commit" />

Two kinds of news leave a worker:

- **Recorded events** are announced with `pg_notify` on `<schema>_changes` inside the same
  transaction that writes them, so a notification exists exactly when the change committed. Every
  process keeps a `LISTEN` connection (reopened when it drops) that feeds `on_change`; a front end
  watching a task then reads the new events from the store.
- **Live model text** (the deltas while a reply streams) goes on a second channel,
  `<schema>_deltas`, outside any task transaction (`publish_deltas`, `noeta/host/local.py`). It is
  best effort and never stored: each notification stays under Postgres' 8,000-byte payload limit
  (payloads up to 7,000 bytes, a long fragment cut into several). `noeta serve` on any machine
  hears it and streams it on `/watch` (server-sent events), so a front end attached to one server sees the text of a
  task another machine runs, as it is written.

Notifications sent while a `LISTEN` connection reconnects are lost. To cover that gap a runner
looks at a running (or kept-waiting) task's inbox every third of its lease, so a message posted
during the gap is taken late, never missed. On sqlite (one machine) live text stays in the process
that runs the task; the recorded events still reach everyone.

## The workspace must look the same from every worker

Tools read and write files and run commands in an exec env on the task's `cwd`. Since a task may
continue on another machine after any step, that directory must look the same from every worker.
Two deployments work:

1. **A shared filesystem at the same path on every worker** (NFS, EFS, CephFS and the like). The
   default local exec env just works.
2. **A remote exec env**: commands run in a container on a host every worker reaches
   (`noeta.backends.execenvs.container`, or your own exec env in the `noeta.execenvs` group), chosen
   per task by the host's `Runtime(env_for=…)`.

A task's own files live where its tools do, in its exec env, so they follow the task: long tool
outputs saved for the model (the spill directory, `~/.noeta/tool-results/<task id>/`), plan files
(`~/.noeta/plans/`) and auto memory (`~/.noeta/projects/<project>/memory/`,
`~/.noeta/agent-memory/`). The model reads and edits them with its file tools, as in Claude Code,
so they are files, not store rows; their paths are recorded in the log the first time they are
used. With a remote exec env they live in that env, and noeta's home path must be the same on every
worker. With the local exec env on a shared filesystem, those directories must be on the shared
mount too, at the same path everywhere (`NOETA_HOME` on the mount, or `tool-results`, `plans`,
`projects` and `agent-memory` linked to it); otherwise a task that moves finds its saved outputs,
plan and memories missing.

## What stays in one process

Some things cannot leave the process that has them. Each is handled so that a task moving or a
worker dying is safe:

| Local to one process | What happens when the task moves or the worker dies |
|---|---|
| A background `Bash` job and its output | The task keeps its claim on that worker while the job runs (`ToolContext.keep`, [ADR-029](https://github.com/initxy/noeta/blob/main/docs/adr/029-a-task-keeps-its-own-claim-while-it-owns-process-bound-work.md)): the row says waiting, the worker renews the lease and takes the task's inbox itself. If the worker dies, the lease runs out and the next claimer tells the model the job is lost. |
| A parked MCP call, MCP connections | Connections are pooled per process and opened again where needed; a call that must wait on its connection keeps the claim the same way. |
| Live model text | Best effort, as above; the recorded reply is what counts. |
| Images people paste | Kept in `~/.noeta/image-cache` (local exec env) or a per-user temp directory; a worker that takes the task over writes them again from the conversation and names the new place. |
| A summary being written in the background | Lost with its worker; a later step starts another ([context](./context.md#writing-the-summary-ahead)). |
| Model listings, the device id | Per machine (`~/.noeta/cache/models`, `~/.noeta/device_id`). A model's provider and listed limits are recorded per task, so listings may differ ([models](./models.md#routing-with-several-providers)). |
| `~/.noeta` settings, agents, plugins | Each process reads its own. Ship the same copy to every machine; a worker missing a plugin a task was started with never runs it without it ([plugin pin](./plugins.md#the-plugin-pin)). |
| Channels | Run in every `noeta worker` / `noeta serve` that enables them; inbound posts are idempotent, so several copies are safe, and replies leave from inside the task. |

## When a worker dies

A worker that dies leaves nothing behind that another cannot pick up:

- Its tasks stay `running` until their leases run out (`noeta worker --lease`, default 30 seconds),
  then any worker claims them and continues from the log.
- A model call cut off by the crash is made again from the log (the same request); a tool call that
  was logged as started is answered "outcome unknown" unless its tool can look the outcome up or is
  safe to run again ([recovery](./recovery.md)).
- A worker that only froze and comes back finds its epoch stale: its next append is refused and it
  drops the task.
- A worker stopped on purpose (`SIGTERM`) hands its tasks back at once instead of waiting for the
  leases; tasks that kept their claims for background jobs stay waiting and are told at their next
  message that the job is gone.

`make cluster` runs worker processes on Postgres under random kills to check exactly this
(`tests/cluster`).

## Upgrades and several parties

- **Upgrades.** Stop every worker and `noeta serve` before upgrading; workers of different versions
  must not share a store. The first process of the new version moves the store's schema forward in
  place; a process older than the store's schema refuses to open it (`StoreUnavailable`). For a
  plugin change, a rolling restart is enough: stop one worker at a time, install, start it again,
  within a few leases.
- **Queues.** `noeta worker --queue NAME` serves only that queue; a child task can be sent to
  another queue (`view.spawn(queue=...)`), for example to run on machines with a GPU.
- **One party per store.** A store and a server serve one party: whoever holds the token drives
  every task, and tools run as the worker's OS user
  ([ADR-028](https://github.com/initxy/noeta/blob/main/docs/adr/028-no-tenants-in-noeta.md)). To
  serve parties that must not see each other, give each its own server and store schema, or its own
  machines.
