# Extending noeta

Everything noeta does beyond running the task log is a plugin, and the built-in ones are written
exactly as yours would be. This page says where your code hooks in, how to write and ship each
kind of plugin, and how that compares with pi, DeepSeek Harness (dsh) and Claude Code.

**noeta sends nothing anywhere except to the model provider you configure.** No telemetry, no
log upload, no update check. A plugin you install can of course make its own requests.

## Where your code hooks in

| You want to… | Write | Where it lives |
|---|---|---|
| give the model a tool | `Extension.tools()` returning a `Tool`, or `function_tool(fn)` on a typed function | `noeta/core/tool.py`, `noeta/toolkit/function_tool.py` |
| add a tool built from the other plugins' tools, or keep some out of a request | `Extension.tool_changes(view, offered)` → `ToolChanges` (as `noeta/plugins/tool_search` does for deferred tools) | `noeta/core/extension.py` |
| add system prompt text | `Extension.context()` → `ContextPart` (inside the one cached block) | `noeta/core/extension.py` |
| tell the model something each turn | `Extension.reminders()` → `Reminder` (recorded, so a resumed task resends it) | `noeta/core/extension.py` |
| allow, deny, rewrite or ask about a tool call | `before_tool` → `Allow` / `Deny` / `Rewrite` / `Ask`; `resolve_ask` answers an ask; `after_tool` changes the result | `noeta/core/extension.py` |
| run shell / HTTP hooks in Claude Code's format | settings `hooks`, or a Claude Code plugin's `hooks/hooks.json` | `noeta/plugins/hooks` |
| add a slash command | `commands()` / `run_command()`, or `commands/*.md` in a Claude Code plugin | `noeta/core/commands.py` |
| keep state across restarts and machines | `view.record(name, kind, data)` + `reduce()` (never a new event type) | `noeta/core/extension.py` |
| wake a task later | `view.set_timer(name, at, text)` | `noeta/core/records.py` |
| ask the person something | `ask(...)` (the call parks, the task holds no worker) | `noeta/core/questions.py` |
| add an agent or sub-agent | an agent markdown file (Claude Code's format plus `plugins`) | `noeta/agents/agent_files.py` |
| add skills | `SKILL.md` folders in `~/.noeta/skills` or a plugin's `skills/` | `noeta/plugins/skills` |
| connect MCP servers | `.noeta/mcp.json`, settings `mcpServers`, a plugin's `.mcp.json` | `noeta/plugins/mcp` |
| let a chat app or webhook in | a `Channel` (`run(client, stop)`), posting with `deliver()` | `noeta/core/channel.py` |
| send replies back out | `on_event` in an `Extension` inside the task | `examples/chat-bridge` |
| a new model API | a provider in the `noeta.providers` entry-point group | `noeta/backends/providers` |
| run commands somewhere else (a container) | an exec env in `noeta.execenvs` | `noeta/backends/execenvs` |
| store tasks elsewhere | a store in `noeta.stores`, checked by `noeta.testing.store_contract` | `noeta/backends/stores` |
| a new front end or protocol | the client API (`Client`, `RemoteClient`, HTTP); never a second API | `noeta/core/client.py`, `noeta/host` |

## Writing a plugin

### A plugin file (two steps)

1. Write `~/.noeta/plugins/<name>.py` (or a directory with `__init__.py`; a trusted project's
   `.noeta/plugins/` works too) defining an `Extension` subclass with a `name`. Set
   `add_to = ("main",)` (or `("*",)`) and it joins those agents by itself.
2. Start a front end, or type `/reload-plugins` in a running one.

Arguments come from `~/.noeta/config.toml` `[plugins.<name>]`; `enabled = false` turns a plugin
off everywhere; `[agents.<name>] add_plugins` adds one without `add_to`. A secret is written as
`"{env:NAME}"` or `"{keyring:service/user}"` and resolved only when the plugin is made.

The four examples are each one file, a short README and a test `make check` runs:

- [`examples/run-tests`](../examples/run-tests) — a tool: runs the test command where the task's
  commands run, folds the last result from the log, reminds the model once after an edit.
- [`examples/chat-bridge`](../examples/chat-bridge) — a channel: a chat thread becomes a task,
  requests carry a shared secret, senders are allowlisted, replies relayed from the task.
- [`examples/chat-approvals`](../examples/chat-approvals) — a second channel next to chat-bridge:
  an approval the task waits on is posted to its thread, and the chat app's Allow or Deny press
  answers it.
- [`examples/house-style`](../examples/house-style) — prompt and reminder text: an output style
  captured when the task starts, so a resumed task sends identical bytes.

### A package

Register the class in your `pyproject.toml` under `[project.entry-points."noeta.plugins"]`
(`noeta.channels`, `noeta.providers`, `noeta.execenvs`, `noeta.stores` for the others) and
install it next to noeta — the built-ins do exactly this. [`examples/hello-plugin`](../examples/hello-plugin)
is one. An installed package wins a name clash with a file, and its `add_to` is ignored: a
dependency never changes your agents by being installed.

### A Claude Code plugin

A Claude Code plugin directory (`.claude-plugin/plugin.json`, `skills/`, `agents/`,
`commands/`, `hooks/hooks.json`, `.mcp.json`) put in `~/.noeta/plugins/` loads through the
plugins that read those formats; commands are `/<plugin>:<command>` and, as skills do, run
their `!` lines through the permission checks and pre-approve their `allowed-tools`.
`${CLAUDE_PLUGIN_ROOT}` and `${CLAUDE_PLUGIN_DATA}` (`~/.noeta/plugin-data/<name>`) are
substituted. From a project, its MCP
servers never start: noeta says to copy them into `.noeta/mcp.json`, which needs consent. Monitors, `bin/`, LSP servers, themes and
output styles are not loaded (reasons [below](#what-noeta-does-not-build-and-why)). noeta never
reads `~/.claude` or `.claude/`.

### Installing

```sh
noeta plugin add https://github.com/you/your-plugin@<40-hex commit>   # https or ssh, a full commit
noeta plugin add ./my_plugin.py                                       # copied
noeta plugin add --marketplace ./team-marketplace formatter           # a Claude Code marketplace.json
noeta plugin list
noeta plugin remove your-plugin
```

Installing runs nothing from the plugin: no install scripts, git hooks or dependency installs.
A plugin that imports a package noeta's environment lacks is listed as broken (`noeta plugins`,
`/reload-plugins`) with the `uv tool install` / `pip install` line that adds it there.
A marketplace is only read, to find where a plugin comes from; an entry fetched from a
repository installs at its `sha`, or at `--commit <sha>` when it pins none (a marketplace that
is itself a repository needs `<url>@<commit>`).

## Durable and multi-machine

- **Pinned plugin set.** A task records which plugins it runs with (name, package version or
  file hash, a hash of the arguments) when it starts. A worker that lacks one never runs the task
  without it: it lets its lease run out, so a worker that has the plugin picks the task up, and
  after three such claims in a row with nothing new in the log it fails the task with
  `plugin_missing`. A changed set is recorded again.
- **Reload.** `/reload-plugins` re-imports plugin files and remakes the agents. A turn in
  progress finishes on the old code; each task runs the new code from its next turn. On servers
  the reload is a rolling restart: stop one `noeta worker` at a time (its running tasks go back
  to the store and continue on another worker from their logs), install, start it again. Finish
  within a few leases: a task pinned to a new plugin that keeps landing on a worker without it
  fails after three of its leases.
  Different code on two workers for the same task is visible in its log and never silent.
- **Channels** run in every `noeta worker` / `noeta serve` that enables them; inbound posts are
  idempotent, so several replicas are safe, and replies leave from the task's one writer.
- **Policy.** `/etc/noeta/config.toml` `[policy] allowed_plugins = [...]` /
  `blocked_plugins = [...]` win over every user and project setting.

## Compared with pi, dsh and Claude Code plugins

Sources: pi-mono 9ae3d88 (coding-agent 1.4.0), deepseek-harness f3803c96, Claude Code docs as of
2026-10-28 (2.1.3xx).

| | noeta | pi | dsh | Claude Code |
|---|---|---|---|---|
| Extension points | one `Extension` interface: tools, prompt parts, recorded reminders, gates (deny > rewrite > ask > allow, rewrites gated again), ask resolvers, commands, durable state, timers, compaction; plus providers, exec envs, stores, channels | ~40 events, tools, commands, providers, TUI components; can rewrite the raw provider request | everything is a Cordis plugin: tools, prompt sections, waterfalls, sub-agent providers, sandboxes, web UI nodes | declarative skills, commands, agents, hooks, MCP servers, LSP, output styles, monitors, channels; in-process "mods" for UI |
| Steps to write a plugin | 2: one file with `add_to`, then start or `/reload-plugins`; 1 for a skill (a `SKILL.md` folder in `~/.noeta/skills`) | 2: one file, `/reload` | 3 (source checkout + patch file) to 5 (bundle, profile) | 3 (MCP server + `.mcp.json` + `--plugin-dir`) for a tool, 1 for a skill |
| Hot reload | at a turn boundary (`/reload-plugins`; rolling restart on servers), recorded in the log | `/reload`, old call frames finish on old code | HMR of every registration (web profile) | `/reload-plugins` for hooks, MCP, LSP |
| Distribution | Python packages; `noeta plugin add <git>@<commit>`; Claude Code plugin directories and marketplaces (read only) | npm or git packages, gallery | npm or git bundles into profiles, opt-in indexes | git marketplaces pinned by version or SHA, auto-update |
| Config and trust | one plugin-setting table at every level, secret references, project code only after `noeta trust`, project MCP servers only with consent, machine allow/block policy | per-extension JSON, project trust | per-plugin config schemas, explicit installs | `userConfig` with keychain secrets, workspace trust, per-server consent, managed allow/block |
| Isolation of plugin code | in-process; side effects of tools go through the task's exec env (a container if you choose), MCP servers and command hooks run out of process | in-process | in-process; sandbox for spawned processes | MCP, hooks, LSP out of process; mods in process behind a mediated API |
| Valid on many machines | yes: state folded from the task log, plugin set pinned per task, channels idempotent across replicas, any worker resumes any task | no (one local process) | no (one host process) | no (local session; channels process-local) |
| Sends data home by default | nothing | install telemetry on | session-log upload on for the official API | telemetry mod where analytics are on |

Where noeta is not strictly ahead, and why:

- **UI contributions** (pi, dsh, Claude Code mods draw panels). noeta is a server-side harness and
  front ends import no plugin; a plugin sends `notify(text, kind=..., data=...)` (structured data with a
  plain-text fallback) and a front end renders it.
- **Hot reload granularity.** pi and dsh swap code inside a running turn; noeta reloads between
  turns because a request must stay a pure function of the task log.
- **Isolation.** No one sandboxes in-process plugin code; Claude Code's mods at least mediate
  their API. noeta's isolation options are the exec env, MCP servers and command hooks.
- **Distribution.** No hosted gallery or auto-update: Python packaging, pinned git installs and
  Claude Code marketplaces cover it without running an index or moving code under a person.
- **Claude Code plugin parts noeta does not load** (monitors, `bin/`, LSP servers, output
  styles, themes): each has its reason and the noeta way in the table below.
- **Interactive configuration.** Claude Code prompts for `userConfig`; noeta reads
  `config.toml` with secret references (environment or OS keyring) instead of a dialog.

## What noeta does not build, and why

| Not built | Why |
|---|---|
| A sandbox for in-process plugin code | Not achievable in CPython; isolation exists where it is real — the exec env, MCP servers, command hooks, the machine policy. |
| Swapping code inside a running turn | Breaks "the request is a pure function of the log"; a reload between turns gives the same loop. |
| Hooks on the raw provider request | Makes requests unreproducible from the log; `before_model` works on the `ModelRequest`, providers are pluggable for wire changes. |
| UI drawn by plugins | Front ends import no plugin; `notify(text, kind=..., data=...)` carries what a front end may render. |
| A hosted marketplace, gallery or auto-update | An operations product and a supply-chain liability; packages, pinned git and Claude Code marketplaces suffice. |
| Agents installing plugins into their own harness | Model output would install code into the harness; writes to `.noeta/` ask the person. |
| Any default upload of logs or telemetry | Never. An opt-in exporter can be an `on_event` plugin the operator installs. |
| Monitors and `bin/` from plugins | A per-session background process does not fit a task that holds no worker while waiting (use a channel or a timer); `bin/` silently widens what Bash runs. |
| LSP servers from plugins | A language server is a long-lived process holding one checkout's state on one machine, while a task holds no worker between turns and resumes on any worker. To give the model a checker's findings, run the linter or type checker as a tool in the task's exec env (the shape of `examples/run-tests`), or put a language server behind an MCP server. |
| Output styles from plugins | noeta has no style switch of its own: a style is prompt text a plugin adds, recorded when the task starts so a resumed task sends the same bytes. `examples/house-style` does this for a style file in Claude Code's format. |
| Themes | Colors belong to a front end, and front ends import no plugin; each front end has its own settings. |
| A second interface for a channel's outbound side | Outbound is an ordinary `Extension` (`on_event`); only the listener is new. |
