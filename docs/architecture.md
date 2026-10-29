# Architecture

noeta is a small kernel that runs tasks from append-only logs, and plugins that give the model
everything it can do. This page walks through how the pieces fit; the exact definitions live in
[CONTEXT.md](../CONTEXT.md) (linked per section) and the reasons in [docs/adr/](adr/index.md).

```
  front ends ──► client API ──► store ◄── workers (Runtime) ──► model provider
  (TUI, -p, ACP,   (create, send,   (task logs,   claim a task, fold its log,
   HTTP/WS, SDK)    answer, cancel,  inboxes,     call the model, run tools
                    read, watch)     claims)      through plugins, append
```

## The kernel

**A task is a log.** Each task is a sequence of ten event types (`noeta/core/events.py`): it was
created, took input, called the model, got a reply, started and finished a tool call, was
compacted, waits, ended, or a plugin recorded something. Nothing else holds task state: the loop
state is a fold of the log (`noeta/core/loop/state.py`), and the next model request is a pure
function of that state (`noeta/core/loop/request.py`). That is why a task can stop anywhere and
continue on another machine, and why a resumed task sends the same bytes.
See [Task](../CONTEXT.md#task) and [Event log](../CONTEXT.md#event-log).

**The inbox is the only way in.** Messages from the person, answers to questions, cancels,
children ending, timers and plugin signals are posted to the task's inbox, each idempotent on a
dedupe key. Only the task's runner reads it, and taking input is itself a logged event.
See [Inbox](../CONTEXT.md#inbox).

**One writer.** A worker claims a task from the store before running it (`claim` / `renew` /
`release`; Postgres uses `SKIP LOCKED`). Every append names the sequence number it expects and
the claim's epoch, so a worker that froze and lost its claim can never write again.
See [One writer](../CONTEXT.md#one-writer).

**Recovery never repeats a side effect blindly.** A tool call is logged as started before it
runs. If the worker dies mid-call, the next worker answers it "outcome unknown" — unless the
tool can look the outcome up (`Tool.recover`, keyed by `ToolContext.idempotency_key`, the same
on every machine) or declares itself safe to run again (`retry_safe`).

**Waiting costs nothing.** A call that needs a person or children *parks*: the task logs
`Waiting`, gives up its claim and holds no worker until an answer arrives in its inbox.
Timers (`view.set_timer`) are kernel state too, so a scheduled wake-up survives every worker.
See [Park](../CONTEXT.md#park).

**Holds** cover what lives in one process: a background shell job holds its task tree on its
worker (`Store.hold`, one holding worker per tree), so no other worker claims those tasks while
the job runs. Each hold has
an epoch; when a worker goes silent any worker reaps its holds and posts `hold_lost` to the
tree, so the model is told the job is gone instead of waiting forever.

**Snapshots** keep claims cheap: as a task's log grows the runner stores a versioned fold
snapshot (`noeta/core/snapshots.py`; at most every `Runtime.snapshot_every` events, and once
the events since the last one hold a tenth of its bytes), and a claim folds the snapshot plus
the events after it. Ending a task deletes its snapshot. A snapshot whose version does not match the code is ignored and the log is
folded from the start, so a snapshot can be missing but never wrong.

## One step of a task

1. A worker claims a ready task, folds its log (snapshot + tail) and takes its inbox.
2. Plugins run `before_model` and contribute context parts and reminders; the runner builds the
   request from the state and records what it needs to rebuild it identically.
3. The model's reply is appended. Each tool call goes through the plugins' gates
   (`before_tool`: deny wins over rewrite, ask and allow), is logged as started, runs in the
   task's exec env, and its result is appended (after `after_tool`).
4. The loop continues until the model stops, a call parks, or the task is cancelled; the worker
   then releases the claim. The code is `noeta/core/runner/` (`Runtime` claims and serves,
   `TaskRun` runs one claim).

## Plugins

Every capability — the file tools, the shell, permissions, hooks, skills, sub-agents, MCP,
memory, compaction, the environment and instructions — is a plugin implementing parts of one
interface, `Extension` (`noeta/core/extension.py`). Plugins never add event types; they record
`ExtensionRecorded` entries and fold them in their own `reduce`, so their state is durable and
machine-independent like the kernel's. Plugins are found by name through entry points; the
built-ins register in `pyproject.toml` exactly as a third-party package does, and a plugin can
also be a single file. A task records which plugins it runs with, and a worker missing one fails
the task rather than running it differently.
See [Extension](../CONTEXT.md#extension), [Registry](../CONTEXT.md#registry),
[Plugin pin](../CONTEXT.md#plugin-pin), and [docs/extending.md](extending.md) for writing one.

What the model sees follows Claude Code: tool names and schemas, the order and roles of prompt
parts, reminders, compaction. The texts are noeta's own words; `make parity` replays Claude
Code's recorded requests against noeta and reports every difference
(`scripts/parity/README.md`).

## Layers

The package is layered and imports only downward; `lint-imports` and `scripts/lint-layout.py`
enforce it (see [Layers](../CONTEXT.md#layers) and [Where code goes](../CONTEXT.md#where-code-goes)):

| Package | Holds |
|---|---|
| `noeta/core` | the kernel: events, store interface, runner, client, extension and tool interfaces |
| `noeta/toolkit` | code several plugins share (paths, shell parsing, output limits, sandbox) |
| `noeta/plugins/<name>` | capabilities; a plugin imports only `core` and `toolkit` |
| `noeta/backends` | model providers, exec envs (local, sandbox, container), stores (sqlite, Postgres) |
| `noeta/host` | running noeta (`LocalApp`) and the ways in: HTTP, ACP, stream-json |
| `noeta/agents` | the agent-file format and the built-in agents |
| `noeta/cli` | the `noeta` command, its terminal UI and `~/.noeta` configuration |

Everything but `noeta/cli` is the SDK.

## Ways in

There is one API, the client (`noeta/core/client.py`): create, send, answer, cancel, read,
watch. Every front end is a translation of it ([Ways in](../CONTEXT.md#ways-in)):

- **In process**: `noeta.Client` on a store, or `noeta.LocalApp` (store, worker and client).
- **HTTP / WebSocket**: `noeta serve` (`noeta/host/http.py`) puts the client on the wire;
  `noeta.RemoteClient` and `noeta --server URL` use it. A bearer token is required unless
  `--no-token` is given, which serves a loopback address only.
- **ACP**: `noeta acp` (`noeta/host/acp.py`) for editors, locally or against a server.
- **Claude Code's headless protocol**: `noeta -p` and `--input-format stream-json`
  (`noeta/host/stream_json.py`), also served as `/stream-json`, so a program written for Claude
  Code's Agent SDK can drive noeta.
- **Channels**: long-running listeners for outside conversations (a chat app, a webhook), run by
  `noeta worker` / `noeta serve` (`noeta/core/channel.py`).

## Many machines and tenants

Any number of `noeta worker` processes share one Postgres store; any of them can continue any
task, because everything a task needs is in its log. Live model output crosses machines through
Postgres notifications; recorded events reach everyone through the store. A task's commands run
in an exec env that must look the same from every worker: a shared filesystem or a remote
container. Tasks belong to a tenant; a server token selects one, and every read, write and
inter-task message stays inside it, with per-tenant workspace, running limit, monthly budget and
provider. See [Tenant](../CONTEXT.md#tenant) and [docs/server.md](server.md).
