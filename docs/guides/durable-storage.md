# Durable storage

By default Noeta runs with in-memory storage — conversations and the
session list disappear when the process exits. To persist sessions across
restarts, point the backend at a storage URL: a SQLite file path or a
PostgreSQL DSN.

## Enabling persistence

### Via env var

```bash
# SQLite file
NOETA_AGENT_STORAGE=./sessions.sqlite python -m noeta.agent

# PostgreSQL (the psycopg driver ships with noeta-runtime)
NOETA_AGENT_STORAGE=postgresql://user:pass@localhost:5432/noeta python -m noeta.agent
```

The legacy spelling `NOETA_AGENT_SQLITE` still works for file paths.

### Via config file

Add `storage_url` to your `NOETA_AGENT_CONFIG` JSON (the legacy key
`sqlite_path` is still accepted):

```json
{
  "provider_id": "openai",
  "model": "gpt-5.5",
  "base_url": "https://api.openai.com/v1",
  "api_key": "<your-key>",
  "workspace_dir": ".",
  "storage_url": "./sessions.sqlite"
}
```

```bash
NOETA_AGENT_CONFIG=noeta.config.json python -m noeta.agent
```

### In-process (SDK)

When embedding the engine in your own app, build the storage triple
yourself and pass it through `HostConfig`:

```python
from pathlib import Path
from noeta.agent.host.storage import open_durable_storage
from noeta.sdk import Client, HostConfig, Options

options = Options(
    system_prompt="You are a helpful assistant.",
    name="main",
    allowed_tools=("read", "write"),
    permission_mode="bypassPermissions",
)

# A sqlite path or a postgresql:// DSN — same call either way.
(event_log, content_store, dispatcher), storage_close = open_durable_storage(
    "./sessions.sqlite"
)

host_config = HostConfig(
    event_log=event_log,
    content_store=content_store,
    dispatcher=dispatcher,
)

client = Client(
    options,
    provider=my_provider,
    workspace_dir=Path("./my-project"),
    model="gpt-5.5",
    host_config=host_config,
)

try:
    outcome = client.start(goal="Analyze this codebase.")
    # ... sessions survive across restarts
finally:
    client.shutdown()
    storage_close()
```

## What gets stored

One SQLite file — or one PostgreSQL database — backs all three storage
adapters (`Sqlite*` / `Postgres*` prefixes, same behavioural contract):

| Adapter | What it stores |
| --- | --- |
| `SqliteEventLog` / `PostgresEventLog` | Per-task `EventEnvelope` records — the full history of every step (messages, tool calls, decisions, state changes). |
| `SqliteContentStore` / `PostgresContentStore` | Content-addressed blobs larger than the 4 KB event-payload cap: full LLM request/response bodies, large tool outputs, uploaded images. |
| `SqliteDispatcher` / `PostgresDispatcher` | Worker lease state + wake event queue. Lets a restarted process reclaim stale leases and deliver pending wake events. |

The SQLite file is created automatically on first write; the PostgreSQL
schema is created automatically on first connect (the DSN's database must
already exist). Use `:memory:` to get an in-memory SQLite instance —
useful for tests.

## Choosing a backend

- **SQLite** is the zero-setup default for a single machine: one file,
  no server, full durability (`synchronous=FULL`, WAL).
- **PostgreSQL** puts the same three adapters on a database server —
  choose it when the storage should live off-box (managed database,
  backups, operational tooling). The driver (`psycopg[binary]`, bundled
  libpq) ships with noeta-runtime — nothing extra to install.

## How fold recovery works

When the backend starts with `storage_url` set, it:

1. Opens the three adapters over the same file/database.
2. The `GET /tasks` read view folds each task's envelope stream into a
   status/title/closed summary — the sidebar session list appears
   immediately.
3. When you click a session, the frontend opens `GET /stream?task=<id>`
   and replays the envelopes from seq 0, reconstructing the full
   conversation view.

This is **fold, not load**: the Engine never deserializes a "state"
object. It re-derives every slice of state (RuntimeState, TaskState,
ContextState, GovernanceState) by replaying envelopes through the same
fold functions used during live execution.

## Key points

- **`NOETA_AGENT_STORAGE`** is the env var; `storage_url` is the config
  file key (legacy `NOETA_AGENT_SQLITE` / `sqlite_path` still accepted).
  The SDK default is in-memory (no file).
- **One database, three adapters.** `open_durable_storage()` constructs
  all three together so the event log already holds the dispatcher as its
  `lease_validator`.
- **Fold is deterministic.** Replaying the same envelopes always
  produces the same state — no separate "state table" to drift.
- **`storage_close()`** is your responsibility when using the SDK
  in-process. The app backend handles it automatically on shutdown.

## Source

- `apps/noeta-agent/noeta/agent/host/storage.py` — `open_durable_storage()`
- `apps/noeta-agent/noeta/agent/backend/lifecycle.py` — `BackendConfig.from_env()` (`NOETA_AGENT_STORAGE` → `storage_url`)
- SQLite adapters: `packages/noeta-runtime/noeta/storage/sqlite/`
- PostgreSQL adapters: `packages/noeta-runtime/noeta/storage/postgres/`
- `HostConfig`: `packages/noeta-sdk/noeta/client/host_config.py`
- See also: [Concepts](../concepts.md#eventlog),
  [Configuration](../reference/configuration.md),
  [ADR: Event-sourced truth](../adr/event-sourced-truth.md),
  [ADR: Storage protocols L0](../adr/storage-protocols-l0.md)
