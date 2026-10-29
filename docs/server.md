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
  on the server's model, in the server's directory (a tenant's: its workspace), unless
  `--model` names one or `--cwd PATH` names a directory on the server (a relative one is
  taken from the server's). `noeta -p` always runs here and refuses `--server`.
- **Same settings everywhere.** Each process reads its own `~/.noeta` (`NOETA_HOME`): agents,
  plugins, `config.toml`, tenant directories. Ship the same copy to every machine; a worker
  missing a plugin a task was started with never runs that task without it
  ([extending](extending.md#durable-and-multi-machine)).
- **Server directories and bypass mode.** A worker hides from other tenants' tasks the
  directory it started in, but knows nothing of where `noeta serve` started or its
  `--allow-root` paths: list them as `dirs = ["/srv/app", ...]` under `[server]` in
  `config.toml` (both read it) or pass `noeta worker --server-dir PATH`. A directory holding
  the machine's programs or `/tmp` cannot be hidden, so with other tenants configured
  `noeta serve` and `noeta worker` refuse to start in one (such as `/`) or with one listed.
  Like `noeta serve`, a worker refuses `bypassPermissions` unless started with
  `--allow-bypass-permissions`.
- **Upgrades:** stop every worker (and `noeta serve`) before upgrading; workers of different
  versions must not share a store. The first process of the new version moves the store's
  schema forward in place; a process older than the store's schema refuses to open it
  (`StoreUnavailable`). On sqlite, a binary from before schema version 2 does not refuse:
  left running, it keeps writing the upgraded store and holds trees in the old `holds` table,
  which newer workers do not read, so stop every old sqlite worker before the upgrade. A
  store from before schema version 2 keeps its old `holds` table, unused since; dropping it
  is left to you.

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

`noeta serve` always requires a bearer token (`--no-token` only on a loopback address, and
not once a tenant token or tenant directory exists):
`NOETA_SERVER_TOKEN`, else one made on the first start and printed that once.
`~/.noeta/server-token` (owner-only) keeps only its SHA-256, so a client (`noeta --server`,
`noeta acp --server`) needs the token in its own `NOETA_SERVER_TOKEN`; delete the file and
restart for a new one. A file an older noeta left holding the token itself is rewritten to
its digest on the next start, which prints the token one last time. This token is the
**default tenant's**; a single-tenant server needs nothing else. On a loopback bind only the
loopback `Host` names are answered (DNS rebinding); on any other bind `Host` is checked only
against the names given with `--allow-host NAME` (repeatable), so name them.

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

- `workspace` is required for every tenant but the default one: `noeta serve` refuses to
  start while a tenant in `tokens.json` or `~/.noeta/tenants/` has none, `noeta token add`
  refuses such a tenant, and a request of a tenant without one (a token added while the
  server runs, or host code's `create_app` without `workspaces=`) may not start a task or
  name a `cwd` (403). It is checked where tasks come in (the HTTP API, including `config`
  signals and stream-json): a `cwd` must lie in the tenant's workspace and never in another
  tenant's, even one nested inside its own. The default tenant works in the server's
  directory and `--allow-root` paths. With a shared filesystem, put workspaces there.
- `queues` is checked on `POST /tasks`: a queue not listed answers 403. Without it a tenant
  may use `default` and the queue of its own name, so a pool started with
  `noeta worker --queue acme` takes only acme's tasks (and the default tenant's, which may
  use any queue). A task's sub-agents go to its queue (host code may route them
  elsewhere with `Subtasks(queue=)`).
- `max_running` is enforced by the claim, exactly, across all workers; other tenants are not
  held up behind it.
- A model call past a budget fails its turn with `budget_spent` and a message that names the
  tenant, the month and the numbers; the conversation stays and continues next month or once
  the budget is raised. Usage is counted from when a budget is set (table `tenant_usage`).
- A tenant with its own `provider`, `base_url` or `extra_headers` (a table of headers sent
  with its model calls) must set its own non-empty `api_key`: `noeta serve` and
  `noeta worker` refuse to start otherwise. It never borrows the server's key, the worker's
  `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` or the server's `extra_headers`.

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
2. **A remote exec env (host code only)**: tools run in a container per task, chosen by
   `Runtime(env_for=…)` in host code that embeds noeta. The `noeta` command cannot set one
   up, and its start-up check does not recognise one: such a host runs its own workers.
3. **The operator accepts tenants separated at the API only**:
   `shared_os_user_tenants = true` under `[server]` in `~/.noeta/config.toml`. Tenants then must trust each
   other and the operator with everything the OS user can reach.

A task of a tenant other than the default one may read what the OS user can, except a
protected set:

- noeta's home (`NOETA_HOME`: tokens, settings, tenant directories, other tasks' files) and
  the spill directory wherever they are, but for the task's own corners: its spilled tool
  outputs (its task's and its root's), its tenant's plan files (`plans/<tenant>/`), in its
  tenant's project directory of its working directory (`projects/<tenant>/<slug>/`) the
  memory and, to read, its own and its root's compaction transcripts, and read access to
  `skills/` and `workflows/`;
- the sqlite store and its `-wal` / `-shm` files, `/etc/noeta` and `/proc`;
- noeta's places in the temp directories (`noeta-background-<uid>`, `noeta-temp-<uid>`)
  but for its tenant's temporary directory (`noeta-temp-<uid>/tenants/<tenant>/`: its job
  logs and its commands' `TMPDIR`);
- other tenants' workspaces;
- the server's own directories (the default tenant's workspace: the directory `noeta serve`
  or `noeta worker` started in, `--allow-root` and `--server-dir` paths and `[server] dirs`,
  with the server's `.env`), but a tenant's workspace inside one; a tenant's task may not
  run in a server directory that lies inside its workspace;
- the sandbox's credential stores in every home directory (the worker's `~`, each
  account's, each directory in `/home` and `/Users`): `.ssh`, `.aws`, `.config/gh`,
  `.netrc` and the rest of `DEFAULT_DENY_READ` in `noeta/toolkit/sandbox.py`;
- the operator's sandbox `filesystem.denyRead` (with `Read(...)` deny rules and
  `credentials` files), whether or not the sandbox is enabled.

It writes only in its tenant's workspace, its own corners and its tenant's temporary
directory. `/opt`, `/srv`, `/var`, the rest of `/tmp`, data disks and the rest of a home
directory stay readable: put anything else a tenant must not read in `denyRead`. The
operator's `additionalDirectories` (and the sandbox's `allowWrite` / `allowRead`) are the
default tenant's only. No allow rule, permission mode (`bypassPermissions` included) or
approval lifts any of this. The default tenant's tasks are not
fenced, their sandbox is the settings' own, and their project directories stay
`projects/<slug>/`. A `log_dir` given to the shell plugin holds the default tenant's job
logs only; it is not fenced, so keep it inside noeta's home.

