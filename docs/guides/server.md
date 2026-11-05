# Run it as a server

This page shows how to run noeta for other people and programs: `noeta serve` for the
HTTP / WebSocket API, `noeta worker` processes that run the tasks, one Postgres store they
share, the bearer token, the web app, and channels that let a chat app in. How the pieces stay
correct across machines is on [Many machines](../how-it-works/many-machines.md).

Every task is a log in the store. A worker that dies leaves nothing behind: another one claims
its tasks where their logs stop.

<Diagram name="guide2-server-deploy" />

## Install

The server and Postgres parts are extras:

```sh
uv tool install --with 'noeta-tui @ git+https://github.com/initxy/noeta#subdirectory=tui' \
  'noeta[server,postgres] @ git+https://github.com/initxy/noeta'
```

`server` adds `noeta serve` (Starlette, uvicorn, websockets); `postgres` adds the Postgres store
(psycopg). A command that needs a missing extra stops and prints the line that installs it
(`noeta/cli/main.py` `missing_extra`).

## One machine

```sh
export NOETA_MODEL=claude-sonnet-5-5 NOETA_API_KEY=sk-ant-...
noeta serve                       # http://127.0.0.1:8642, a worker in the same process
```

With no `--store` / `NOETA_STORE` the store is sqlite at `~/.noeta/tasks.db`. `noeta serve`
without `--no-worker` runs a worker in its own process (queue `default`, 8 tasks at once), so
this is a complete server. On the first start it makes a bearer token and prints it once
([below](#the-token)).

## Many workers

```sh
export NOETA_STORE=postgres://noeta:…@db.internal/noeta
noeta worker --concurrency 16            # on every worker machine, as many as you like
noeta serve --no-worker --host 0.0.0.0 --allow-host api.internal   # the API, on one or more machines
```

- **Claims, not assignments.** A worker takes a claimable task with `FOR UPDATE SKIP LOCKED`; a
  lease says how long a silent worker keeps it (`noeta worker --lease`, default 30 s). Writes
  are fenced by the claim's epoch, so a frozen worker that wakes up later can never write.
- **Live output crosses machines.** A worker publishes the model's streamed text through
  Postgres (the `noeta_deltas` notification channel; best effort, never stored). `noeta serve`
  hears it and streams it on `/tasks/{id}/watch`, so a front end attached to any server sees the
  text as it is written. On sqlite (one machine) live output stays in the process that runs the
  task; the recorded events still reach everyone.
- **Same settings everywhere.** Each process reads its own `~/.noeta` (`NOETA_HOME`): agents,
  plugins, `config.toml`, settings files, MCP servers. Ship the same copy to every machine. A
  worker missing a plugin a task was started with never runs that task without it: it lets the
  lease run out so a worker that has the plugin picks the task up, and after three such claims
  in a row with nothing new in the log the task fails with `plugin_missing`
  ([Write a plugin](./plugins.md)).
- **Queues.** A task is created on a queue (`POST /tasks` `queue`, default `default`); a worker
  takes only the queues it names (`noeta worker --queue NAME`, repeatable). Use it to keep
  some tasks on machines that have what they need.
- **Bypass mode.** Like `noeta serve`, a worker refuses `bypassPermissions` unless started with
  `--allow-bypass-permissions`.

### `noeta worker` flags

| Flag | Default | Meaning |
|---|---|---|
| `--queue NAME` | `default` | a queue to take tasks from (repeatable) |
| `--concurrency N` | 8 | tasks this process runs at once |
| `--lease SECONDS` | 30 | how long a claim lasts without renewal; a dead worker's tasks wait about this long before another takes them |
| `--snapshot-every EVENTS` | 100 | write a task's fold snapshot at most every this many events (`0`: never) |
| `--allow-bypass-permissions` | off | let tasks run in `bypassPermissions` |

### `noeta serve` flags

| Flag | Default | Meaning |
|---|---|---|
| `--host` | `127.0.0.1` | address to bind |
| `--port` | 8642 | port |
| `--no-worker` | off | API only; workers run elsewhere |
| `--no-token` | off | serve without a bearer token; refused on any address but loopback |
| `--allow-host NAME` | — | a `Host` header name the server answers (repeatable) |
| `--allow-origin ORIGIN` | — | a web origin (`https://host:port`) whose pages may call the API (repeatable) |
| `--allow-root PATH` | — | a directory besides the server's own that tasks may run in (repeatable) |
| `--allow-bypass-permissions` | off | let requests start or switch tasks into `bypassPermissions` |
| `--web DIR` | — | also serve the built web app from `DIR` at the other paths |

Both also take the common flags (`--model`, `--store`, `--provider`, `--base-url`, `--cwd`, …);
see [the noeta command](../reference/cli.md). `--effort` and `--permission-mode` set the
starting config of the tasks clients create through `noeta serve` (a request's own value wins);
on `noeta worker` they reach only the tasks its channels start. Either refuses
`--permission-mode bypassPermissions` without `--allow-bypass-permissions`. The endpoints are
listed in [HTTP / WebSocket API](../reference/http-api.md).

## Postgres

`NOETA_STORE=postgres://…` (or `postgresql://…`, or `--store`) picks the Postgres store
(`noeta/backends/stores/postgres.py`). The first process creates the schema `noeta` and its
tables; nothing else needs setting up. Leases and claim times use the database clock, so
machines with skewed clocks agree on who holds a task. Changes are announced with `pg_notify`
on `noeta_changes` inside the same transaction, so a waiting task wakes as soon as something
lands in its inbox.

**Upgrades.** Stop every worker (and `noeta serve`) before upgrading; workers of different
versions must not share a store. The first process of the new version moves the store's schema
forward in place. A process older than the store's schema refuses to open it
(`StoreUnavailable: … stop every worker and upgrade`); a 0.6 binary does not check, so stop
every old worker first. A 0.6 store (schema version 1) is moved to version 5 in place: its
`holds` table goes, and a background job it named, gone with its worker, is told lost at its
task's next turn.

**Rolling restarts.** `SIGTERM` or `SIGINT` stops a worker: its running tasks go back to the
store and continue on another worker from their logs. A task that was waiting on a background
job of that worker stays waiting and is told the job is lost with its next message. To roll out a plugin, stop one worker at
a time, install, start it again; finish within a few leases, since a task pinned to the new
plugin that keeps landing on workers without it fails after three claims.

## The token

`noeta serve` always requires a bearer token ([ADR-009](https://github.com/initxy/noeta/blob/main/docs/adr/009-serve-requires-a-token.md)):
the HTTP API creates tasks that run commands with the server's rights, and even a loopback
port is reachable from other local processes and from web pages in a browser.

- The token is `NOETA_SERVER_TOKEN`, else one made on the first start and printed that once.
- `~/.noeta/server-token` (owner-only) keeps only its SHA-256
  ([ADR-013](https://github.com/initxy/noeta/blob/main/docs/adr/013-server-token-kept-as-a-digest.md)),
  so a client (`noeta --server`, `noeta acp --server`) needs the token in its own
  `NOETA_SERVER_TOKEN`. Delete the file and restart for a new one. A file an older noeta left
  holding the token itself is rewritten to its digest on the next start, which prints the
  token one last time.
- `--no-token` works only on a loopback address.
- On a loopback bind only the loopback `Host` names are answered (DNS rebinding). On any other
  bind `Host` is checked only against the names given with `--allow-host`, so name them;
  without any, `noeta serve` warns on stderr at startup that every `Host` is answered.
- A request from a web page of another origin is refused unless that origin is given with
  `--allow-origin`.
- Host code embedding the API uses `create_app(client, token=...)` (or `token_sha256=`); it
  refuses to build an app without one unless told `auth=False`.

## What a request may set

A request may set only the task config a client may (`noeta/host/_http_guard.py`
`REQUEST_CONFIG`): `model`, `effort`, `max_turns`, `cwd`, `permission_mode`, `output_schema`,
`timezone`, `interactive`, `permission_prompt_tool`, `ultracode`, `max_budget_usd`,
`continued_as`, `bash_mode`. Any other key is refused (403); a `max_budget_usd` that is not a number above 0
is `400`, at creation and in a config signal. It may post only the signals front ends send, and create tasks only in the
server's directory and its `--allow-root` paths (a relative `cwd` is taken from the server's).

- `timezone` is the person's IANA zone (`Europe/Berlin`), which noeta's own front ends send:
  without it the model is given the UTC date, marked as UTC's, never the server's local one.
  Cron jobs (`CronCreate`) and `ScheduleWakeup`'s shown time follow it too; without it they use
  the zone of the machine that runs the task.
- `output_schema` (a JSON Schema whose root is `"type": "object"`) makes the model answer
  through a `StructuredOutput` tool, as Claude Code's `--json-schema`; the value is the task's
  `structured_output` `answer` record (`noeta.toolkit.structured_output.structured_answer`).
- `interactive: false` marks a task nobody follows live (no title, no `AskUserQuestion` unless
  `permission_prompt_tool` is set), as the headless protocol does. These keys stay until a
  client sets them again; the terminal UI and ACP take them away when they reopen a task.
- `bash_mode: true` says the client runs the person's `! <command>` lines, as the terminal UI
  does in process: the main prompt then tells the model to suggest one for an interactive
  command. Set it only at creation; sub-agents do not get it (a fork keeps it with its parent's
  prompt).
- A config signal may remove, never set, `client_hooks`: the record of a headless client's
  hook callbacks (`noeta.toolkit.hook_callbacks`).
- `ultracode: true` is Claude Code's ultracode switch: the model is told to orchestrate with
  `Workflow` on every substantial task, and told again when it is taken away
  (`noeta.plugins.workflow.opt_in`).
- `continued_as` says how a task created with `history_from` continues its source, for
  SessionStart hooks: `fork` (the default), `resume`, or `rewind` (none fires).

A model's provider route is the server's to record, never a client's.

## Front ends

- **Terminal UI**: `noeta --server http://api.internal:8642`.
- **Editors** (ACP): `noeta acp --server URL`.
- **Web app**: build it once (`cd web && npm install && npm run build`) and serve it with
  `noeta serve --web web/dist`. The page asks for the token once, or takes it from the
  address's `#token=…` fragment (never sent to the server, kept in the browser). Its files are
  served without a token, under a content security policy that allows only the server's own
  scripts, styles and requests, and no framing.
- **Your own program**: `noeta.RemoteClient` or plain HTTP; `noeta.connect(url)` gives a client
  for a store URL or a server URL.

Front ends attached with `--server` need the token in `NOETA_SERVER_TOKEN` and no model of
their own: a new task runs on the server's model, in the server's directory, unless `--model`
names one or `--cwd PATH` names a directory on the server. `noeta -p` always runs its task in
its own process and refuses `--server`. More on each in
[Web, desktop and editors](./front-ends.md).

## Where commands run

Tools read and write files and run commands in an exec env on the task's `cwd`. A task may
continue on another machine after any step, so that directory must look the same from every
worker: either a shared filesystem mounted at the same path on every worker (the default local
exec env just works), or a remote exec env such as a container, chosen per task by host code
(`Runtime(env_for=…)`). The `noeta` command itself always uses the local exec env, with the
sandbox when the settings turn it on.

A task's own files follow it the same way: saved long tool outputs
(`~/.noeta/tool-results/<task id>/`), plan files (`~/.noeta/plans/`) and auto memory
(`~/.noeta/projects/<project>/memory/`, `~/.noeta/agent-memory/`). With the local exec env on a
shared filesystem, put `NOETA_HOME` on the shared mount too (or link those four directories to
it), at the same path on every worker. Still local to one worker: a background `Bash` job and
its log (the task keeps its claim on that worker while the job runs, and is told the job is
lost if the worker dies), and pasted images (written again from the conversation by a worker
that takes the task over).

The details, and how to pick between local, sandbox and container, are on
[Where commands run](./sandbox.md).

## Channels: let a chat app in

A channel is a long-running listener for an outside conversation, such as a chat thread or a
webhook (`noeta/core/channel.py`). `noeta worker` and `noeta serve` run every channel
`config.toml` enables, each in its own background task; one that fails is logged and started
again after 5 seconds (`noeta/host/channels.py`).

```toml
# ~/.noeta/config.toml
[channels.chat_bridge]
secret = "{env:CHAT_BRIDGE_SECRET}"
allow_senders = ["U123"]

[plugins.chat_reply]
url = "https://chat.example/hooks/{env:CHAT_HOOK_TOKEN}"
```

That is the [`examples/chat-bridge`](https://github.com/initxy/noeta/tree/main/examples/chat-bridge)
channel (copy `chat_bridge.py` into `~/.noeta/plugins/`): a chat app posts each message to
`POST /chat` on port 8788 with the shared secret, every thread becomes one task, and the
`chat_reply` plugin inside the task posts answers back.
[`examples/chat-approvals`](https://github.com/initxy/noeta/tree/main/examples/chat-approvals)
adds Allow / Deny buttons for the approvals a task waits on (port 8789).

How channels stay correct on several replicas:

- `deliver()` creates a thread's task once however many replicas race, and posts each message
  once by its delivery id. A thread outlives its task: once the task ended, the next message
  starts a continuation `<id>.2`, `<id>.3`, … from the previous conversation.
- An outside sender's words always arrive framed as `<channel source="…" sender="…">`, so the
  model never takes them for the task owner's.
- Replies go out from inside the task (a plugin's `on_event`), where the task's one writer
  makes it a single sender. Make the post idempotent on the event's seq, as chat-bridge does
  with its `Idempotency-Key` header.

Writing your own is in [Write a plugin](./plugins.md).

## Cleaning up

- **Saved tool outputs.** `noeta serve` with its own worker, `noeta worker` and the local front
  ends sweep the spill directory (`~/.noeta/tool-results`) once a day (`noeta/host/spill_sweep.py`,
  started by `LocalApp.start`). A `noeta serve --no-worker` runs no sweep; its tasks' outputs
  are written by the workers, which sweep their own machine's directory. A task's directory is removed once its task (and its tree) has
  ended and nothing was written there for 30 days, Claude Code's retention for tool results.
  A waiting task's outputs stay however long it waits. The sweep covers only this machine's
  directory; a host whose tasks run in a remote exec env sweeps that env's directory itself.
- **Snapshots.** Ending a task deletes its fold snapshot.
- **Task logs** are never deleted: a task is the record of what happened. Front ends can
  archive a task (the `archive` signal), which hides it from the list.

## Several parties

A store and a server serve one party: whoever holds the token drives every task, and tools run
as the worker's OS user ([ADR-028](https://github.com/initxy/noeta/blob/main/docs/adr/028-no-tenants-in-noeta.md)).
To serve parties that must not see each other, give each its own server and its own database
(or its own machines). The `noeta` command always uses the Postgres schema `noeta`; host code
can pass `PostgresStore(dsn, schema=…)`. Or build the separation in host code on the extension
points: queues and worker pools per party (`noeta worker --queue NAME`), an exec env per task
(`Runtime(env_for=…)`, a container), `Mcp(scope=…)` connection partitions, and an `Extension`
for gates, limits and accounting.
