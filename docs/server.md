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
  (`noeta acp --server URL`) drive the server's tasks with the same API the server serves.
- **Same settings everywhere.** Each process reads its own `~/.noeta` (`NOETA_HOME`): agents,
  plugins, `config.toml`, tenant directories. Ship the same copy to every machine; a worker
  missing a plugin a task was started with fails that task with `plugin_missing` instead of
  running it without.
- **Upgrades:** stop every worker (and `noeta serve`) before upgrading; workers of different
  versions must not share a store. The first process of the new version moves the store's
  schema forward in place; a process older than the store's schema refuses to open it
  (`StoreUnavailable`; on sqlite, a process from before schema version 2 does not check, one
  more reason every worker stops first). A store from before schema version 2 keeps its old `holds` table,
  unused since; dropping it is left to you.

## Where commands run (the workspace)

Tools read and write files and run commands in an exec env on the task's `cwd`. A task may
continue on another machine after any step, so that directory must look the same from every
worker. Two deployments work:

1. **A shared filesystem at the same path on every worker** (NFS, EFS, CephFS, …). Simplest:
   the default local exec env just works. Put tenants' workspaces on it too.
2. **A remote exec env**: commands run in a container on a host every worker reaches
   (`noeta.backends.execenvs.container`, or your own exec env in the `noeta.execenvs` group),
   chosen per task by the host's `Runtime(env_for=…)`.

Still local to a worker either way: a background `Bash` job and its log (the tree is held by
that worker while it runs, and its tasks are told `hold_lost` if the worker dies), and the
spill directory for long tool outputs (`~/.noeta/tool-results`; put it on the shared mount if
front ends on other machines should open those files).

## Tokens and tenants

`noeta serve` always requires a bearer token (`--no-token` only on a loopback address):
`NOETA_SERVER_TOKEN`, else one made on the first start and printed that once.
`~/.noeta/server-token` (owner-only) keeps only its SHA-256, so a client (`noeta --server`,
`noeta acp --server`) needs the token in its own `NOETA_SERVER_TOKEN`; delete the file and
restart for a new one. A file an older noeta left holding the token itself is rewritten to
its digest on the next start, which prints the token one last time. This token is the
**default tenant's**; a single-tenant server needs nothing else.

For more tenants, give each its own tokens:

```sh
noeta token add --tenant acme --name ci   # prints the token once; only its SHA-256 is kept
noeta token list                          # digest prefix, tenant, date, name
noeta token remove 3f2a9c1d0b7e           # revoke by digest prefix
```

Tokens live in `~/.noeta/tokens.json` (owner-only) on the API servers; a change applies to the
next request without a restart. A request sees and drives only its tenant's tasks: another
tenant's task answers 404 on every route, `/tasks` lists only its own, and inside a task
`SendMessage` and `ListAgents` reach only the same tenant. Sub-agents inherit their parent's
tenant. Host code embedding noeta gets the same with `Client(store).scoped("acme")` and
`create_app(client, tokens={token: tenant})`.

What a tenant may do lives in `~/.noeta/tenants/<name>/config.toml` (read by `noeta serve` and
`noeta worker`; keep the copies the same):

```toml
workspace = "/srv/shared/acme"   # its tasks' cwd must lie in it; tasks start there by default
queues = ["acme"]                # the queues its requests may create tasks on
max_running = 4                  # its tasks running at once; the rest wait their turn
budget_tokens = 5_000_000        # per calendar month (UTC), input + output tokens
budget_usd = 50.0                # per calendar month, at catalog prices
provider = "openai_compat"       # its own model endpoint and key instead of the server's
base_url = "https://llm.acme.example/v1"
api_key = "{env:ACME_API_KEY}"   # or "{keyring:service/user}"; never the secret itself
```

- `workspace` is checked where tasks come in (the HTTP API, including `config` signals and
  stream-json); with a shared filesystem, put it there.
- `queues` is checked on `POST /tasks`: a queue not listed answers 403. Without it a tenant
  may use `default` and the queue of its own name, so a pool started with
  `noeta worker --queue acme` takes only acme's tasks (and the default tenant's, which may
  use any queue).
- `max_running` is enforced by the claim, exactly, across all workers; other tenants are not
  held up behind it.
- A model call past a budget fails its turn with `budget_spent` and a message that names the
  tenant, the month and the numbers; the conversation stays and continues next month or once
  the budget is raised. Usage is counted from when a budget is set (table `tenant_usage`).
- A tenant with its own `provider` never borrows the server's key.

### What separates tenants

