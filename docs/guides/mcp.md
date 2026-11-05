# MCP servers

This page shows how to connect Model Context Protocol (MCP) servers to noeta, what the model
sees of them, and what happens when a server is slow, down or needs a sign-in. The formats are
Claude Code's (`mcpServers`, `claude mcp …`); the places are noeta's own.

The `mcp` plugin (`noeta/plugins/mcp/`) does all of it. The `noeta` command turns it on with the
settings files and the project's `.noeta/mcp.json`; an SDK host that builds agents with
`default_agents` keeps it off until it passes `plugins={"mcp": {...}}` (`noeta/agents/__init__.py`
`OFF_UNLESS_SET`).

<Diagram name="guide2-mcp" />

## Add a server

`noeta mcp` works like `claude mcp` (`noeta/cli/mcp_command.py`):

```sh
# a local process (stdio): the command and its arguments, never a shell line
noeta mcp add files -- npx -y @modelcontextprotocol/server-filesystem /srv/data

# a remote server over Streamable HTTP, with a header
noeta mcp add -t http -H 'Authorization: Bearer ${GH_TOKEN}' github https://api.example.com/mcp

# the whole entry as JSON
noeta mcp add-json -s user search '{"type":"sse","url":"https://search.example.com/sse"}'

noeta mcp list            # every server, its scope, and whether it connects now
noeta mcp get github      # one server in full
noeta mcp remove github   # add -s SCOPE when the name is in several
```

`-e KEY=VALUE` (repeatable) sets a stdio server's environment, `-H "Name: value"` a remote
server's headers. Quote `${VAR}` with single quotes so the file keeps the reference and the
secret is read from the environment when the server starts, not written into the file. A server name may hold any characters; the alias used in tool names replaces
everything outside `[A-Za-z0-9_-]` with `_` (`noeta/plugins/mcp/settings.py` `alias_for`).

### Scopes: where the entry is written

| `-s` | File | Who sees it |
|---|---|---|
| `local` (default) | `<project>/.noeta/settings.local.json` `mcpServers` | you, in this project; read once the project is trusted |
| `project` | `<project>/.noeta/mcp.json` | everyone who clones the repository, but only after consent (below) |
| `user` | `~/.noeta/settings.json` `mcpServers` | you, in every project |

noeta never reads `~/.claude.json`, `.mcp.json` at a project's root, or anything under
`.claude/` ([ADR-004](https://github.com/initxy/noeta/blob/main/docs/adr/004-noeta-reads-only-its-own-config-places.md)).
Copy a Claude Code entry into one of the files above; the format is the same.

### A project's servers need trust and consent

A repository you clone must not start processes by itself. A server in `.noeta/mcp.json`
starts only when both hold:

1. the project is trusted (`noeta trust` in it), and
2. one of the project's own settings files (`.noeta/settings.json` or
   `.noeta/settings.local.json`) consents with Claude Code's keys:
   `"enableAllProjectMcpServers": true`, or the server's name in `"enabledMcpjsonServers"`.

A name in `"disabledMcpjsonServers"` in any settings file keeps it off. Consent in your user
settings does not count: it would follow the name into every repository that reuses it
(`noeta/plugins/mcp/settings.py`). `noeta mcp list` shows such a server as
`pending approval (not started)`, and a worker logs which servers wait for consent.

```json
// <project>/.noeta/settings.local.json
{ "enabledMcpjsonServers": ["files"] }
```

## The file format

Every place uses Claude Code's `mcpServers` object:

```json
{
  "mcpServers": {
    "files": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "${HOME}/data"],
      "env": { "LOG_LEVEL": "${LOG_LEVEL:-info}" }
    },
    "github": {
      "type": "http",
      "url": "https://api.example.com/mcp",
      "headers": { "Authorization": "Bearer ${GH_TOKEN}" },
      "timeout": 120000,
      "alwaysLoad": true
    }
  }
}
```

| Key | Meaning |
|---|---|
| `type` | `stdio` (the default with `command`), `http` (also `streamable-http`; the default with `url`), `sse` (the older HTTP+SSE pair) or `ws` (WebSocket) |
| `command`, `args` | a stdio server's program and arguments, run directly (no shell) |
| `env` | variables laid over the environment a stdio server inherits |
| `url` | a remote server's endpoint |
| `headers` | sent with every request to a remote server |
| `headersHelper` | a shell command that prints a JSON object of headers when a connection opens (at most 10 s) |
| `oauth` | for `http` / `sse`: `clientId`, `callbackPort`, `authServerMetadataUrl` (https only), `scopes` |
| `timeout` | one tool call's limit in milliseconds (under 1000 is ignored, as in Claude Code) |
| `alwaysLoad` | `true`: send this server's tool schemas from the first request; `false`: defer every tool, whatever the server's hints say |
| `bareElicitationCapability` | `true` for servers that reject the elicitation `form` / `url` modes |

`${VAR}` and `${VAR:-default}` are filled from the environment in `command`, `args`, `env`,
`url`, `headers` and `headersHelper`. A server entry that cannot be read is logged and skipped;
the others still load.

### Other sources

- **An agent file's `mcpServers`** (Claude Code's field): a bare name refers to a server
  configured elsewhere; a `name: {config}` entry adds a server for that agent's tasks only
  (`noeta/agents/agent_files.py` `plugin_overlays`).
