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
- **Upgrades:** stop every worker before upgrading; workers of different versions must not
  share a store.

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

`noeta serve` always requires a bearer token (`--no-token` only on a loopback address). The
token in `NOETA_SERVER_TOKEN` or `~/.noeta/server-token` is the **default tenant's**; a
single-tenant server needs nothing else.

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

For hard isolation (separate machines or databases per tenant), run a worker pool per queue
(`noeta worker --queue acme`) or a store schema per tenant.
