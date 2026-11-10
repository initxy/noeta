# Deploy with Docker

This page runs noeta as a small cluster on one Docker host: Postgres, one `noeta serve` for the
HTTP API, and several `noeta worker` containers that share the tasks. The files are in
[`deploy/`](https://github.com/initxy/noeta/tree/main/deploy). What each command and flag does,
and how to spread the same setup over several machines, is on
[Run it as a server](./server.md).

## What is in `deploy/`

| File | What it is |
|---|---|
| `Dockerfile` | the image: noeta built from the checkout with the `postgres` and `server` extras, on `python:3.12-slim-bookworm` |
| `compose.yaml` | Postgres 16, `serve` (API only, `--no-worker`) and `worker` (3 replicas by default) |
| `.env.example` | the settings `compose.yaml` reads: model, key, token, Postgres password, worker count, port |

No image is published; you build it from the source you checked out.

## The image

```sh
docker build -f deploy/Dockerfile -t noeta .      # from the repository root
docker run --rm noeta --version
```

- **What it runs.** The entry point is `noeta`; the default command is `worker`. Pass `serve …`
  or any other subcommand instead.
- **What it installs.** The dependencies at the versions in `uv.lock`, then the wheel of the
  checkout. `git`, `ripgrep` (the model's `Bash` searches reach for `rg`), `bubblewrap` (the
  sandbox, [off by default here](#commands-and-the-sandbox)), `openssh-client`, and `tini`,
  which reaps the processes commands leave behind. No terminal UI, web app or ACP.
- **Other extras.** `docker build --build-arg EXTRAS="postgres server acp" …` installs more.
- **Who it runs as.** The user `noeta` (uid 1000; change it with `--build-arg UID=… GID=…` to
  match the owner of a mounted workspace). noeta's home is `/home/noeta/.noeta`
  (`NOETA_HOME`); the working directory is `/workspace`.

## Run the cluster

```sh
cp deploy/.env.example deploy/.env
# fill in NOETA_MODEL, NOETA_API_KEY (and NOETA_PROVIDER / NOETA_BASE_URL for a non-Anthropic
# model), NOETA_SERVER_TOKEN and POSTGRES_PASSWORD
docker compose -f deploy/compose.yaml up -d --build --wait
```

Compose reads `deploy/.env` because it sits next to `compose.yaml`. `--wait` returns once
Postgres and `serve` report healthy (the `serve` check calls `GET /agents` with the token).
The API listens on `127.0.0.1:8642` of the Docker host; set `NOETA_PORT=0.0.0.0:8642` to reach
it from other machines, and add the name they use as another `--allow-host` in
`compose.yaml` (`serve` answers only the `Host` names it is given:
`localhost`, `127.0.0.1` and `serve`).

Start a task and follow it:

```sh
export NOETA_SERVER_TOKEN=…            # the value in deploy/.env
H="Authorization: Bearer $NOETA_SERVER_TOKEN"
curl -s -X POST http://127.0.0.1:8642/tasks -H "$H" -H 'content-type: application/json' \
  -d '{"agent":"main","input":"list the files here"}'
curl -sN http://127.0.0.1:8642/tasks/<task id>/watch -H "$H"
```

Or attach the terminal UI from any machine that has noeta installed:
`noeta --server http://127.0.0.1:8642`. Every endpoint is in
[HTTP / WebSocket API](../reference/http-api.md).

A task created over HTTP starts in the `bypassPermissions` permission mode unless the request
sets `permission_mode`; anything that needs a person waits in `GET /tasks/{id}/pending` until it is
answered. Start both `serve` and the workers with `--disable-bypass-permissions` to refuse
`bypassPermissions` (tasks then start in `auto`).

## What the containers share

Every noeta container mounts the same two volumes at the same paths, because a task may
continue on any worker after any step:

- **`workspace` at `/workspace`**: the tasks' working directory. Put the repositories the
  agent works on here (`docker compose cp`, a `git clone` from a task, or replace the named
  volume with a bind mount of a host directory).
- **`noeta-home` at `/home/noeta/.noeta`**: agents, plugins, `config.toml`, settings files and
  MCP servers, so every worker runs with the same ones; also saved long tool outputs, plan
  files and auto memory, which follow a task to whichever worker runs it next.

Postgres keeps the tasks in the `pgdata` volume. Background `Bash` jobs and their logs stay in
the worker that started them ([Known limitations](../operations/limitations.md#several-machines)).

These are named volumes on one Docker host. To run workers on several hosts, give them a
shared filesystem mounted at the same paths on every host (an NFS volume, for example), point
`NOETA_STORE` at a Postgres they all reach, and run the same image there.

## Scale, stop and restart

```sh
docker compose -f deploy/compose.yaml up -d --scale worker=5   # or NOETA_WORKERS in .env
docker compose -f deploy/compose.yaml stop worker              # SIGTERM: tasks go back to the store
docker compose -f deploy/compose.yaml logs -f worker
```

- **A worker that stops cleanly** (`docker stop`, `compose stop`, a rolling restart) hands its
  running tasks back to the store at once, and another worker continues them from their logs.
- **A worker that is killed** (`docker kill`, the host losing power) holds its tasks until their
  lease runs out (30 seconds; `worker --lease`), then another worker claims them. A tool call
  that was running when it died is told to the model as "outcome unknown", unless the tool can
  safely run again ([One writer and crash recovery](../how-it-works/recovery.md)).
- **Upgrades.** Workers of different versions must not share a store. Pull the new source,
  then `docker compose -f deploy/compose.yaml down` (volumes stay), and
  `up -d --build --wait`; the first new process moves the store's schema forward.

## Commands and the sandbox

Inside these containers, commands the model runs execute as the `noeta` user with no sandbox:
the container is the boundary. A command sees the workspace, noeta's home, and the network the
container has; it never gets noeta's own credentials (`NOETA_API_KEY`, `NOETA_STORE`,
`NOETA_SERVER_TOKEN` are withheld from every command). The permission rules and modes still
apply ([Permissions and hooks](./permissions.md)).

The Linux sandbox (bubblewrap) needs user namespaces and a fresh `/proc`, which Docker's
default profile blocks. It starts inside a container only with all three of these options on
the worker, each of which loosens the container itself:

```yaml
  worker:
    security_opt:
      - seccomp=unconfined
      - apparmor=unconfined
      - systempaths=unconfined
```

Then turn it on in the shared home's `settings.json` (`"sandbox": {"enabled": true,
"failIfUnavailable": true}`, [Where commands run](./sandbox.md)). With only the first two the
sandbox does not start (bwrap cannot mount `/proc`), whatever `enableWeakerNestedSandbox` says.
For untrusted code, a container per task from host code (`Runtime(env_for=…)`) is the
stronger fence.

## The web app

The image is built from the source tree without node, so it does not include
[the web app](./front-ends.md#the-web-app) the published package carries. To serve it, build
`web/dist` on the host (`cd web && npm ci && npm run build`), mount it into `serve`
(`- ../web/dist:/web:ro`) and add `--web /web` to its command.
