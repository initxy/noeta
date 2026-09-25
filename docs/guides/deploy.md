# Deploy to production

Run the same agent from a single process up to several hosts sharing Postgres.
Only the storage and the worker pool change; the agent definition doesn't.

<NtScale />

| Stage | Storage | Who drives tasks | Survives a crash? |
| --- | --- | --- | --- |
| In-process | in memory (default) | the calling thread | no |
| One host | SQLite file | `Client.start_workers(n)` | yes |
| Several hosts | Postgres | a worker pool on each host | yes, and hosts take over each other's tasks |

## 1. In-process

```python
from noeta.sdk import Client, Options
from noeta.sdk.providers import AnthropicProvider

client = Client(Options(system_prompt="You are a helpful assistant."), provider=AnthropicProvider(), model="claude-sonnet-5")
outcome = client.start(goal="Summarize README.md.")
```

With no `HostConfig`, the event log lives in memory and dies with the process.
Fine for scripts and tests; timers never fire and nothing is recovered.

## 2. One host: SQLite + a worker pool

```python
from noeta.sdk import Client, HostConfig, Options
from noeta.sdk.providers import AnthropicProvider

client = Client(
    Options(system_prompt="You are a helpful assistant."),
    provider=AnthropicProvider(),
    model="claude-sonnet-5",
    workspace_dir="./workspace",
    host_config=HostConfig(storage_path="./noeta.sqlite"),
)

with client:
    client.start_workers(4)
    seeded = client.seed_start(goal="Summarize README.md.")
    client.dispatch_seeded(seeded)      # returns at once; a worker drives the turn
    ...
```

- `storage_path` takes a SQLite file path, a `postgresql://` DSN, or `":memory:"`.
- `start_workers(n)` starts `n` daemon threads that drain the ready queue. Without
  a pool, `wait_timer` suspensions never fire and a task whose worker crashed is
  never picked up again.
- `seed_start` + `dispatch_seeded` is the shape for an HTTP handler: the turn is
  durably recorded before you return, and a worker runs it. `start()` still
  drives the turn on the calling thread.
- `start_workers` can be called once; a second call raises `RuntimeError`.

## 3. Several hosts: Postgres

```python
host_config = HostConfig(storage_path="postgresql://noeta:noeta@postgres:5432/noeta")
```

Every host runs its own pool against the same database. Writes are checked
against the live lease inside the database transaction, so a worker whose lease
was taken over can't write behind the new owner. The Postgres driver ships with
`noeta-sdk`.

::: warning SQLite is single-host
SQLite has no cross-host lease check. Several workers in one process are fine;
several processes or machines on one SQLite file are not.
:::

**Different agents, one database.** Give each client its own
`HostConfig(queue="…")`. Root tasks are created on their client's queue,
children inherit it, and a pool only claims its own queue — so two differently
configured clients never run each other's tasks. See
[ADR: worker queue routing](https://github.com/initxy/noeta/blob/main/docs/adr/worker-queue-routing.md).

## Package it with Docker

```dockerfile
FROM python:3.11-slim

WORKDIR /app

# ripgrep is required: the Grep and Glob tools run `rg`, and fail without it.
# git and the rest are whatever your own tools need.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ripgrep git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir "noeta-sdk>=0.6,<0.7"

COPY host.py .
RUN mkdir -p /workspace
VOLUME ["/workspace"]

CMD ["python", "host.py"]
```

::: tip ripgrep is required
The `Grep` and `Glob` tools run `rg`. An image without ripgrep fails on the
agent's first search.
:::

`host.py` runs a pool until the container is stopped:

```python
import os
import signal
import threading

from noeta.sdk import Client, HostConfig, Options
from noeta.sdk.providers import AnthropicProvider

client = Client(
    Options(system_prompt="You are a helpful assistant."),
    provider=AnthropicProvider(),
    model="claude-sonnet-5",
    workspace_dir="/workspace",
    host_config=HostConfig(storage_path=os.environ["NOETA_STORAGE_PATH"]),
)

stop = threading.Event()
signal.signal(signal.SIGTERM, lambda *_: stop.set())

with client:                      # leaving the block stops the pool
    client.start_workers(4)
    stop.wait()
```

A `docker-compose.yml` for several workers on Postgres:

```yaml
services:
  postgres:
    image: postgres:16
    environment:
      POSTGRES_USER: noeta
      POSTGRES_PASSWORD: noeta
      POSTGRES_DB: noeta
    volumes:
      - pgdata:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U noeta"]
      interval: 5s
      timeout: 5s
      retries: 10

  worker:
    build: .
    depends_on:
      postgres:
        condition: service_healthy
    environment:
      ANTHROPIC_API_KEY: ${ANTHROPIC_API_KEY}
      NOETA_STORAGE_PATH: postgresql://noeta:noeta@postgres:5432/noeta
    volumes:
      - ./workspace:/workspace

volumes:
  pgdata:
```

```bash
docker compose up --scale worker=4
```

For SQLite in a single container, point `storage_path` at a file on a mounted
volume (`-v noeta-data:/data`, `storage_path="/data/noeta.sqlite"`).

| Environment variable | Read by |
| --- | --- |
| `ANTHROPIC_API_KEY` | `AnthropicProvider()` |
| `OPENAI_API_KEY` | OpenAI-compatible providers |
| `NOETA_WEB_SEARCH_API_KEY` | enables the `WebSearch` tool |
| `SANDBOX_API_KEY` | sandbox container auth (see [Sandbox](sandbox.md)) |

`NOETA_STORAGE_PATH` above is this example's own convention — `host.py` reads
it, Noeta doesn't.

## Tune the pool

All are keyword arguments of `start_workers`:

| Parameter | Default | What it does |
| --- | --- | --- |
| `num_workers` | `1` | number of worker threads (must be ≥ 1) |
| `poll_interval` | `0.1` s | sleep when the ready queue is empty |
| `heartbeat_interval` | `30.0` s | how often a running step renews its lease |
| `stale_sweep_interval` | `10.0` s | how often expired leases are reclaimed |
| `timer_poll_interval` | `1.0` s | how often due timers are fired |
| `lease_seconds` | `600.0` s | initial lease length per task |
| `shutdown_grace_s` | `10.0` s | how long a stopping worker waits for its current step; `None` = forever |

## Shut down

```python
stopped = client.stop_workers(timeout=30.0)   # True if every worker exited
```

Workers stop taking new tasks and wait up to `shutdown_grace_s` for the step in
flight. A step that runs longer is abandoned — it may still be writing, so
**exit the process** afterwards. Its lease then expires and another worker
reclaims the task. On a timeout, `stop_workers` returns `False` and keeps the
pool tracked, so you can call it again. `Client.shutdown()` (and leaving a
`with client:` block) stops the pool first.

A host with no `Client` can run the loop directly — see
[WorkerLoop](../reference/worker-loop.md).

## Next

- [Sandbox](sandbox.md) — run the agent's tools in a container
- [Tasks and waking](../how-it-works/tasks-and-waking.md) — what a worker does when it picks up a task
- [Limitations](../operations/limitations.md) — the SQLite and crash-recovery boundaries
