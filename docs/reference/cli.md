# The noeta command

Every `noeta` subcommand and flag, read from `noeta/cli/main.py`, `noeta/cli/headless.py`,
`noeta/cli/plugin_command.py` and `noeta/cli/mcp_command.py`, and the slash commands of the
terminal UI. For a guided tour see [The terminal UI and -p](../guides/terminal.md).

<Diagram name="ref-ways-in" caption="Front ends run on a LocalApp in their own process (only their own tasks) or a RemoteApp against a server; serve and worker run every task in the store." />

## Overview

```text
noeta [flags] [prompt]          the terminal UI; with -p (or a stream-json format) headless
noeta acp [--server URL]        Agent Client Protocol on stdio, for editors
noeta serve [flags]             the HTTP / WebSocket API, with workers unless --no-worker
noeta worker [flags]            run tasks from the store until stopped
noeta trust                     trust the current folder
noeta plugins                   list every plugin and where it comes from
noeta plugin add|configure|list|remove
noeta mcp add|add-json|list|get|remove|login|logout
```

A word is taken as a subcommand when it is the first word that is not an option. Before it,
only the shared flags below (`--model`, `--effort`, `--permission-mode`, `--provider`,
`--base-url`, `--store`, `--cwd`, `--system-prompt`, `--append-system-prompt`) may appear;
any other flag means the terminal UI or `-p`.

## Shared flags

Taken by the terminal UI, `-p`, `acp`, `serve` and `worker`.