- **A Claude Code plugin's `.mcp.json`** (or its manifest's `mcpServers`) installed in
  `~/.noeta/plugins/`. From a project's `.noeta/plugins/` its servers never start; noeta tells
  you to copy them into `.noeta/mcp.json`, where consent applies (`noeta/cli/claude_plugins.py`).
- **`noeta -p --mcp-config FILE_OR_JSON`** (repeatable) adds servers for one headless run;
  `--strict-mcp-config` uses only those (`noeta/cli/headless.py` `mcp_arguments`).
- **An editor over ACP** names its servers per conversation; they stay in memory and never
  enter the log (`noeta/host/acp.py` `EditorServers`).
- **Host code**: `Mcp(servers=...)` takes a list of `McpServer`, a mapping in the `mcpServers`
  shape, or a function of the task returning either.

When two sources name the same alias, the first found wins: the agent file's own servers, then
the ones given in code or by flags and plugins, then the project's `.noeta/mcp.json`, then the
settings files in the order `~/.noeta/settings.json`, `.noeta/settings.json`,
`.noeta/settings.local.json`, managed settings (`noeta/plugins/mcp/extension.py` `_resolve`,
`settings.py` `read_servers`). Keep each name in one place.

## What the model sees

### Tool names

A server tool is offered as `mcp__<alias>__<tool>`: the tool's own name with every character
outside `[A-Za-z0-9_-]` made `_`, case kept, nothing cut. Two names of one server that come out
the same get `_` plus 8 hex digits of the raw name's SHA-256. A full name over 128 characters
is not offered (the server's log says so, and a shorter server name fixes it). Two servers that
produce the same tool name are a clash: the second is reported down until one alias is renamed
(`noeta/plugins/mcp/config.py`).

noeta once lower-cased aliases and cut names to 64 characters. Permission rules, hook matchers
and `--disallowedTools` lists written that way still match, but only to narrow: an old name
works in a deny or ask rule, never in an allow rule (`noeta/core/mcp_names.py`).

### Deferred loading

MCP tools are deferred by default ([ADR-017](https://github.com/initxy/noeta/blob/main/docs/adr/017-deferred-tool-loading-is-a-plugin.md)):
the request names them, and the model calls `ToolSearch` to load the schemas it needs before it
calls one. This keeps a server with fifty tools from costing fifty schemas on every request.
To send schemas up front, set `"alwaysLoad": true` on the server, or let the server mark single
tools with `_meta["anthropic/alwaysLoad"]: true`. `_meta["anthropic/searchHint"]` adds words
`ToolSearch` matches. A tool whose `_meta.ui.visibility` leaves out `"model"` is never offered.

A tool's description is cut to 4,096 characters while it is only named and to 16,384 once
loaded; `NOETA_MAX_MCP_DESCRIPTION_LENGTH` replaces both. A server's `instructions` get the
first cap and are sent as a system note when the server joins or changes them.

### The tool set is fixed for a turn

When a turn opens, every server is asked for `tools/list` and the result is recorded in the
task's log as one `listed` record, only when it changed. The tools of that turn come from the
record alone, so the request prefix (and the provider's cache) stays the same for the whole
turn. A server that announces `list_changed` is listed again after the call that brought the
notice, and the new set is offered from the next step. Because the set is in the log, a task
resumed on another worker offers the same tools and reconnects on its first call.

The record holds aliases and schemas only. Commands, URLs, environment and headers stay in
memory on the worker and never reach the log.

### Resources, prompts and the `/mcp` command

- The main agent also gets `ListMcpResourcesTool`, `ReadMcpResourceTool` and
  `ReadMcpResourceDirTool` while any server is configured. Sub-agents get the server tools only,
  as in Claude Code.
- Each server prompt is a slash command, `/mcp__<alias>__<prompt> arg1 arg2…` (the last argument
  takes the rest of the line).
- `/mcp` lists every server: connected with its tool count, waiting for a sign-in, or failed.

## Limits and timeouts

| What | Default | Change it with |
|---|---|---|
| `initialize`, lists, prompts, resources | 30 s | `NOETA_MCP_TIMEOUT` (milliseconds) |
| one `tools/call` | none in practice (100,000 s) | the server's `timeout` (ms), else `NOETA_MCP_TOOL_TIMEOUT` (ms); kept between 1 s and 2,147,483 s |
| a tool result | 25,000 tokens | `NOETA_MAX_MCP_OUTPUT_TOKENS` |
| a tool description | 4,096 chars named, 16,384 loaded | `NOETA_MAX_MCP_DESCRIPTION_LENGTH` |
| a tool name | 128 chars | (fixed: the Messages API's limit) |
| an idle connection | closed after 30 min unused | `McpPool(idle_ttl=…)` in host code |

A text result over the budget is saved under the task's spill directory and the model is told
where it is and how to read it; a result with images keeps what fits and cuts the text
(`noeta/plugins/mcp/results.py`). `NOETA_TERMINAL_MCP_TOOLS` (comma-separated full names) lists
tools whose successful result ends the turn, as Claude Code's `CLAUDE_CODE_TERMINAL_MCP_TOOLS`
does.

## When things go wrong

- **A server that cannot be listed** at the start of a turn is left out of that turn. The log
  gets a `server_down` record (front ends show it) and the model gets a note naming the servers
  that could not connect, so it does not take the missing tools for a missing feature. The next
  turn lists it again; a `ToolSearch` that finds nothing while a server is down also lists the
  servers once more in that turn.
- **A stdio server that died, or an HTTP endpoint that restarted**, costs one reconnect: a
  reused connection that fails on a list or read is dropped and opened again once
  (`noeta/plugins/mcp/pool.py` `use`). A connection that hits EOF, a broken pipe or an oversized
  line is retired, and the next use starts the server again.
- **A tool call that fails** is not retried: the model gets the error as the call's result.
  Every call carries its idempotency key in `_meta` (`noeta/idempotencyKey`) for servers that
  can drop a repeat.
- **A worker that crashes mid-call** leaves the call "outcome unknown" for the model, never
  re-run: noeta cannot know whether the server acted.
- **A server that asks the person something** (MCP elicitation) parks the call. The question is
  shown like any other; the worker keeps the task's claim while it waits because the open
  call lives in that process. If that worker dies, the next worker answers the call
  "outcome unknown" at once. A second question in the same call is declined.
- **Shutdown** closes a stdio server's stdin, then terminates and finally kills its whole
  process group, so what it started goes with it. Its stderr is discarded.

## Signing in (OAuth) and secrets

A remote `http` or `sse` server that answers 401 needs a sign-in. Two ways:

```sh
noeta mcp login github              # opens the browser, waits for the redirect
noeta mcp login --no-browser github # prints the URL; paste back the address you land on
noeta mcp logout github
```

Or inside a task: the server's tools are replaced by `mcp__<alias>__authenticate` and
`mcp__<alias>__complete_authentication`. The model calls the first and gives you a URL; when
the browser cannot reach the redirect, you paste the final address and the model passes it to
the second. The server's tools replace the two once the sign-in succeeds. A sign-in nobody
finishes within 5 minutes ends (`noeta/plugins/mcp/oauth.py`).

noeta follows the MCP authorization spec: discovery of the authorization server, dynamic client
registration unless `oauth.clientId` is set, authorization code with PKCE. The redirect goes to
`http://localhost:<port>/callback`, the port taken from `NOETA_MCP_OAUTH_CALLBACK_PORT`, else
`oauth.callbackPort`, else a free one. Tokens are kept per server under `~/.noeta/mcp-auth`
(directory 0700, files 0600) and renewed before they expire.

A client secret is never written to a settings file: `noeta mcp add --client-id ID
--client-secret` asks for it (or reads `NOETA_MCP_CLIENT_SECRET`) and keeps it with the
sign-in. Headers, environment and tokens never enter the task log.

A stdio server starts with noeta's environment minus noeta's own secrets (`NOETA_API_KEY`,
`NOETA_STORE`, `NOETA_SERVER_TOKEN`, `NOETA_MCP_CLIENT_SECRET`, `NOETA_WEB_SEARCH_API_KEY`, and
any variable noeta's own config hands to a plugin or channel as `{env:NAME}`), plus its `env` (`noeta/toolkit/local_env.py`).

## MCP on a server with many workers

- Each worker works out a task's servers again every time it picks the task up, from its own
  `~/.noeta` and the task's working directory. Ship the same settings to every worker, or a task
  that moves finds a different set.
- A stdio server runs on the worker machine itself, not in the task's exec env or sandbox. Its
  files are the worker's.
- Connections are per worker process and shared by every task naming the same server in the
  same scope. A stateful server (a browser, a login) would then carry one person's state into
  another's task; host code gives each person a scope with `Mcp(scope=…)` (a string or a
  function of the task).
- Sign-in tokens live in each machine's `~/.noeta/mcp-auth`. A sign-in started in a task runs
  in the worker that started it.

## Related

- [Permissions and hooks](./permissions.md): rules such as `mcp__github__*` and hooks matching
  MCP tools.
- [Built-in tools](../reference/tools.md): `ToolSearch` and the resource tools.
- [Built-in plugins](../reference/plugins.md): the `mcp` plugin's arguments.
- [Run it as a server](./server.md).
