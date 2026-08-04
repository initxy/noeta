# Deploy with Docker

This guide shows you how to package a Noeta host as a container image and run
it — single-host with SQLite, or multi-host with Postgres. You need a host
program that drives `Client` (see [Your first agent](../tutorials/first-agent.md)
and [`examples/reference-host`](https://github.com/initxy/noeta/tree/main/examples/reference-host)).

Noeta ships no Dockerfile of its own — the library is in-process and has no
daemon — so what follows is the canonical shape a host's container takes.

## 1. Build the image

Noeta is a pure-Python library. Your image installs `noeta-sdk`, copies your
host code, and runs it. The runtime is in-process — there is no Noeta daemon to
start.

```dockerfile
# Dockerfile
FROM python:3.11-slim

WORKDIR /app

# System deps your tools need (git, curl, …). Trim to what your agent uses.
RUN apt-get update && apt-get install -y --no-install-recommends \
        git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install Noeta. Pin the version your host was built against.
RUN pip install --no-cache-dir "noeta-sdk>=0.4.0,<0.5.0"

# Copy your host.
COPY host.py .

# The workspace the agent operates on. Mount a volume here for persistence.
RUN mkdir -p /workspace
VOLUME ["/workspace"]

# Noeta reads provider / model keys from the environment.
ENV NOETA_MODEL="claude-sonnet-4-5-20250929"

CMD ["python", "host.py"]
```

Build and run:

```bash
docker build -t my-noeta-host .
docker run --rm -it \
    -v "$(pwd)/workspace:/workspace" \
    -e ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY" \
    my-noeta-host
```

```
Successfully tagged my-noeta-host:latest
```

## 2. Single-host: SQLite

For one container (or one host process), SQLite is the default durable store.
Point `HostConfig.storage_path` at a file on a mounted volume:

```python
from noeta.sdk import Client, HostConfig, Options

client = Client(
    Options(system_prompt="You are a helpful assistant."),
    provider=my_provider,
    workspace_dir="/workspace",
    model="claude-sonnet-4-5-20250929",
    host_config=HostConfig(storage_path="/data/noeta.sqlite"),
)
```

Mount the data volume so the store survives container restarts:

```bash
docker run -v noeta-data:/data -v "$(pwd)/workspace:/workspace" my-noeta-host
```

SQLite is **single-host only** — there is no cross-process write fencing. For
multiple containers sharing one store, use Postgres.

## 3. Multi-host: Postgres

Postgres gives you lease-fenced writes across multiple host processes, even on
different machines. Point `storage_path` at a `postgresql://` DSN:

```python
host_config = HostConfig(
    storage_path="postgresql://noeta:noeta@postgres:5432/noeta",
)
```

A `docker-compose.yml` running a host pool against Postgres:

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
      NOETA_MODEL: claude-sonnet-4-5-20250929
      NOETA_STORAGE_PATH: postgresql://noeta:noeta@postgres:5432/noeta
    volumes:
      - ./workspace:/workspace
    # Scale out: docker compose up --scale worker=4
    deploy:
      replicas: 1

volumes:
  pgdata:
```

Scale the worker pool horizontally:

```bash
docker compose up --scale worker=4
```

```
 ✔ Container noeta-postgres-1  Healthy
 ✔ Container noeta-worker-1    Started
 ✔ Container noeta-worker-2    Started
 ✔ Container noeta-worker-3    Started
 ✔ Container noeta-worker-4    Started
```

Every worker drains the same Postgres-backed dispatcher. Lease fencing
(`FOR SHARE` row checks against the database clock) guarantees that a task
whose lease was reclaimed cannot write behind the new generation.

## 4. Optional: run a sandbox inside the container

If your host provisions per-session containers (see
[Use a sandbox execution environment](use-sandbox.md)), the host container needs
access to the container runtime. Two common shapes:

### Docker-outside-of-Docker (DooD)

Mount the host's Docker socket so the host can spawn sibling containers:

```bash
docker run -v /var/run/docker.sock:/var/run/docker.sock my-noeta-host
```

The `DockerSandboxProvider` in the sandbox how-to works unchanged — it shells
out to `docker`, which now talks to the host daemon.

### Sidecar sandbox

Run the sandbox container as a sidecar in the same pod / compose stack and
attach to it via `SandboxExecEnvConfig`:

```python
from noeta.sdk import HostConfig, SandboxExecEnvConfig

host_config = HostConfig(
    exec_env=SandboxExecEnvConfig(
        base_url="http://sandbox:8080",
        api_key_env="SANDBOX_API_KEY",
        workdir="/workspace",
    ),
)
```

```yaml
services:
  sandbox:
    image: aio-sandbox:latest
    environment:
      SANDBOX_API_KEY: ${SANDBOX_API_KEY}
    volumes:
      - ./workspace:/workspace

  host:
    build: .
    depends_on: [sandbox]
    environment:
      SANDBOX_API_KEY: ${SANDBOX_API_KEY}
    volumes:
      - ./workspace:/workspace
```

## Environment variables

Noeta reads provider and sandbox credentials from the environment — never
hard-code them.

| Variable | Purpose |
| --- | --- |
| `ANTHROPIC_API_KEY` | Anthropic provider |
| `OPENAI_API_KEY` | OpenAI-compatible provider |
| `NOETA_WEB_SEARCH_API_KEY` | `WebSearch` tool |
| `SANDBOX_API_KEY` | Sandbox container auth (read at connect time) |

## Next steps

- [Deploy a worker](deploy-worker.md) — the resident worker pool and its knobs
- [Use a sandbox](use-sandbox.md) — the `ExecEnv` / `SandboxProvider` seam
- [Known limitations](../operations/limitations.md) — the SQLite single-host
  boundary
- [Troubleshooting](../operations/troubleshooting.md) — shutdown and lease
  symptoms in production
