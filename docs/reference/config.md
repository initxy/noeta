# Configuration keys

Every key of `~/.noeta/config.toml`, every provider table key, the settings-file keys noeta
reads, how plugin settings combine, and every environment variable noeta reads. Where each
comes from and which wins is explained in [Configuration](../start/configuration.md).

## `config.toml`

`~/.noeta/config.toml` (`$NOETA_HOME/config.toml`) is read by every `noeta` command
(`noeta/cli/config.py`). A flag or an environment variable wins over the top-level keys.

### Top-level keys

| Key | Type | Default | Meaning | Overridden by |
|---|---|---|---|---|
| `model` | string | none (required somewhere) | the model new tasks run, an id or an alias | `--model`, `NOETA_MODEL` |
| `provider` | string | `anthropic` | the default provider: a family (`anthropic`, `openai_compat`, `openai_responses`, or a provider package's) or the name of a `[providers.<name>]` table | `--provider`, `NOETA_PROVIDER` |
| `base_url` | string | the family's | the default provider's base URL: the host for `anthropic`, `…/v1` for `openai_compat`, the whole `…/v1/responses` URL for `openai_responses` | `--base-url`, `NOETA_BASE_URL` |
| `api_key` | string | none | the default provider's key, as a literal (no `{env:…}` here); without one, `anthropic` reads `ANTHROPIC_API_KEY` and `openai_compat` reads `OPENAI_API_KEY`; `openai_responses` reads no variable and needs one | `NOETA_API_KEY` |
| `extra_headers` | table of strings | none | headers sent with every request of the default provider | — |
| `store` | string | `sqlite:///~/.noeta/tasks.db` | `sqlite:///path` or `postgres://…` | `--store`, `NOETA_STORE` |
| `effort` | string | the model's | `low`, `medium`, `high`, `xhigh` or `max`, for the tasks the terminal UI and `-p` start | `--effort`, `NOETA_EFFORT` |
| `models_file` | path | none | a JSON file describing more models (below) | `NOETA_MODELS_FILE` |
| `list_models` | bool | `true` | read the default provider's `/models` listing for models the catalog does not know | — |
| `prompt_cache_ttl` | `"5m"` or `"1h"` | the API's (`5m`) | `anthropic` only: the one-hour prompt cache, billed higher per write | — |
| `prompts` | table | none | prompt overrides for every task this front end starts, by prompt name (`"system/main.append" = "…"`) | `--system-prompt`, `--append-system-prompt` |

Tables: `[providers.<name>]`, `[plugins.<name>]`, `[agents.<name>]`, `[channels.<name>]`
(below). Other top-level keys are ignored.

### `[providers.<name>]`

More providers besides the default one. Each model id goes to the provider whose table names
it, else to the first provider whose `/models` listing has it (the default first), else to
the default. A task records the route its model took, so every worker sends it to the same
provider ([Providers and model routing](../how-it-works/models.md)).

| Key | Required | Meaning |
|---|---|---|
| `family` | yes | `anthropic`, `openai_compat`, `openai_responses`, or a provider package's |
| `base_url` | no | as the top-level `base_url`, for this family |
| `api_key` | no | a literal, `{env:NAME}` or `{keyring:service/user}`; without one, `anthropic` and `openai_compat` fall back to their usual environment variable |
| `headers` | no | a table of headers; values may be secret references |
| `models` | no | model ids this provider serves, as strings or rows `{id, context_window, max_output_tokens}`; a row with both limits teaches the catalog a model it does not know |
| `list_models` | no (default `true`) | read this provider's `/models` for more models and their windows (kept a day in `~/.noeta/cache/models`) |
| `prompt_cache_ttl` | no | `anthropic` only: `"1h"` or `"5m"` |

Any other key is an error. The name `default` is taken (it names the top-level provider). One
model named for two providers is an error. `provider = "<table name>"` at the top level makes
a table the default instead.

### Models file

`models_file` / `NOETA_MODELS_FILE` names a JSON file of models the shipped catalog does not
know, so they run at their real limits:

```json
{"models": [
  {"id": "my-gateway/model-x", "context_window": 1000000, "max_output_tokens": 65536,
   "supports_vision": false, "family": "anthropic", "provider": "deepseek"}
]}
```

`id`, `context_window` and `max_output_tokens` are needed for a row to count; `supports_vision`,
`family` (the wire family) and `efforts` (marks a reasoning model) are optional; `provider`
routes the model to a configured provider by name. A bare list of rows works too.

A model's limits come from noeta's catalog first, then a models file or table rows, then its
provider's listing, else 128,000 tokens of window and 16,384 of output with a warning.

### `[plugins.<name>]`

A plugin's arguments, by its registry name, for every agent that runs it:

```toml
[plugins.shell]
background = true
[plugins.compaction]
compaction_model = "claude-haiku-5-5"
[plugins.web]
enabled = false                  # off for every agent
[plugins.my_plugin]              # a third-party plugin's arguments
endpoint = "https://…"
token = "{env:MY_PLUGIN_TOKEN}"
```

Each built-in plugin's arguments are in [Built-in plugins](./plugins.md).

### `[agents.<name>]`

Takes one key, `add_plugins`: plugins appended to that agent after its file's own, as names or
`{name = {arguments}}` tables. `[agents."*"]` adds to every agent. A plugin the agent already
has is not added twice.

```toml
[agents.main]
add_plugins = ["my_plugin", { hooks = {} }]
[agents."*"]
add_plugins = ["audit_log"]
```

### `[channels.<name>]`

A channel's arguments: a long-running listener for an outside conversation (a chat app, a
webhook), run by `noeta worker` and `noeta serve`. `enabled = false` leaves one out.