- **File tools** (`Read`, `Edit`, `Write`, `NotebookEdit`, a workflow's `scriptPath`) run in
  the worker process whatever the deployment. The fence is checked at the permission gate after symlinks and `..` are
  resolved, and again by the local exec env on the file actually opened, so a link swapped
  in between does not help. A PDF is read once through the fence and its page converters
  get a copy.
- **Commands**, under the sandbox: `Bash` (foreground, background, `Monitor`), a skill's
  script, an inline skill command and every `git` noeta runs in the tenant's checkout (for
  a worktree, the first turn's git snapshot, memory's repository lookup; with the hooks and
  config programs such as `gpg.program` or filter drivers the tenant set there) run inside
  the sandbox with the protected set above unreadable but for the tenant's workspace, its
  own corners and the Python running the sandbox's network bridge (`TenantFence.confine`;
  the rest of the machine stays readable as for any sandboxed command), always: the
  settings' `excludedCommands`, `dangerouslyDisableSandbox`, `filesystem.disabled`,
  `enableWeakerNestedSandbox`, `allowNestedUserNamespaces` and `network.allowUnixSockets` /
  `network.allowAllUnixSockets` do not apply to them (a daemon's socket, such as the user's
  D-Bus or Docker's, would run their commands outside the sandbox). On Linux each command has its own PID
  namespace, so no other process (`/proc/<pid>/environ`) is visible; writes go only to
  the command's working directory (in the tenant's workspace) and its tenant's temporary
  directory. A worktree under `.noeta/worktrees/` cannot be made from inside the
  sandbox (`.noeta/` is read-only there), so `EnterWorktree` and isolated agents fail for
  such a task.