| Flag | Meaning |
|---|---|
| `--model MODEL` | the model id or an alias (`opus`, `sonnet`, `haiku`, `fable`); else `NOETA_MODEL`, `config.toml`, a settings file's `model` |
| `--effort {low,medium,high,xhigh,max}` | how hard the model thinks; else `NOETA_EFFORT`, `config.toml` `effort` |
| `--permission-mode MODE` | the starting permission mode: `default`, `acceptEdits`, `plan`, `auto`, `dontAsk`, `bypassPermissions`, or `manual` (an alias of `default`). Default: `auto`, or the settings files' `permissions.defaultMode` |
| `--provider {anthropic,openai_compat,openai_responses}` | the default provider's family; else `NOETA_PROVIDER`, `config.toml` `provider` (which may also name a `[providers.<name>]` table) |
| `--base-url URL` | the default provider's base URL; else `NOETA_BASE_URL` |
| `--store URL` | `sqlite:///path` or `postgres://…`; else `NOETA_STORE`, `config.toml` `store`, then `~/.noeta/tasks.db` |
| `--cwd PATH` | the working directory (default: the current one). With `--server`, a directory on the server (default: the server's own) |
| `--system-prompt TEXT` | replace the main agent's system prompt (the prompt `system/main`) |
| `--append-system-prompt TEXT` | add text after the main agent's system prompt (`system/main.append`) |

No model from any of these is an error (`noeta: no model configured`), except for a front end
attached with `--server`, which uses the server's.

## The terminal UI: `noeta`

`noeta` with no prompt and no headless flag opens the terminal UI (the `noeta-tui` package,
found through the `noeta.frontends` entry point; noeta prints the install line when it is
missing). In a folder not yet trusted it first asks whether to trust it.

| Flag | Meaning |
|---|---|
| `-c`, `--continue` | open the newest conversation started in this folder |
| `-r`, `--resume [ID]` | open task `ID`; without an id, pick from a list |
| `--server URL` | drive the tasks of a `noeta serve` server instead of running them here (token in `NOETA_SERVER_TOKEN`). New tasks run on the server's model and directory unless `--model` / `--cwd` name others. `--plugin-dir` is refused with it |
| `--agent NAME` | the agent new tasks start with (default: the settings files' `agent`, else `main`) |
| `--plugin-dir PATH` | load a plugin file, plugin folder or folder of them for this run only (repeatable) |
| `-v`, `--version` | print the version and exit |

Flags of the next section that only make sense headless are accepted and have no effect in
the UI.

## Headless: `noeta -p`

Claude Code's print mode and its stream-json protocol (`noeta/host/stream_json.py`). Headless
mode is on with `-p`, with `--input-format stream-json`, or with any `--output-format`.

| Flag | Meaning |
|---|---|
| `-p`, `--print` | run the prompt without the UI and print the result |
| `prompt` | the prompt; with `-p` and none given, read from stdin |
| `--output-format {text,json,stream-json}` | `text` (default): the answer; `json`: one result object; `stream-json`: one JSON message per line (needs `--verbose`) |
| `--input-format {text,stream-json}` | `stream-json`: read user messages and control requests from stdin, one JSON object per line (needs `--output-format stream-json`) |
| `--include-partial-messages` | also emit `stream_event` messages as the model streams |
| `--replay-user-messages` | echo the user messages read from stdin |
| `--verbose` | required by `--output-format stream-json`, as in Claude Code |
| `--max-turns N` | at most N model calls in a turn; a turn that would make another fails (result subtype `error_max_turns`) |
| `--max-budget-usd X` | stop once the task tree has cost X dollars (result subtype `error_max_budget_usd`) |
| `--json-schema SCHEMA` | a JSON Schema (root `"type": "object"`) the answer must match; the model answers through a `StructuredOutput` tool and the result carries `structured_output` |
| `--allowedTools`, `--allowed-tools RULES…` | allow rules, e.g. `"Bash(git *) Edit"`; spaces or commas separate rules, spaces inside parentheses are kept |
| `--disallowedTools`, `--disallowed-tools RULES…` | deny rules; a bare tool name also removes the tool |
| `--tools NAMES…` | the tools offered: `default` for all, `""` for none, else only these |
| `--add-dir DIR…` | more directories the tools may read and write |
| `--permission-mode MODE` | as above |
| `--dangerously-skip-permissions` | start in `bypassPermissions` |
| `--permission-prompt-tool TOOL` | `stdio`: the client answers permission prompts (`can_use_tool` control requests); an MCP tool name `mcp__<server>__<tool>`: that tool answers them. Without it anything that would ask is refused and the turn goes on |
| `--permission-prompts {host,none}` | `none` ignores `--permission-prompt-tool` (default `host`) |
| `-c`, `--continue` | continue the newest task started in this folder with this agent |
| `-r`, `--resume ID` | continue task `ID` |
| `--fork-session` | with `--resume` / `--continue`: go on in a new task started from that conversation |
| `--session-id ID` | the id the new task gets (Claude Code's flag name; in noeta it is a task id) |
| `--agent NAME` | the agent to run (default: the settings files' `agent`, else `main`) |
| `--system-prompt-file PATH` | replace the main agent's system prompt with the file's text |
| `--append-system-prompt-file PATH` | append the file's text to it |
| `--settings FILE_OR_JSON` | one more settings layer, above the project's files and below the managed one |
| `--setting-sources user,project,local` | which settings files are read; the managed one always is. Leaving out `user` also skips `~/.noeta/agents`; leaving out `project` skips the project's agents and its `.noeta/mcp.json` |
| `--mcp-config FILE_OR_JSON…` | MCP servers: JSON files or strings holding `mcpServers`; a later one wins a name |
| `--strict-mcp-config` | use only the `--mcp-config` servers, no settings files or project servers |
| `--agents JSON` | sub-agents as a JSON object: name → `{description, prompt, …}` (agent-file fields); they come last and win a name |
| `--plugin-dir PATH` | as above |
| `-d`, `--debug [FILTER]` | accepted; noeta does not use it |

`--server` is refused with `-p`: `-p` always runs its task in this process.

**Accepted and ignored,** so a Claude Code command line runs unchanged; each one used is named
on stderr: `--fallback-model`, `--betas`, `--max-thinking-tokens`, `--thinking`,
`--thinking-display`, `--plugin-url`, `--task-budget`, `--debug-file`, `--name`,
`--autocompact`, `--file`, `--system-prompt-snapshot` (these take a value),
`--include-hook-events`, `--no-session-persistence`, `--session-mirror`, `--bare`,
`--allow-dangerously-skip-permissions`, `--exclude-dynamic-system-prompt-sections`,
`--forward-subagent-text`, `--disable-slash-commands`.

**Exit code:** 0 when the result is a success, 1 on an error result, a bad flag, an empty
prompt, or no task to continue. With `--input-format stream-json` the code is 0 when stdin
ends.

**The result** (`json`, and the last `stream-json` line of each prompt) has Claude Code's
fields: `type: "result"`, `subtype` (`success`, `error_during_execution`,
`error_max_budget_usd`, …), `is_error`, `result`, `duration_ms`, `duration_api_ms`,
`num_turns`, `total_cost_usd`, `usage`, `modelUsage`, `permission_denials`, `stop_reason`,
`session_id` (the root task's id) and, with a schema, `structured_output`.

**Control requests** a stream-json client may send: `initialize`, `interrupt`,
`set_permission_mode`, `set_model`, `set_max_thinking_tokens`, `rewind_files`,
`get_context_usage`, `get_session_cost`, `list_models`, `mcp_status`.

## `noeta acp`

Serves the Agent Client Protocol on stdin/stdout, for Zed and other ACP editors (the `acp`
extra). Locally it runs a `LocalApp` with the editor's MCP servers added; each editor session
is a task.

| Flag | Meaning |
|---|---|
| `--server URL` | drive a `noeta serve` server's tasks instead (token in `NOETA_SERVER_TOKEN`); the model is the server's unless `--model` names one, and the editor's local directory is not sent |
| `--cwd PATH` | as the shared flag (taken after `acp` too; the later wins) |
| `--model MODEL` | as the shared flag (taken after `acp` too) |

Editor setup is in [Web, desktop and editors](../guides/front-ends.md).

## `noeta serve`

The client API over HTTP and WebSocket (the `server` extra), plus workers and channels in the
same process unless `--no-worker`. Endpoints are in [HTTP / WebSocket API](./http-api.md).

| Flag | Default | Meaning |
|---|---|---|
| `--host HOST` | `127.0.0.1` | the address to bind |
| `--port PORT` | `8642` | the port |
| `--no-worker` | off | API only; workers run elsewhere (`noeta worker`) |
| `--no-token` | off | serve without a bearer token; refused unless `--host` is a loopback address |
| `--allow-host NAME` | none | a `Host` header name the server answers (repeatable). On a loopback bind the loopback names are always allowed; on another bind without it, `Host` is not checked |
| `--allow-origin ORIGIN` | none | a web origin (`https://host:port`) whose pages may call the API (repeatable) |
| `--allow-bypass-permissions` | off | let requests start or switch tasks into `bypassPermissions` |
| `--allow-root PATH` | none | a directory besides the server's own that tasks may run in (repeatable) |
| `--web DIR` | none | also serve this directory's files (the built web app, `web/dist`) at the other paths |

The token is `NOETA_SERVER_TOKEN`, else one made on the first start and printed that once;
`~/.noeta/server-token` keeps only its SHA-256. Delete that file and restart for a new one.
Without `--allow-bypass-permissions` the permissions plugin itself refuses
`bypassPermissions`, however it is asked for.

## `noeta worker`

Claims and runs tasks from the store until SIGINT or SIGTERM, with the channels
`config.toml` enables. Use it with a Postgres store to run tasks on many machines.

| Flag | Default | Meaning |
|---|---|---|
| `--queue NAME` | `default` | a queue to claim from (repeatable) |
| `--concurrency N` | `8` | tasks run at once |
| `--lease SECONDS` | `30` | how long a claim lasts without renewal; a dead worker's tasks wait about this long before another takes them |
| `--snapshot-every EVENTS` | `100` | write a task's fold snapshot at most every this many events; `0` never |
| `--allow-bypass-permissions` | off | let tasks run in `bypassPermissions` |

On the way out, running tasks are handed back at once; tasks keeping a claim for a background
job are released and told the job is lost at their next turn.

## `noeta trust`

Adds the current folder (or `--cwd`) to `~/.noeta/trusted.json`. From then on its
`.noeta/settings*.json`, agents, skills, prompt overrides and plugins load
([Configuration](../start/configuration.md#trust)).

## `noeta plugins`

Lists every plugin with where it comes from (a package, a plugin file), the channels
(marked `(channel)`), and the Claude Code plugin folders found (with their version). Plugin
files that failed to load go to stderr with the reason, and, for a missing module, the
install line that adds it.

## `noeta plugin`

Installs plugins into `~/.noeta/plugins/`, pinned, running nothing from them.

| Command | Meaning |
|---|---|
| `noeta plugin add SOURCE` | install from `<git-url>@<commit>` (https or ssh, a full commit), or copy a path |
| `noeta plugin add --marketplace MARKETPLACE NAME` | install `NAME` from a Claude Code `marketplace.json` (a path or `<git-url>@<commit>`); the marketplace is only read |
| `  --commit SHA` | the commit to install when the marketplace entry pins none |
| `  --config KEY=VALUE` | a Claude Code plugin's `userConfig` value (repeatable); the others are asked for on a terminal |
| `noeta plugin configure NAME [--config KEY=VALUE]` | set a Claude Code plugin's `userConfig` values again |
| `noeta plugin list` | the plugins installed with `noeta plugin add` |
| `noeta plugin remove NAME` | remove one |

`userConfig` values are kept in `~/.noeta/settings.json` `pluginConfigs`, sensitive ones in the
OS keyring. See [Write a plugin](../guides/plugins.md).

## `noeta mcp`

Manages MCP servers as `claude mcp` does, in noeta's places.

| Command | Meaning |
|---|---|
| `noeta mcp add NAME COMMAND_OR_URL [ARGS…]` | add a server |
| `  -s`, `--scope {local,project,user}` | `local` (default): `.noeta/settings.local.json`; `project`: `.noeta/mcp.json`, shared through the repository; `user`: `~/.noeta/settings.json` |
| `  -t`, `--transport {stdio,sse,http,ws}` | default `stdio` |
| `  -e`, `--env KEY=value…` | environment variables for a stdio server |
| `  -H`, `--header "Name: value"…` | headers for a remote server |
| `  --client-id ID` | OAuth client id for an HTTP / SSE server |
| `  --client-secret` | ask for the OAuth client secret (or set `NOETA_MCP_CLIENT_SECRET`) |
| `  --callback-port N` | a fixed port for the OAuth redirect |
| `noeta mcp add-json [-s SCOPE] [--client-secret] NAME JSON` | add a server from its JSON entry |
| `noeta mcp list` | list the configured servers and check each |
| `noeta mcp get NAME` | show one |
| `noeta mcp remove [-s SCOPE] NAME` | remove one |
| `noeta mcp login [--no-browser] NAME` | sign in (OAuth); `--no-browser` prints the address instead of opening it |
| `noeta mcp logout NAME` | forget the sign-in |

Client secrets and sign-in tokens are kept under `~/.noeta/mcp-auth/`, never in a settings
file. See [MCP servers](../guides/mcp.md).

## Slash commands

The terminal UI's own commands (`tui/src/noeta_tui/slash.py`):

| Command | Does |
|---|---|
| `/help` | show help and the commands |
| `/status` | the version, model, task, directory and settings |
| `/model [MODEL]` | pick or set the model, from the next request on; alone, lists every provider's models |
| `/effort [LEVEL]` | pick or set the effort (`low` … `max`) |
| `/context` | what fills the next request, as a grid |
| `/cost` | tokens and cost of this task and its sub-agents |
| `/memory` | list the instruction and memory files; open one in `$EDITOR` |
| `/config [edit user\|project\|local]` | show the settings; edit a settings file |
| `/permissions` | the permission mode and rules |
| `/agents` | the agents this front end runs |
| `/mcp` | MCP servers: connected, failed or waiting for a sign-in |
| `/hooks` | the hooks configured (read-only) |
| `/rewind` | go back to before a prompt: conversation, code or both (also Esc Esc) |
| `/clear`, `/new` | start a new task |
| `/resume [N]` | pick a task to resume |
| `/stop` | stop the turn (also Esc) |
| `/reload-plugins` | load the plugin and agent files again, from the next turn on |
| `/exit` | quit |

Commands the agent's plugins offer (`noeta/core/commands.py` lists and expands them; every
front end shows them, and `POST /tasks/{id}/messages` runs them):

| Command | Plugin | Does |
|---|---|---|
| `/compact [focus]` | `compaction` | summarize the conversation now, optionally focused |
| `/plan [request]` | `plan_mode` | switch to plan mode; with a request, plan it |
| `/init` | `markdown_commands` | write or improve the project's `NOETA.md` |
| `/review [PR \| branch \| paths]` | `markdown_commands` | review changes for bugs |
| `/loop [interval] [prompt]` | `schedule` | run a prompt or command on a recurring interval |
| `/hooks` | `hooks` | the hooks configured |
| `/mcp` | `mcp` | MCP server status |
| `/mcp__<server>__<prompt>` | `mcp` | an MCP server's prompt |
| `/<skill>`, `/<plugin>:<skill>` | `skills` | run a skill by name |
| `/<command>` | `markdown_commands` | a `commands/*.md` file from `~/.noeta/commands`, `.noeta/commands` or a Claude Code plugin |

Keys in the terminal UI: Esc stops the turn; Esc Esc clears the input, or on an empty one
opens `/rewind`; Shift+Tab cycles the permission mode; Ctrl+C clears the input (twice on an
empty one quits); `!` at the start of the input runs a shell command (bash mode).