```toml
[channels.chat_bridge]
token = "{env:CHAT_TOKEN}"
```

### `[policy]` (managed only)

Read only from the machine's directory (`/etc/noeta/config.toml`, `/Library/Application
Support/Noeta/config.toml`, `C:\Program Files\Noeta\config.toml`):

| Key | Meaning |
|---|---|
| `allowed_plugins` | when set, only these plugins load among those noeta does not ship |
| `blocked_plugins` | these never load, built-in or not |

## Plugin settings

A plugin setting is what a host says about one plugin, by name: its arguments (a table, where
`enabled = false` switches it off), a ready `Extension` instance (in Python), or on/off. The
same shape is used at every level (`noeta/agents/agent_files.py`):

1. the arguments the `noeta` command derives from your places (the memory home, the settings
   files for hooks and MCP, the skills folders, permission rules);
2. the settings Claude Code plugin folders bring;
3. `config.toml` `[plugins.<name>]`;
4. what the front end itself passes (`-p`'s `--mcp-config`, `--allowedTools` …).

Later levels lay over earlier ones key by key; an instance or an on/off replaces. An agent
file's own arguments for a plugin (`plugins: [{shell: {background: true}}]`) are laid over
the host's. The built-in agents keep `memory`, `mcp` and `repetition_guard` off until a
setting names them; the `noeta` command sets the first two.

Secrets in arguments are written as references (`{env:NAME}`, `{keyring:service/user}`),
resolved only when the plugin is made; config files and task logs hold only the reference.

A task records the plugins it runs with (name, source, version or file hash, a hash of the
arguments). A worker that lacks one never runs the task without it
([The plugin system](../how-it-works/plugins.md)).

## Settings files

The JSON settings files (`~/.noeta/settings.json`, `.noeta/settings.json`,
`.noeta/settings.local.json`, `--settings`, the managed `managed-settings.json`) use Claude
Code's format. The keys noeta acts on (`noeta/cli/settings_files.py` and the plugins that read
the files):

| Key | Read by | Meaning |
|---|---|---|
| `env` | the `noeta` command | environment variables set in noeta's process before anything else is read |
| `model` | the `noeta` command | the model, when no flag, variable or `config.toml` names one |
| `agent` | front ends | the agent new tasks start with |
| `outputStyle` | — | read; output styles are not applied |
| `language` | `environment` | the language the model answers in |
| `attribution.commit`, `attribution.pr` | `environment` | the trailer lines for commits and pull requests (`""`: none); `includeCoAuthoredBy: false` empties both |
| `permissions.allow`, `.deny`, `.ask` | `permissions` | rule lists, Claude Code's `Tool(spec)` strings; they add up across files |
| `permissions.defaultMode` | `permissions` | the starting permission mode |
| `permissions.additionalDirectories` | `permissions`, `fs`, `skills` | more directories the tools may use |
| `permissions.disableBypassPermissionsMode` | `permissions` | `"disable"`: no `bypassPermissions` |
| `useAutoModeDuringPlan` | `plan_mode` | `false` in any file: plan mode does not use the auto mode classifier |
| `sandbox` | the local exec env | Claude Code's sandbox object: `enabled`, `failIfUnavailable`, `filesystem`, `network`, `excludedCommands`, … ([Where commands run](../guides/sandbox.md)) |
| `hooks` | `hooks` | Claude Code's hooks ([Permissions and hooks](../guides/permissions.md)) |
| `mcpServers` | `mcp` | MCP servers (user and local scope) |
| `enabledMcpjsonServers`, `disabledMcpjsonServers`, `enableAllProjectMcpServers` | `mcp` | consent for a project's `.noeta/mcp.json` servers ([MCP servers](../guides/mcp.md)) |
| `skillListingBudgetFraction`, `skillListingMaxDescChars` | `skills` | the skill listing's budget and per-skill description cap |
| `pluginConfigs` | Claude Code plugins | `userConfig` values written by `noeta plugin add` / `configure` |

## Environment variables

Variables noeta reads (grepped across `noeta/` and `tui/`):

| Variable | Read by | Meaning |
|---|---|---|
| `NOETA_HOME` | everything | noeta's home (default `~/.noeta`) |
| `NOETA_MODEL` | `noeta` | the model |
| `NOETA_PROVIDER` | `noeta` | the default provider family or table name |
| `NOETA_BASE_URL` | `noeta` | the default provider's base URL |
| `NOETA_API_KEY` | `noeta` | the default provider's key (never passed to commands noeta starts) |
| `ANTHROPIC_API_KEY` | `anthropic` provider | its key when none is configured |
| `OPENAI_API_KEY` | `openai_compat` provider | its key when none is configured |
| `NOETA_STORE` | `noeta` | the store URL (never passed to commands) |
| `NOETA_EFFORT` | `noeta` | the effort |
| `NOETA_MODELS_FILE` | `noeta` | a models file |
| `NOETA_SERVER_TOKEN` | `serve`, `--server`, `acp --server` | the server's bearer token; a client sends it (never passed to commands) |
| `NOETA_AUTO_COMPACT_WINDOW` | `compaction` | cap the window auto-compaction derives its threshold from (Claude Code's `CLAUDE_CODE_AUTO_COMPACT_WINDOW`) |
| `NOETA_AUTOCOMPACT_PCT_OVERRIDE` | `compaction` | compact at this percentage of the effective window when that is lower (Claude Code's `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE`) |
| `NOETA_SLASH_COMMAND_TOOL_CHAR_BUDGET` | `skills` | the skill listing's budget in characters (Claude Code's `SLASH_COMMAND_TOOL_CHAR_BUDGET`) |
| `NOETA_MCP_TIMEOUT` | `mcp` | milliseconds for every MCP request but a tool call (default 30 s) |
| `NOETA_MCP_TOOL_TIMEOUT` | `mcp` | milliseconds for one MCP tool call when the server sets none (default: in effect no limit) |
| `NOETA_MAX_MCP_OUTPUT_TOKENS` | `mcp` | an MCP result's size before it is saved to a file instead (default 25,000) |
| `NOETA_MAX_MCP_DESCRIPTION_LENGTH` | `mcp` | the cap on MCP tool descriptions and server instructions (default 4,096 characters, 16,384 for a deferred tool once loaded; the variable sets both) |
| `NOETA_TERMINAL_MCP_TOOLS` | `mcp` | comma-separated full MCP tool names whose result ends the turn |
| `NOETA_MCP_OAUTH_CALLBACK_PORT` | `mcp` | the OAuth redirect port |
| `NOETA_MCP_CLIENT_SECRET` | `noeta mcp add` / `add-json` with `--client-secret` | the OAuth client secret, instead of typing it (never passed to commands) |
| `NOETA_WEB_SEARCH_API_KEY` | `web` | the Tavily key that turns on `WebSearch` (never passed to commands) |
| `TZ` | terminal UI, `-p`, `acp` | the person's time zone, sent with each new task as its `timezone` (else the zone `/etc/localtime` or `/etc/timezone` names) |
| `VISUAL`, `EDITOR` | terminal UI | the editor `/memory` and `/config edit` open |

Variables noeta sets for what it starts:

| Variable | Set for | Value |
|---|---|---|
| `NOETA_PROJECT_DIR`, `CLAUDE_PROJECT_DIR` | hook commands | the task's working directory |
| `NOETA_MCP_SERVER_NAME`, `NOETA_MCP_SERVER_URL` | an MCP server's `headersHelper` | the server's name and URL |
| `CLAUDE_PLUGIN_OPTION_<KEY>` | a Claude Code plugin's hooks | its `userConfig` values |

In skill and command bodies, `${NOETA_SKILL_DIR}` / `${CLAUDE_SKILL_DIR}`,
`${NOETA_PROJECT_DIR}` / `${CLAUDE_PROJECT_DIR}`, `${NOETA_EFFORT}` / `${CLAUDE_EFFORT}` and
`${CLAUDE_SESSION_ID}` (the root task's id) are substituted. In a Claude Code plugin's files,
`${CLAUDE_PLUGIN_ROOT}`, `${CLAUDE_PLUGIN_DATA}` (`~/.noeta/plugin-data/<name>`) and
`${user_config.KEY}` are too.

Every variable a `{env:NAME}` reference in noeta's config names is withheld from commands,
hooks and MCP servers too, as are `PGPASSWORD`, `PGPASSFILE`, `PGSERVICE` and
`PGSERVICEFILE` when the store is on Postgres (`noeta/toolkit/local_env.py`). Everything else
is inherited, as in Claude Code.

The desktop app reads `NOETA_DESKTOP_NOETA` (the `noeta` binary), `NOETA_DESKTOP_WEB` (the web
app's files) and `NOETA_DESKTOP_USER_DATA`; see [Web, desktop and editors](../guides/front-ends.md).
