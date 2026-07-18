# Configuration

The platform (`python -m noeta.agent`) is configured through
**`apps/noeta-agent/.env`** plus environment variables — environment
variables take precedence over the file, the file over built-in defaults.
There are no CLI flags. Source of truth:
`apps/noeta-agent/noeta/agent/config.py` (pydantic-settings; unknown keys in
a legacy `.env` are ignored). `apps/noeta-agent/.env.example` is the
annotated starter copy.

**Every key is optional.** With everything left empty the server runs fully
offline: the deterministic mock LLM, dev-login, SQLite storage, no sandbox.

Relative paths (`DATA_DIR`, `SHARED_DATA_DIR`, `MODELS_CONFIG`) resolve
against the application project root `apps/noeta-agent/`.

## Server

| Key | Default | Purpose |
| --- | --- | --- |
| `HOST` | `127.0.0.1` | Bind interface. |
| `PORT` | `8000` | Listen port. |
| `LOG_LEVEL` | `INFO` | Backend log level. |
| `CORS_ORIGINS` | `http://127.0.0.1:5173,http://localhost:5173` | Comma-separated allowed origins (only needed for a separately-served frontend dev server; `make dev`'s vite proxy does not need it). |

## Paths and storage

| Key | Default | Purpose |
| --- | --- | --- |
| `DATA_DIR` | `data` | The writable data root (below). |
| `SHARED_DATA_DIR` | `data/shared` | Backend-writable content mounted **read-only** into sandboxes: knowledge, skills. In a multi-host future both sides mount the same shared subtree. |

`DATA_DIR` layout (created on boot):

```text
data/
├── app.db          # application DB: users, spaces, sessions, skills,
│                   # templates, knowledge, MCP connectors, feedback, …
├── noeta.db        # engine storage: EventLog + ContentStore + Dispatcher
├── workspaces/     # one directory per session (bind-mounted at /workspace
│                   # in sandbox mode; the files panel reads it)
├── memories/       # one long-term memory pool per space
└── shared/         # SHARED_DATA_DIR default location
    ├── knowledge/       # materialized knowledge sources
    ├── builtin-skills/  # admin-managed platform skills
    └── space-skills/    # per-space uploaded skills
```

Both databases are SQLite files; Postgres is a documented future option, not
wired in v1 (the platform is single-process single-instance).

## LLM gateway

| Key | Default | Purpose |
| --- | --- | --- |
| `LLM_PROVIDER` | `auto` | `auto` \| `openai` \| `mock`. **`auto` resolves to `openai` when `LLM_BASE_URL` and `LLM_API_KEY` are both set, otherwise to the offline `mock`** (deterministic FakeLLM demo script — the zero-credential mode). `openai` without credentials fails at boot. |
| `LLM_BASE_URL` | *(empty)* | Primary gateway root — any **OpenAI-Responses-compatible** endpoint; the provider appends `/responses`. Auth uses the `api-key` header. |
| `LLM_API_KEY` | *(empty)* | Primary gateway credential. |
| `SECONDARY_LLM_BASE_URL` | *(empty)* | Optional second gateway (same Responses protocol, `Authorization: Bearer` auth). |
| `SECONDARY_LLM_API_KEY` | *(empty)* | Its credential. Both must be set to count as configured; the secondary only stacks **on top of** an active primary — it never stands alone. |
| `MODELS_CONFIG` | `models.json` | Path to the model-menu file (below). |
| `LLM_REQUEST_TIMEOUT` | `300.0` | Per-request timeout (seconds). |
| `LLM_MAX_TOKENS` | `8192` | Output-token cap. |
| `TITLE_MODEL` | `gpt-5.4-2026-03-05` | Model for async session-title generation (reasoning disabled; no titles under the mock provider). |

### `models.json`

Defines the model menu users pick from (`GET /api/v1/models`). Per entry:
`id`, `label`, `default` (one entry), `efforts` (reasoning-effort levels),
`default_effort`, plus backend-only fields: `gateway` (`"openai"` = primary,
`"secondary"` = routed to the secondary gateway via `RoutingProvider`) and
`context_window` / `max_output_tokens` (register a spec so context
compaction works for models the SDK catalog does not know). A missing or
unparseable file degrades to a single fallback model with a warning — the
backend never crashes over model config.

## Auth and admin

| Key | Default | Purpose |
| --- | --- | --- |
| `DEV_LOGIN_ENABLED` | `true` | The dev-login reference provider: any username, signed cookie. Also a **dynamic config** key — hot-switchable from the admin console (the DB override wins over this static value). |
| `SESSION_SECRET` | dev placeholder | Signs the session cookie. **Change it in any real deployment.** |
| `SESSION_COOKIE_NAME` | `noeta_session` | Cookie name. |
| `SESSION_COOKIE_SECURE` | `false` | Set `true` behind HTTPS. |
| `ADMIN_USERS` | *(empty)* | Comma-separated usernames that get `is_admin` and the admin console. Empty = nobody; admin endpoints answer 404 for everyone. Under dev-login anybody can log in as an allowlisted name — real deployments plug an identity provider into the `AuthProvider` seam (`noeta/agent/auth/provider.py`). |

## Sandbox

One Docker container per session; the standard fs/shell tool side effects
route into it through the ExecEnv seam. Disabled = **pure conversation
mode**: no containers, shell execution off, no file surface.

| Key | Default | Purpose |
| --- | --- | --- |
| `SANDBOX_ENABLED` | `false` | The whole switch. Requires a local Docker daemon. |
| `SANDBOX_IMAGE` | `ghcr.io/agent-infra/sandbox:latest` | The stock AIO Sandbox image; build your own on top for extra in-sandbox tooling. |
| `SANDBOX_MEMORY` | `2g` | Per-container memory cap. |
| `SANDBOX_CPUS` | `2` | Per-container CPU cap. |
| `SANDBOX_API_KEY_ENV` | `SANDBOX_API_KEY` | **Name** of the env var holding the container API key — read at provisioning, injected into the container and ExecEnv auth, never recorded. Unset var = the container runs without auth (local dev only). |
| `SANDBOX_PREVIEW_PORT` | `0` | Dedicated reverse-proxy port for the live Browser/Terminal/Code panels. Deliberately a **separate origin** from the main port (the panel iframes run `allow-same-origin`; container content must not share the cookie/API origin). `0` = ephemeral (discovered via `GET /sessions/{id}/preview`); pin it when firewalls need a fixed port. |
| `SANDBOX_IDLE_STOP_HOURS` | `1.0` | Idle level 1: `docker stop` — memory/CPU return to the host; the container and its disk stay, and resuming re-attaches in seconds. |
| `SANDBOX_IDLE_REMOVE_HOURS` | `24.0` | Idle level 2: `docker rm` — reclaims disk too; after this only a fresh session works. Keep it much longer than stop. `0`/negative disables a level; both disabled = no reaper. |
| `SANDBOX_IDLE_CHECK_INTERVAL_HOURS` | `0.1` | Reaper poll interval (one-minute floor). |

## Agent tool switches

Global switches for the agent tool surface (temporary until per-space
switches land); all default **off**:

| Key | Default | Purpose |
| --- | --- | --- |
| `MEMORY_TOOLS_ENABLED` | `false` | `memory_write/read/search/archive` + auto-recall + consolidation. |
| `COLLAB_TOOLS_ENABLED` | `false` | The collaboration tools (`channel_read_*`, `board_*`) behind the channels/board preview surface. |
| `SUBAGENT_ENABLED` | `false` | `spawn_subagent` delegation (explorer / web specialist). |

## Memory consolidation

Only effective when `MEMORY_TOOLS_ENABLED` is on.

| Key | Default | Purpose |
| --- | --- | --- |
| `MEMORY_CONSOLIDATION` | `true` | Background curation at turn boundaries, debounced per space; the consolidation agent gets only the memory tool surface and can archive, never delete. |
| `MEMORY_CONSOLIDATION_DEBOUNCE_HOURS` | `24.0` | Minimum hours between passes (marker file in the space's memory directory). |

## Observability

| Key | Default | Purpose |
| --- | --- | --- |
| `OTLP_ENDPOINT` | *(empty)* | OTLP trace export: the **full** OTLP/HTTP traces URL (e.g. `http://localhost:4318/v1/traces`). Empty = off. Export is **opt-in through this key only** — the ambient OTel-standard `OTEL_EXPORTER_OTLP_ENDPOINT` is deliberately **not** honored as an enable switch (an operator injecting it for other apps must not silently start noeta exporting). |
| `OTLP_HEADERS` | *(empty)* | Extra headers on every export request (hosted-collector auth), OTel form `k=v,k2=v2` with percent-encoded values. Falls back to the ambient `OTEL_EXPORTER_OTLP_HEADERS` when unset. Headers apply **only** when `OTLP_ENDPOINT` is set — they never enable anything by themselves. |

## Worker pool

| Key | Default | Purpose |
| --- | --- | --- |
| `AGENT_NUM_WORKERS` | `4` | Resident `WorkerLoop` threads in the embedded noeta Client: N workers drive different sessions' turns concurrently (turns within one session stay serialized by the dispatcher lease). Set `1` to degrade to a single worker. |

## Dynamic config

A small allowlist of settings is hot-reloadable at runtime through the admin
console (`GET/PUT /api/v1/admin/config`): a DB override wins over the static
`.env` value, and only settings re-read on every use qualify. Registered
today: `dev_login_enabled`. Source:
`apps/noeta-agent/noeta/agent/config_registry.py`.