The API is a boundary: a tenant's token reaches only its own tasks, sets only the task config
a client may (`model`, `effort`, `max_turns`, `cwd`, `permission_mode`, `output_schema`),
posts only the signals front ends send, and creates tasks only in its workspace and on its
queues. Tools are another matter: they run where the worker runs, as the worker's OS user.
Without something in between, a tenant whose task runs `Bash` (approved by that same tenant)
can read `~/.noeta` (the tokens file, the sqlite store, other tenants' settings), the
worker's environment (`/proc/<pid>/environ`: provider keys) and any file that user can.

So `noeta serve` (with its worker) and `noeta worker` refuse to start when this machine knows
tenants besides the default one (a token in `tokens.json` or a tenant directory), unless one
of these holds:

1. **The sandbox confines every command**: `sandbox.enabled: true`,
   `sandbox.failIfUnavailable: true` and `sandbox.allowUnsandboxedCommands: false` in
   `~/.noeta/settings.json`. `Bash` then runs inside bubblewrap or Seatbelt.
2. **A remote exec env**: tools run in a container per task (`Runtime(env_for=…)`, from host
   code embedding noeta; the `noeta` command does not set one up).
3. **The operator accepts tenants separated at the API only**:
   `shared_os_user_tenants = true` under `[server]` in `~/.noeta/config.toml`. Tenants then must trust each
   other and the operator with everything the OS user can reach.

A task of a tenant other than the default one is fenced off from these places: noeta's
home (`NOETA_HOME`: tokens, settings, tenant directories, other tasks' files), the spill
directory wherever it is, the sqlite store and its `-wal` / `-shm` files wherever they are,
`/etc/noeta`, `/proc` and every other tenant's workspace. Inside them such a task keeps its
own spilled tool outputs, its tenant's plan files (`plans/<tenant>/`), the memory of its
working directory (`projects/<slug>/`) and read access to `skills/` and `workflows/`. No
allow rule, permission mode (`bypassPermissions` included) or approval lifts the fence. The
default tenant's tasks are not fenced, and their sandbox is the settings' own.

- **File tools** (`Read`, `Edit`, `Write`, `NotebookEdit`, a workflow's `scriptPath`) run in
  the worker process whatever the deployment. The fence is checked at the permission gate after symlinks and `..` are
  resolved, and again by the local exec env on the file actually opened, so a link swapped
  in between does not help. A PDF is read once through the fence and its page converters
  get a copy.
- **Commands**, under the sandbox: `Bash` (foreground, background, `Monitor`), a skill's
  script, an inline skill command and `git` for a worktree (its hooks included) run inside
  the sandbox with the same places unreadable (`TenantFence.confine`), always: the
  settings' `excludedCommands`, `dangerouslyDisableSandbox`, `filesystem.disabled` and
  `enableWeakerNestedSandbox` do not apply to them. On Linux each command has its own PID
  namespace, so no other process (`/proc/<pid>/environ`) is visible; writes go only where
  the settings allow. A worktree under `.noeta/worktrees/` cannot be made from inside the
  sandbox (`.noeta/` is read-only there), so `EnterWorktree` and isolated agents fail for
  such a task.
- **MCP**: servers that run a process on the worker (stdio servers, a `headersHelper`) do
  not start for such a task, whoever configured them; they show as down. Remote servers do,
  with the headers configured for them, in a connection partition of the tenant's own
  (`tenant:<name>/<scope>`), so no connection or OAuth sign-in is shared with another
  tenant.

Not a boundary, even under the strict sandbox:

- What the operator runs outside the sandbox: hooks (`settings.json` `hooks`, run as the OS
  user with the tool call on stdin), plugins (in-process code), the MCP servers of default
  tenant tasks.
- A remote MCP server configured for the server reaches tenants with its configured
  headers, and `WebFetch` fetches any address the worker can reach (loopback and cloud
  metadata addresses included); the sandbox's network filter confines commands only.
- File and directory names: the permission gate expands a command's globs, and the
  instructions and skills plugins list directories of the working directory (following
  links), as the OS user; the files themselves are then read through the fence.
- On macOS, Seatbelt does not hide other processes: a command can list them and may see
  the arguments and environment of the worker's own (`ps eww`).
- A trusted tenant workspace is configuration: its `.noeta/settings.json`, hooks, plugins and
  agents load as the operator's own. Do not `noeta trust` a tenant's workspace.

A token for another tenant added while a non-isolated server runs is not accepted (401) until
it is restarted with one of the above. `noeta serve --no-worker` runs no tools and is not
checked; the workers check themselves, against their own settings and tenant directories.
A worker that knows of no tenants (no tokens file, no tenant directories, as on another
machine) still starts, but when it claims a task of a tenant other than the default one
without one of the boundaries above, it runs none of that task's tools: the turn fails with
`tenant_not_isolated` and the reason, before any model call (a one-shot task ends failed;
an interactive one waits for its next message). Give every worker the same `~/.noeta`, or
route tenants to their own queues (`queues = ["acme"]`) served only by workers set up for
them.

For hard isolation (separate machines or databases per tenant), run a worker pool per queue
(`noeta worker --queue acme`) or a store schema per tenant.
