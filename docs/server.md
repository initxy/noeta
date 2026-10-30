# Running noeta as a server

One Postgres store, any number of `noeta worker` processes on any machines, and `noeta serve`
for the HTTP API in front of them. Every task is a log in the store, so a worker that dies
leaves nothing behind: another one claims its tasks where they stopped.

## Many workers

```sh
export NOETA_STORE=postgres://noeta:…@db.internal/noeta
noeta worker --concurrency 16            # on every worker machine, as many as you like
noeta serve --no-worker --host 0.0.0.0   # the API, on one or more machines
```

- **Claims, not assignments.** A worker takes a claimable task with `SKIP LOCKED`; a lease
  says how long a silent worker keeps it (`noeta worker --lease`, default 30 s). Writes are
  fenced by the claim's epoch, so a frozen worker that wakes up later can never write.
- **Live output crosses machines.** A worker publishes the model's streamed text through
  Postgres (`<schema>_deltas`, best effort, never stored); `noeta serve` hears it and streams
  it on `/watch`, so a front end attached to any server sees the text as it is written. On
  sqlite (one machine) live output stays in the process that runs the task; the recorded
  events still reach everyone.
- **Front ends attach anywhere.** The TUI (`noeta --server URL`) and editors through ACP
  (`noeta acp --server URL`) drive the server's tasks with the same API the server serves,
  with the token in `NOETA_SERVER_TOKEN`. They need no model of their own: a new task runs
  on the server's model, in the server's directory, unless
  `--model` names one or `--cwd PATH` names a directory on the server (a relative one is
  taken from the server's). `noeta -p` always runs here and refuses `--server`.
- **Same settings everywhere.** Each process reads its own `~/.noeta` (`NOETA_HOME`): agents,
  plugins, `config.toml`. Ship the same copy to every machine; a worker
  missing a plugin a task was started with never runs that task without it
  ([extending](extending.md#durable-and-multi-machine)).
- **Bypass mode.** Like `noeta serve`, a worker refuses `bypassPermissions` unless started
  with `--allow-bypass-permissions`.
- **Upgrades:** stop every worker (and `noeta serve`) before upgrading; workers of different
  versions must not share a store. The first process of the new version moves the store's
  schema forward in place; a process older than the store's schema refuses to open it
  (`StoreUnavailable`). On sqlite, a binary from before schema version 2 does not refuse:
  left running, it fails once the upgrade has dropped the old `holds` table, and started again
  it writes a new one, so stop every old worker before the upgrade. Each open that finds a
  `holds` table tells each tree held there that its hold is gone (once per tree), then drops
  the table; on
  Postgres it waits for an old worker's open transaction on that table.

## Where commands run (the workspace)

Tools read and write files and run commands in an exec env on the task's `cwd`. A task may
continue on another machine after any step, so that directory must look the same from every
worker. Two deployments work:

1. **A shared filesystem at the same path on every worker** (NFS, EFS, CephFS, …). Simplest:
   the default local exec env just works.
2. **A remote exec env**: commands run in a container on a host every worker reaches
   (`noeta.backends.execenvs.container`, or your own exec env in the `noeta.execenvs` group),
   chosen per task by the host's `Runtime(env_for=…)`.

A task's own files live where its tools do, in its exec env, so they follow the task to
any worker: long tool outputs saved for the model (the spill directory,
`~/.noeta/tool-results/<task id>/`, written through the exec env; an ended task's are removed
30 days after their last write), plan files (`~/.noeta/plans/`) and auto memory
(`~/.noeta/projects/<project>/memory/`, `~/.noeta/agent-memory/`; a project agent's is in
the workspace). The model reads and edits them with its file tools (and `grep`s them with
`Bash`), as in Claude Code, so they are files, not store rows; their paths are recorded in
the task's log the first time they are used.

- With a remote exec env they live in that env and nothing else needs sharing; noeta's home
  path must be the same on every worker (it names the files) and writable in the env.
- With the local exec env on a shared filesystem, those directories of noeta's home must be
  on the shared mount too, at the same path on every worker (`NOETA_HOME` on the mount, or
  `tool-results`, `plans`, `projects` and `agent-memory` linked to it); otherwise a task
  that moves finds its saved outputs, plan and memories missing.

Still local to a worker either way: a background `Bash` job and its log (the tree is held by
that worker while it runs, and its tasks are told `hold_lost` if the worker dies), and the
files of images people paste (`~/.noeta/image-cache` with the local exec env, a per-user
temp directory in any other): a worker that takes a task over writes them again from the
conversation at the next turn and names the new place.

## The token

`noeta serve` always requires a bearer token (`--no-token` only on a loopback address):
`NOETA_SERVER_TOKEN`, else one made on the first start and printed that once.
`~/.noeta/server-token` (owner-only) keeps only its SHA-256, so a client (`noeta --server`,
`noeta acp --server`) needs the token in its own `NOETA_SERVER_TOKEN`; delete the file and
restart for a new one. A file an older noeta left holding the token itself is rewritten to
its digest on the next start, which prints the token one last time. On a loopback bind only
the loopback `Host` names are answered (DNS rebinding); on any other bind `Host` is checked
only against the names given with `--allow-host NAME` (repeatable), so name them.

A request may set only the task config a client may (`model`, `effort`, `max_turns`, `cwd`,
`permission_mode`, `output_schema`, `timezone`), post only the signals front ends send, and create tasks
only in the server's directory and its `--allow-root` paths. Host code embedding the API uses
`create_app(client, token=...)` (or `token_sha256=`). `timezone` is the person's IANA zone
(`Europe/Berlin`), which noeta's own front ends send: without it the model is given the UTC
date, marked as UTC's, never the server's local one.

## Several parties

A store and a server serve one party: whoever holds the token drives every task, and tools run
as the worker's OS user ([ADR-028](adr/028-no-tenants-in-noeta.md)). To serve parties that
must not see each other, give each its own server and store schema (or its own machines), or
build the separation in host code on the extension points: queues and worker pools per party
(`noeta worker --queue NAME`), an exec env per task (`Runtime(env_for=…)`, a container),
`Mcp(scope=)` connection partitions, and an `Extension` for gates, limits and accounting.
