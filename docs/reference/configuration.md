# Configuration

Noeta Agent (`python -m noeta.agent`) is configured through environment
variables and an optional JSON config file. Env vars take precedence over
the file; the file takes precedence over built-in defaults.

## Configuration sources

Precedence (low → high):

1. **Dataclass defaults** — safe offline defaults (`stub` provider, `dry_run` writes, in-memory storage).
2. **`NOETA_AGENT_CONFIG` file** — a JSON object whose keys override defaults (see [below](#json-config-file-fields)).
3. **`NOETA_AGENT_*` environment variables** — highest precedence (see [below](#environment-variables)).

## Environment variables

| Variable | Type | Default | Purpose |
| --- | --- | --- | --- |
| `NOETA_AGENT_CONFIG` | path | *(none)* | Path to a JSON config file. See [JSON config file fields](#json-config-file-fields). |
| `NOETA_AGENT_HOST` | string | `127.0.0.1` | Interface the HTTP server binds to. **The server is unauthenticated — keep it on localhost.** Binding `0.0.0.0` exposes full engine control (and the preview gateway's proxy) to the network; put a reverse proxy with auth in front if you must. |
| `NOETA_AGENT_PORT` | int | `8765` | Port the HTTP server listens on. `0` = OS-assigned. |
| `NOETA_AGENT_WORKSPACE` | path | `$PWD` | Default workspace directory (the agent's file root). |
| `NOETA_AGENT_WORKSPACES_FILE` | path | `~/.noeta/workspaces.json` | Workspace (project) registry JSON store. |
| `NOETA_AGENT_MCP_FILE` | path | `~/.noeta/mcp_servers.json` | MCP server connector registry JSON. |
| `NOETA_AGENT_STORAGE` | URL | *(none)* | Durable storage for EventLog + ContentStore + Dispatcher: a SQLite file path or a `postgresql://` DSN. Unset = in-memory (no persistence). Legacy `NOETA_AGENT_SQLITE` still accepted. |
| `NOETA_AGENT_PROVIDER` | string | `stub` | Provider adapter: `stub` (offline), `openai`, `openai-responses`, `anthropic`. |
| `NOETA_AGENT_MODEL` | string | *(none)* | Model identifier served by the configured provider. |
| `NOETA_AGENT_MODELS` | string | *(none)* | Comma-separated list of selectable models (enables per-turn model switching in the UI). |
| `NOETA_AGENT_API_KEY` | string | *(none)* | Provider API key. Required for real providers. |
| `NOETA_AGENT_BASE_URL` | URL | *(none)* | Provider base URL. Required for `openai` and `openai-responses`. |
| `NOETA_AGENT_API_VERSION` | string | *(none)* | API version query param (used by `openai-responses`). |
| `NOETA_AGENT_MAX_TOKENS` | int | *(none)* | Output token cap forwarded to requests that carry none. |
| `NOETA_AGENT_WRITE_MODE` | string | `dry_run` | Filesystem write policy: `dry_run` (stages a diff, safe default) or `apply` (performs real writes). |
| `NOETA_AGENT_WORKFLOW_ENABLED` | bool | `false` | Host kill-switch for the `run_workflow` control tool. |
| `NOETA_AGENT_BACKGROUND_DRIVE` | bool | `true` | Drive turns asynchronously on a background thread (command endpoints return `202`). |
| `NOETA_AGENT_OTLP_ENDPOINT` | URL | *(none)* | OTLP trace export: the **full** OTLP/HTTP traces URL (e.g. `http://localhost:4318/v1/traces`). Task / tool / LLM execution is exported as spans to any OTLP collector (Jaeger, OpenTelemetry Collector, …). Unset = export off. The OTel-standard `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` (used as-is) and `OTEL_EXPORTER_OTLP_ENDPOINT` (`/v1/traces` appended) are honored as fallbacks; `OTEL_EXPORTER_OTLP_HEADERS` (`k=v,k2=v2`) supplies headers. |
| `NOETA_WEB_SEARCH_API_KEY` | string | *(none)* | Enables the `web_search` built-in tool. Without this key, the tool is not mounted. |

### Boolean parsing

`*_ENABLED` / `*_DRIVE` booleans accept `1`, `true`, `yes`, `on` (case-insensitive) as true; everything else is false.

## JSON config file fields

Pass the path via `NOETA_AGENT_CONFIG=/path/to/config.json`. The file must
hold a single JSON object. All keys are optional.

| Key | Type | Default | Purpose |
| --- | --- | --- | --- |
| `host` | string | `127.0.0.1` | Bind interface. |
| `port` | int | `8765` | Bind port. |
| `workspace_dir` | string | `$PWD` | Default workspace directory. |
| `workspaces_registry_path` | string | `~/.noeta/workspaces.json` | Workspace registry store. |
| `mcp_servers_registry_path` | string | `~/.noeta/mcp_servers.json` | MCP connector registry. |
| `storage_url` | string | *(none)* | Durable storage URL (see env var above). Legacy key `sqlite_path` still accepted. |
| `provider_id` | string | `stub` | Provider adapter id. |
| `model` | string | *(none)* | Model id. |
| `models` | list[string] | `[]` | Selectable model list. |
| `api_key` | string | *(none)* | Provider API key. |
| `base_url` | string | *(none)* | Provider base URL. |
| `api_version` | string | *(none)* | API version. |
| `max_tokens` | int | *(none)* | Output token cap. |
| `default_headers` | object[string→string] | `{}` | Extra HTTP headers for provider requests (e.g. gateway `X-TT-LOGID`). File-only. |
| `write_mode` | string | `dry_run` | Write policy. |
| `workflow_enabled` | bool | `false` | Workflow tool gate. |
| `background_drive` | bool | `true` | Async turn driving. |
| `otlp_endpoint` | string | *(none)* | OTLP trace export URL (see env var above). |
| `otlp_headers` | object[string→string] | `{}` | Extra headers on every OTLP export request (hosted-collector auth). The export carries the audit allowlist projection only — no goals, tool arguments, or message bodies. |

### Example

```json
{
  "provider_id": "openai",
  "model": "gpt-4o-mini",
  "base_url": "https://api.openai.com/v1",
  "api_key": "sk-…",
  "workspace_dir": ".",
  "storage_url": ":memory:",
  "host": "127.0.0.1",
  "port": 8765
}
```

## Provider adapters

| `provider_id` | Notes |
| --- | --- |
| `stub` | *(default)* Offline deterministic two-turn LLM double. No API key, no network. Use this for install + wiring smoke tests. |
| `openai` | OpenAI-compatible `/chat/completions` endpoint. Requires `api_key` + `base_url`. |
| `openai-responses` | OpenAI Responses API. Requires `api_key` + `base_url` (the full responses endpoint). Supports vision via `image_resolver`. Consumes `api_version` + `max_tokens`. |
| `anthropic` | Anthropic Messages API. Requires `api_key`. Optional `base_url`, `max_tokens`, `default_headers`. Supports vision. |

## Write & shell safety

- **Writes** are `dry_run` by default: `edit` / `write` / `apply_patch` emit a unified-diff artifact without touching bytes. Set `write_mode: apply` (or `NOETA_AGENT_WRITE_MODE=apply`) for real writes.
- **`shell_run`** is gated by `ShellMode.ALLOWLIST` by default: only allowlisted argv patterns pass (`git status`, `git diff`, `pytest`, `uv run pytest`, `npm test`, `pnpm test`). Shell metacharacters are rejected before tokenization. This is **path-containment + an allowlist, not a process sandbox** — `shell_run` spawns external programs in the trusted workspace.

## Source

The authoritative config parsing lives in
`noeta.agent.backend.lifecycle.BackendConfig.from_env`
(`apps/noeta-agent/noeta/agent/backend/lifecycle.py`). Provider construction
is in `build_provider()` in the same module.
