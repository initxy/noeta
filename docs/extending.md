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
| run shell / HTTP hooks in Claude Code's format | settings `hooks`, or a Claude Code plugin's `hooks/hooks.json` (InstructionsLoaded included) | `noeta/plugins/hooks` |
| add a slash command | `commands()` / `run_command()`, or `commands/*.md` in a Claude Code plugin | `noeta/core/commands.py` |
| keep state across restarts and machines | `view.record(name, kind, data)` + `reduce()` (never a new event type) | `noeta/core/extension.py` |
| fold records written under another name (a plugin split out of another; records front ends read by name) | `folds = ("other",)`: those records reach `reduce` too | `noeta/core/extension.py` |
| wake a task later | `view.set_timer(name, at, text)` | `noeta/core/records.py` |
| ask the person something | `ask(...)` (the call parks, the task holds no worker) | `noeta/core/questions.py` |
| add an agent or sub-agent | an agent markdown file (Claude Code's format plus `plugins`) | `noeta/agents/agent_files.py` |
| add skills | `SKILL.md` folders in `~/.noeta/skills`, a project's `.noeta/skills`, or a plugin's `skills/`; `*.md` files in `.noeta/commands` | `noeta/plugins/skills` |
| connect MCP servers | `.noeta/mcp.json`, settings `mcpServers`, a plugin's `.mcp.json` | `noeta/plugins/mcp` |
| let a chat app or webhook in | a `Channel` (`run(client, stop)`), posting with `deliver()` | `noeta/core/channel.py` |
| send replies back out | `on_event` in an `Extension` inside the task | `examples/chat-bridge` |
| a new model API | a provider in the `noeta.providers` entry-point group | `noeta/backends/providers` |
| run commands somewhere else (a container) | an exec env in `noeta.execenvs` | `noeta/backends/execenvs` |
| store tasks elsewhere | a store in `noeta.stores`, checked by `noeta.testing.store_contract` | `noeta/backends/stores` |
| a new front end or protocol | the client API (`Client`, `RemoteClient`, HTTP); never a second API | `noeta/core/client.py`, `noeta/host` |
| change each model request, or end the turn without one | `before_model` → `ModelRequest`; raise `TurnDone` (no call) or `RequestStale` (build it again) | `noeta/core/extension.py` |
| capture something once, when a task starts | `on_start` (recorded, so a resumed task sends the same) | `noeta/core/extension.py` |
| pass config to sub-agents, or choose what a fork carries | `inherit`, `continue_state` | `noeta/core/extension.py` |
| start a child task, or post to an inbox | `view.spawn`, `view.post` / `view.enqueue` | `noeta/core/extension.py` |
| use another plugin's tool | `view.find_tool(name)` | `noeta/core/extension.py` |
| call the model from a hook or a tool | `view.complete`, `ctx.complete` (retried, not a step) | `noeta/core/extension.py`, `noeta/core/tool.py` |
| show a running call's progress | `ctx.progress(text)`: live only, never recorded | `noeta/core/tool.py` |
| survive a crash in the middle of a call | `Tool.recover` (by `ctx.idempotency_key`) or `retry_safe`; `ctx.hold()` for work that lives in one process | `noeta/core/tool.py` |
| offer a tool only when the model asks for it | `deferred = True` (loaded through `ToolSearch`) | `noeta/plugins/tool_search` |
| keep text across a compaction, or take part in one | `resident` / `lead_resident`; `before_compact`; `on_context_overflow(view, request)` | `noeta/core/extension.py` |
| show a result to front ends as data | `ToolOutput.display` → `Display(kind, data, fallback)` | `noeta/core/types.py` |
| go on after a reply with no text and no tool call (the turn ends otherwise) | `on_empty_reply(view, response)` → the message to go on with, or `None` (as `noeta/plugins/empty_reply.py` does) | `noeta/core/extension.py` |
| block a prompt, or keep a turn going (as UserPromptSubmit and Stop hooks do) | `reminders` + `view.erase_messages` + raise `TurnFailed`; `on_event` on `ModelReplied` posting `Say(when="now")` | `noeta/plugins/hooks/extension.py` |

## Writing a plugin

### A plugin file (two steps)

1. Write `~/.noeta/plugins/<name>.py` (or a directory with `__init__.py`; a trusted project's
   `.noeta/plugins/` works too) defining an `Extension` subclass with a `name`. Set
   `add_to = ("main",)` (or `("*",)`) and it joins those agents by itself.
2. Start a front end, or type `/reload-plugins` in a running one.

To try a file kept elsewhere for one run, `noeta --plugin-dir ./my_plugin.py` (also with
`-p`; a plugin directory or a Claude Code plugin works too, and the flag repeats). A plugin
file that joins no agent is named in a notice.

Arguments come from `~/.noeta/config.toml` `[plugins.<name>]`; `enabled = false` turns a plugin
off everywhere; `[agents.<name>] add_plugins` adds one without `add_to`. A secret is written as
`"{env:NAME}"` or `"{keyring:service/user}"` and resolved only when the plugin is made; a
variable named this way never reaches a command, hook or MCP server noeta starts.

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

A plugin that replaces a built-in one front ends read keeps its name and records:
`permissions` (`mode`, `modes`: the mode shown and cycled) and `checkpoints` (`snapshot`:
rewind). `plan_mode` writes its switches as `permissions` `mode` / `plan_exit` records and folds
them back (`folds`), so plan mode works with the permissions plugin off (it then asks about
every call that is neither read-only nor `safe_while_planning`, the plan file's write aside),
and the permissions plugin offers no `plan` mode with `plan_mode` off. A replacement
`permissions` plugin therefore never writes these record kinds with another meaning: `mode`,
`plan_exit`, `modes` (front ends, `plan_mode`), `plan_name`, `noted`, `compacted`, `plan_file`,
`plan_file_sent` (`plan_mode`, from logs before the split), `step`, `tool_call`, `cost`
(`budget`, likewise).

### A package

Register the class in your `pyproject.toml` under `[project.entry-points."noeta.plugins"]`
(`noeta.channels`, `noeta.providers`, `noeta.execenvs`, `noeta.stores` for the others) and
install it next to noeta — the built-ins do exactly this. [`examples/hello-plugin`](../examples/hello-plugin)
is one. An installed package wins a name clash with a file, and its `add_to` is ignored: a
dependency never changes your agents by being installed.

### A Claude Code plugin

A Claude Code plugin directory (`.claude-plugin/plugin.json`, `skills/`, `agents/`,
`commands/`, `hooks/hooks.json`, `.mcp.json`) put in `~/.noeta/plugins/` loads through the
plugins that read those formats; its skills, agents and commands are named `<plugin>:<name>`
(Claude Code's names), and commands, as skills do, run
their `!` lines through the permission checks and pre-approve their `allowed-tools`.
`${CLAUDE_PLUGIN_ROOT}` and `${CLAUDE_PLUGIN_DATA}` (`~/.noeta/plugin-data/<name>`) are
substituted. Its manifest's `userConfig` is asked for on `noeta plugin add` (or
`--config KEY=VALUE`; `noeta plugin configure` asks again), kept in `~/.noeta/settings.json`
`pluginConfigs` with sensitive values in the OS keyring, and substituted as Claude Code does
(each hook gets every value as `CLAUDE_PLUGIN_OPTION_<KEY>`). A secret
a machine cannot read stops only what uses it, with a notice: hooks get no variable for it, an
MCP server or hook naming it is left out, a `PreToolUse` / `PermissionRequest` /
`UserPromptSubmit` hook naming it refuses. Its own files may name only its own keyring secrets. Its `settings.json` `agent` names the agent
front ends start with. From a project, its MCP
servers never start: noeta says to copy them into `.noeta/mcp.json`, which needs consent.
Monitors, `bin/`, LSP servers, themes and output styles are not loaded (reasons [below](#compared-item-by-item)). noeta never
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

## Steps to write a plugin, next to pi

One tool plus an event handler, from nothing:

| Step | noeta | pi |
|---|---|---|
| 1. install | `uv tool install 'noeta[tui] @ git+https://github.com/initxy/noeta'` | `npm install -g @earendil-works/pi-coding-agent` |
| 2. model key | `NOETA_API_KEY` / `ANTHROPIC_API_KEY`, or `api_key` in `config.toml` | `/login`, or a provider key variable |
| 3. write one file | `~/.noeta/plugins/x.py`: an `Extension` with `add_to = ("main",)`, one `from noeta import …` line | `~/.pi/agent/extensions/x.ts` |
| 4. run it | `noeta`, or `/reload-plugins` in a running one | `pi`, or `/reload` |
| a file kept elsewhere, one run | `noeta --plugin-dir ./x.py` | `pi -e ./x.ts` |

Both take 4 steps, and 1 command to try a file in place. A skill is 1 step in noeta (a
`SKILL.md` folder in `~/.noeta/skills`).

## Compared item by item

Sources: pi-mono e7a0df2 (coding-agent 1.4.0), DeepSeek Harness f3803c96 (0.2.2-alpha.2),
Claude Code 2.1.312–2.1.314. Verdicts: **have** (noeta covers it, maybe in another shape),
**added** (built for this comparison), **won't** (left out on purpose). Only noeta's points hold
across machines: state is folded from the task log, the plugin set is pinned per task, and any
worker resumes any task; pi and dsh run in one process, Claude Code in one local session.

| Extension point | pi | dsh | Claude Code | noeta | Verdict | Reason |
|---|---|---|---|---|---|---|
| **Lifecycle** | | | | | | |
| Task start | `session_start` | `session/created`, `agent/created` | SessionStart | `on_start`; hook SessionStart | have | What is captured there is recorded, so a resumed task sends it again. |
| Task end | `session_shutdown` | `session/disposed` | SessionEnd | `on_event` (`TaskEnded`); hook SessionEnd | have | An end is an event in the log. |
| Every appended event | — | `session/event` | — | `on_event` | have | Metrics, audit and bridges read the log as it grows. |
| Turn, model call, idle / busy | `turn_*`, `agent_*`, `agent_settled` | `agent/status` | — | `on_event`: `InputTaken`, `ModelCalled`, `ModelReplied`, `Waiting` | have | Each is a logged event; no second bus. |
| Keep a turn going at its end | `agent_before_settle`, `turn_end` | `agent/turn-stopping` | Stop, SubagentStop | `on_event` on `ModelReplied` posting `Say(when="now")`; hook Stop | have | Going on is an inbox item, so it survives a crash. |
| Transform or block the person's input | `input` | `agent/pre-step` | UserPromptSubmit | `reminders` + `view.erase_messages` + raise `TurnFailed`; hook UserPromptSubmit | have | A blocked prompt is erased by a core record (ADR-026). |
| Before / after compaction, own summary | `session_before_compact`, `session_compact` | a `CompactionEngine` | PreCompact, PostCompact | `before_compact` (text or `Deny`); `on_event` (`Compacted`); a replacement records `view.compacted` | have | Compaction is a plugin; others take part through `before_compact` and `resident`. |
| Context overflow | — | `agent/request-error` | built in | `on_context_overflow(view, request)` | added | It now gets the request that overflowed, which compaction used to keep per task in memory. |
| Instruction files loaded | — | — | InstructionsLoaded | hook InstructionsLoaded | added | Claude Code has it: once per instruction file shown. |
| Model or effort switched | `model_select`, `thinking_level_select` | `agent-preset/selected` | PostModelSwitch | `on_event` (`InputTaken`); hook PostModelSwitch | have | A switch is input in the log. |
| Veto a fork, switch or tree move | `session_before_fork`, `_switch`, `_tree` | `sessions.fork` | `/rewind`, no hook | a fork is a new task (`history_from`); `continue_state` picks what carries over | won't | A fork never changes its source task, so there is nothing to veto. |
| A plugin grants project trust | `project_trust` | — | trust dialog | `noeta trust` | won't | Trust is the person's call; code that grants it widens what code may do. |
| Claude Code's other hook events | — | — | PreModelSwitch, Setup, TeammateIdle, Elicitation, ConfigChange, FileChanged, Worktree… | warned and skipped (`noeta/plugins/hooks/config.py`) | won't | Each belongs to a Claude Code feature noeta does not have. |
| Loader, HMR, market, web host events | — | Cordis `loader/*`, `hmr/*`, `plugin-market/*`, `webserver/*` | — | entry points, the HTTP API | won't | They are dsh's loader and web host, not agent behaviour. |
| **Model requests** | | | | | | |
| Change one request (messages, system) | `context`, `context_with_system` | `agent/pre-step`, `system-prompt/assemble` | — | `before_model` on the `ModelRequest`; `TurnDone`, `RequestStale` | have | The request stays a function of the log and these hooks. |
| Request headers | `before_provider_headers` | `deepseekLlmApiExtensions` | custom-headers variable | `ModelRequest.headers` in `before_model`; settings `extra_headers` | have | Per request, from the hooks. |
| Rewrite the raw provider payload | `before_provider_request` | `llm/stream` | — | — | won't | A request must be reproducible from the log; wire changes go in a provider. |
| Raw response, stream deltas | `after_provider_response`, `provider_stream_event` | `agent/assistant-stream` | — | `ModelReplied` via `on_event`; deltas reach front ends only | won't | Deltas are never stored: a plugin acting on them acts on what the log does not hold. |
| Replace the final reply | `message_end` | — | — | — | won't | The recorded reply is the truth; change the next request instead. |
| Cache warming | `cache_warming_decision` | — | — | — | won't | A call per idle task breaks "waiting costs nothing"; cache breakpoints do the work. |
| Retry policy as a hook | — | `agent/request-error` | built in | recorded kernel retries (`noeta/core/retry.py`) | won't | Retries are the same on every worker and visible in the log. |
| **Tools** | | | | | | |
| Register a tool, change the set at runtime | `registerTool` | `tools.register` | MCP server, SDK `tool()` | `tools(view)` every step; `function_tool` | have | The set may change from one step to the next. |
| Override or wrap a built-in | same name wins; `create*Tool` | `tools.setExecutor` | PreToolUse `updatedInput` | `Rewrite`, `after_tool`, `view.find_tool`; switch the built-in's plugin off | have | The first name wins, so a replacement is explicit, never a silent shadow. |
| Gate a call | `tool_call` | `tools/pre-execute` | PreToolUse, rules | `before_tool` → `Allow` / `Deny` / `Rewrite` / `Ask` | have | Deny > Rewrite > Ask > Allow; a rewrite is gated again. |
| Answer an ask in code | — | `approval/request` | PermissionRequest, `canUseTool` | `resolve_ask` | have | Answered before the call parks. |
| Change a result | `tool_result` | `tools/post-execute` | PostToolUse | `after_tool` | have | Before the result is recorded. |
| Wrap the whole dispatch | — | `tools/execute` | — | `before_tool` + `after_tool` | won't | Retries belong to the tool (`recover`, `retry_safe`), timing to `ToolStarted` / `ToolFinished`. |
| Deferred tools | `exposure` | `when` | `defer_loading` + ToolSearch | `Tool.deferred`; `tool_changes` | have | Sent only once `ToolSearch` loads them. |
| Parallel or last | `executionMode` | `isConcurrencySafe` | read-only tools together | `Tool.read_only`, `run_last` | have | Read-only calls of one reply run together. |
| Progress while a tool runs | `onUpdate` | — | live Bash output, `tool_progress` | `ToolContext.progress` | added | Claude Code has it; it streams with live output and is never recorded. |
| A tool calls the model | `modelRegistry.complete` | `ctx.llm` | WebFetch's small model | `ToolContext.complete` | added | `WebFetch` kept a per-process copy of each task's model call, lost when another worker ran the call. |
| A tool calls another tool | `ctx.executeTool` | — | — | `view.find_tool(name).run(…)` | have | Not gated again (`by_model` is false). |
| Wait for a person or children | blocking `ui.confirm` | approval, questions | AskUserQuestion | `ask()`, `ToolOutput.park` | have | The task holds no worker while it waits. |
| Crash recovery of one call | — | — | — | `Tool.recover`, `retry_safe`, `ctx.idempotency_key` | have | noeta only: a call never runs twice unknowingly. |
| Background work | — | `jobs.start` | background Bash | `ctx.hold()` | have | A hold keeps the tree on the worker the job lives on. |
| Structured output for the model | `outputSchema` | `output.schema` | MCP `structuredContent` | `ToolOutput.meta`, `display` for front ends | won't | The model reads content; structure rides along for front ends. |
| Rendering code for calls and results | `renderCall`, `renderResult` | UI slots | — | `ToolOutput.display` as data | won't | Front ends import no plugin and may be remote or in another language. |
| Shell environment | `spawnHook` | `shellEnv.register` | settings `env` | settings `env`; a `Rewrite` per call | have | Claude Code's settings key. |
| **Commands and flags** | | | | | | |
| Slash commands | `registerCommand` | `commands.register` | `commands/*.md`, skills | `commands` / `run_command`; `commands/*.md` | have | Front ends list and expand them (`noeta/core/commands.py`). |
| Argument completion | `getArgumentCompletions` | — | `argument-hint` | `Command.argument_hint` | have | A hint is data; a callback cannot reach a remote front end. |
| A command drives the session | command context | `ctx.sessions` | built-ins only | the client API | won't | New, fork and reload belong to front ends over the client, not to code inside a task. |
| Keybindings | `registerShortcut` | `shortcuts.register` | the person's `keybindings.json` | — | won't | Plugins never reach into front ends; Claude Code lets people, not plugins, bind keys. |
| Plugin command-line flags | `registerFlag` | `cmdlineArgs` | — | `[plugins.<name>]`, agent-file arguments | won't | A flag lives in one process; arguments are pinned per task and the same on every worker. |
| Load a plugin for one run | `pi -e` | `--patch` (a config overlay that may add plugins) | `--plugin-dir` | `--plugin-dir` | added | Claude Code has it; it was accepted and ignored. |
| **Context** | | | | | | |
| System prompt text | `systemPromptOptions` | `systemPrompt.section` | output styles, `--append-system-prompt` | `ContextPart` | have | Inside the one cached block. |
| Replace the system prompt | `SYSTEM.md` | `system-prompt/assemble` | `--system-prompt` | prompt override `system/<agent>`; agent file body | have | Overrides are recorded when a task starts, so a resumed task sends the same bytes. |
| Notes each turn | `sendMessage(nextTurn)` | `agent.inject` | `<system-reminder>` | `reminders` | have | Recorded, so a resumed task resends them. |
| Steer mid-turn | `sendUserMessage` | `agent.steer` | streaming input | `view.post`, `Say(when="now")` | have | An inbox item, taken at the next step. |
| Skills, prompt templates, instruction files | `SKILL.md`, `prompts/`, `AGENTS.md` | `skills.register`, agent instructions | skills, commands, `CLAUDE.md` | skills, `markdown_commands`, instructions (`NOETA.md`, `AGENTS.md`) | have | Claude Code's formats in noeta's own places (ADR-004). |
| Code plugin adds skills, commands, agents | `resources_discover` | `skills.registerProvider` | a plugin's directories | a Python plugin directory's `skills/`, `commands/`, `agents/` | added | A directory used to be either Python or a Claude Code plugin. |
| Text kept across a compaction | — | — | built in | `resident`, `lead_resident` | have | An interface, so any plugin's text survives. |
| **UI** | | | | | | |
| Dialogs | `ui.select`, `confirm`, `input` | approval, questions | AskUserQuestion, permission prompts | `ask()`, `Ask` | have | Every front end shows them with no plugin code. |
| Notices | `ui.notify` | toast slot | Notification hook | `view.notify(text, kind, data)` | have | Never reaches the model as the person. |
| Panels, widgets, editor, themes | `ui.*` | 106 UI slots | mods, themes | — | won't | Front ends import no plugin; a plugin publishes data a front end renders. |
| Status line | `setStatus` | — | `statusLine` setting | — | won't | Left to plugins: records and `notify` carry what a front end may show. |
| Plugin HTTP routes | — | `webServer.register` | — | — | won't | One API; traffic from outside comes in through a `Channel`. |
| **State** | | | | | | |
| Plugin entries, rebuilt from history | `appendEntry` | session events, projections | — | `view.record` + `reduce`, snapshots | have | Plugin state is a fold of the log. |
| Labels on entries | `setLabel` | — | — | — | won't | No Claude Code counterpart; a plugin can record its own. |
| Timers | — | `ctx.schedule` | scheduled wake-ups | `view.set_timer` | have | A timer is a record; the store fires it on any worker. |
| Read other tasks | `SessionManager.list` | `sessionQuery` | — | `read_log`, `child_tasks`, `ctx.tasks` | have | Read only. |
| Storage backend | — | `SessionPersistence` | — | `noeta.stores`, `store_contract` | have | Checked by the shared contract tests. |
| **Models** | | | | | | |
| A provider (wire) | `registerProvider` | `llm.registerAdapter` | — | a `noeta.providers` package | have | Found by name through entry points, like the built-ins. |
| A provider from a plugin file | yes | yes | — | — | won't | The plugin pin does not cover providers; one on a single worker breaks "any worker resumes any task". |
| Models and their family | `models.json` | `registerModelDiscovery` | — | `register_models`, a models file; any family | added | A third-party provider declares its own family; only anthropic and openai carry conventions. |
| Login providers | `oauth` | DeepSeek account | `/login` | keys, `{env:}` / `{keyring:}` | won't | A server holds operator keys; subscription login is a desktop concern. |
| Route per request | `registerVirtualModel` | `llm/stream` | — | `provider_for`; `before_model` sets `model` | have | From an SDK host. |
| **Sub-agents** | | | | | | |
| Sub-agent definitions | an example | `subagents.registerProvider` | `agents/*.md` | agent files, `subtasks` | have | Claude Code's agent files. |
| Per-agent plugins, config to children, queues | — | agent presets | per-agent tools, MCP, hooks | agent `plugins`, `inherit`, `spawn(queue=)` | have | A child may run on another machine's queue. |
| Other harnesses as sub-agents | — | codex, claude-code, ACP | — | — | won't | A sub-agent is a task in this store; another harness is a tool or an MCP server. |
| **Packaging** | | | | | | |
| One file found by itself | `~/.pi/agent/extensions` | — | a plugin directory | `~/.noeta/plugins`, a trusted `.noeta/plugins` | have | A project's code loads only once trusted. |
| Packages and install | npm, git, `pi install` | `dsh plugin add` | `/plugin install` | entry points; `noeta plugin add <git>@<commit>` | have | Installing runs nothing from the plugin. |
| Hosted gallery, auto-update | gallery, `pi update` | plugin market | marketplaces, auto-update | reads a Claude Code `marketplace.json` | won't | An operations product and a supply-chain risk; pinned installs instead. |
| Hot reload | `/reload` | HMR | `/reload-plugins` | `/reload-plugins` between turns; rolling restart | have | Never inside a turn: a request must stay a function of the log. |
| Plugin set pinned per task | — | — | — | `records.Plugins` | have | noeta only: a worker without a plugin never runs the task without it. |
| Bus or services between plugins | `pi.events` | Cordis services | — | through the kernel only | won't | State stays in the log, and plugins stay replaceable one by one. |
| The agent loads plugins it wrote | — | dynamic Cordis | — | writes to `.noeta/` ask | won't | Model output would install code into the harness. |
| Sandbox for in-process plugin code | — | — | mods behind an API | exec env, MCP servers, command hooks | won't | Not achievable in CPython; isolation is offered where it is real. |
| Names a plugin imports | `ExtensionAPI` | typed `ctx` | — | `from noeta import Extension, Tool, Allow, Deny, Ask, Rewrite, ToolUse, Recorded, ToolChanges, TaskState, …` | added | One import line for a plugin file. |
| **Config** | | | | | | |
| Per-plugin config, secrets | settings keys, `$ENV` | config schemas | `userConfig`, keychain | `[plugins.<name>]`; `{env:}`, `{keyring:}` | have | A secret is resolved only when the plugin is made. |
| A Claude Code plugin's `userConfig`, `settings.json` | — | — | asked on enable; `agent` | asked on `noeta plugin add`; `agent` | added | Claude Code has them. |
| Read others' settings, live config | `getSettings` | `ctx.settings`, `Volatile` | — | a plugin's own arguments | won't | One plugin should not depend on another's settings; a change is a reload, pinned per task. |
| Monitors, `bin/` | — | — | plugin parts | — | won't | A per-session process does not fit a task that holds no worker while waiting (use a channel or a timer); `bin/` silently widens what Bash runs. |
| LSP servers | — | — | plugin part | — | won't | A language server holds one checkout's state on one machine; run the checker as a tool in the exec env (`examples/run-tests`) or behind an MCP server. |
| Output styles | — | — | plugin part | prompt text recorded at start (`examples/house-style`) | won't | noeta has no style switch; a style is prompt text a plugin adds. |
| Themes | — | — | plugin part | — | won't | Colors belong to each front end. |
| **Data sent home by default** | install telemetry and provider attribution headers (`PI_TELEMETRY`) | on official-API requests: the session log (`dsh_session_log`), the plugin inventory (`dsh_plugin_packages`) and an anonymous user id | telemetry where analytics are on | nothing | won't | Never. An opt-in exporter can be an `on_event` plugin the operator installs. |