- **MCP**: servers that run a process on the worker (stdio servers, a `headersHelper`) do
  not start for such a task, whoever configured them; they show as down. Remote servers do,
  with the headers configured for them, in a connection partition of the tenant's own
  (`tenant:<name>/<scope>`), so no connection or OAuth sign-in is shared with another
  tenant.

Not a boundary, even under the strict sandbox:

- Files outside the protected set: a tenant reads what the OS user can (other software's
  settings and data, the rest of a home directory, data disks). Credential stores are
  known by their usual names in the home directories noeta finds; a token kept anywhere
  else (an app's own settings, a `.env` outside the server's directories) is readable
  unless `denyRead` names it.
- Name case on Linux: the file tools' fence compares names with case there (without case
  on macOS and Windows), so on a case-insensitive file system mounted on Linux, `~/.SSH`
  reaches `~/.ssh` past it.
- What the operator runs outside the sandbox: hooks (`settings.json` `hooks`, run as the OS
  user with the tool call on stdin), plugins (in-process code), the MCP servers of default
  tenant tasks. A hook starts in the task's working directory: one that runs `git` or a
  project's scripts there runs what the tenant wrote (its repository config, its files) as
  the OS user.
- The sandbox profile itself is built by the worker: it runs `git rev-parse --git-path
  hooks` in the tenant's checkout as the OS user to find the hooks directory to protect.
  That reads the repository's config but runs nothing from it.
- A remote MCP server configured for the server reaches tenants with its configured
  headers, and `WebFetch` fetches any address the worker can reach (loopback and cloud
  metadata addresses included); the sandbox's network filter confines commands only.
- File and directory names: the permission gate expands a command's globs, and the
  instructions and skills plugins list directories of the working directory (following
  links), as the OS user; the files themselves are then read through the fence.
- On macOS, Seatbelt does not hide other processes: a command can list them and may see
  the arguments and environment of the worker's own (`ps eww`).
- A trusted tenant workspace is configuration: its `.noeta/settings.json`, hooks, plugins and
  agents load as the operator's own. Do not `noeta trust` a tenant's workspace. Untrusted,
  its `.noeta/` settings, agents, plugins, prompts, skills and `mcp.json` are not read. What
  is read from it either way is text for that tenant's own task (`NOETA.md`, `AGENTS.md`,
  `.noeta/rules`, through the fence) or input like a tool call's: a saved workflow
  (`.noeta/workflows/*.js`) runs in the in-process JavaScript engine as a `script` argument
  would, with no file or process access. A host's `Skills(skills_dir=)` is not
  trust-gated; its scripts still run in the sandbox.

A token for another tenant added while a non-isolated server runs is not accepted (401) until
it is restarted with one of the above. `noeta serve --no-worker` runs no tools and is not
checked; the workers check themselves, against their own settings and tenant directories.
A worker that knows of no tenants (no tokens file, no tenant directories, as on another
machine) still starts, but when it claims a task of a tenant other than the default one
without one of the boundaries above, it runs none of that task's tools: the turn fails with
`tenant_not_isolated` and the reason, before any model call (a one-shot task ends failed;
an interactive one waits for its next message). The same happens, with the boundary in
place, to a task of a tenant whose workspace the worker does not know: nothing would say
where its commands may write. Workers read the tenant directories when they start, and a
worker hides from a tenant's tasks only the workspaces it knows, so every worker needs the
same tenant set: when adding a tenant, copy its directory to every machine and restart the
workers before giving out its token. Give every worker the same `~/.noeta`, or route
tenants to their own queues (`queues = ["acme"]`) served only by workers set up for them.

For hard isolation (separate machines or databases per tenant), run a worker pool per queue
(`noeta worker --queue acme`) or a store schema per tenant.
