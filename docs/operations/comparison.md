# Compared with other agents

How noeta's plugin surface lines up with three other agents: Claude Code (whose behaviour noeta
follows), pi and DeepSeek Harness (dsh). Use it to port a plugin, or to see what noeta left out
on purpose and why. How noeta checks that the model sees what Claude Code would send is a
separate page, [Matching Claude Code](../how-it-works/parity.md).

## The short version

| | Claude Code | pi | DeepSeek Harness (dsh) | noeta |
|---|---|---|---|---|
| Runs as | one local process per session | one process | one process | any number of workers on one store, or one process |
| A task's state lives in | the local session | the process, plus session entries | the process, plus session events | the task's log in the store; everything is folded from it |
| Another machine can continue a task | no | no | no | yes, from its log |
| Plugins written in | markdown, JSON, shell / HTTP hooks, MCP | TypeScript | TypeScript (Cordis) | Python, plus Claude Code's markdown, JSON, hooks and MCP formats |
| A task's plugin set | whatever is loaded | whatever is loaded | whatever is loaded | recorded when it starts; a worker without one never runs it |
| Sends data home by default | telemetry where analytics are on | install telemetry, provider attribution headers | session log, plugin inventory, an anonymous id (official API) | nothing |

What follows is the detail. Only noeta's points hold across machines: state is folded from the
task log, the plugin set is pinned per task, and any worker resumes any task; pi and dsh run in
one process, Claude Code in one local session.

## Steps to write a plugin, next to pi

One tool plus an event handler, from nothing:

| Step | noeta | pi |
|---|---|---|
| 1. install | `uv tool install --with 'noeta-tui @ git+https://github.com/initxy/noeta#subdirectory=tui' 'noeta @ git+https://github.com/initxy/noeta'` | `npm install -g @earendil-works/pi-coding-agent` |
| 2. model key | `NOETA_API_KEY` / `ANTHROPIC_API_KEY`, or `api_key` in `config.toml` | `/login`, or a provider key variable |
| 3. write one file | `~/.noeta/plugins/x.py`: an `Extension` with `add_to = ("main",)`, one `from noeta import …` line | `~/.pi/agent/extensions/x.ts` |
| 4. run it | `noeta`, or `/reload-plugins` in a running one | `pi`, or `/reload` |
| a file kept elsewhere, one run | `noeta --plugin-dir ./x.py` | `pi -e ./x.ts` |

Both take 4 steps, and 1 command to try a file in place. A skill is 1 step in noeta (a
`SKILL.md` folder in `~/.noeta/skills`). The noeta side is walked through in
[Write a plugin](../guides/plugins.md).

## Compared item by item

Sources: pi-mono e7a0df2 (coding-agent 1.4.0), DeepSeek Harness f3803c96 (0.2.2-alpha.2),
Claude Code 2.1.312–2.1.314. Verdicts: **have** (noeta covers it, maybe in another shape),
**added** (built for this comparison), **won't** (left out on purpose). The noeta column names
the code; [Extension interface](../reference/extension.md) has each method.

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
| Context overflow | — | `agent/request-error` | built in | `on_context_overflow(view, request)` | added | The hook is handed the request that overflowed, so no plugin has to keep a copy per task in memory. |
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
| Deferred tools | `exposure` | `when` | `defer_loading` + ToolSearch | `ToolFlags.deferred`; `tool_changes` | have | Sent only once `ToolSearch` loads them. |
| Parallel or last | `executionMode` | `isConcurrencySafe` | read-only tools together | `Tool.read_only`, `ToolFlags.run_last` | have | Read-only calls of one reply run together. |
| Progress while a tool runs | `onUpdate` | — | live Bash output, `tool_progress` | `ToolContext.progress` | added | Claude Code has it; it streams with the live output and is never recorded. |
| A tool calls the model | `modelRegistry.complete` | `ctx.llm` | WebFetch's small model | `ToolContext.complete` | added | `WebFetch`'s digest goes through it, so a call that moves to another worker still reaches the task's model. |
| A tool calls another tool | `ctx.executeTool` | — | — | `view.find_tool(name).run(…)` | have | Not gated again (`by_model` is false). |
| Wait for a person or children | blocking `ui.confirm` | approval, questions | AskUserQuestion | `ask()`, `ToolOutput.park` | have | The task holds no worker while it waits. |
| Crash recovery of one call | — | — | — | `Tool.recover`, `ToolFlags.retry_safe`, `ctx.idempotency_key` | have | noeta only: a call never runs twice unknowingly. |
| Background work | — | `jobs.start` | background Bash | `ctx.keep()` | have | The task keeps its claim on the worker the job lives on. |
| Structured output for the model | `outputSchema` | `output.schema` | MCP `structuredContent` | `ToolOutput.meta`, `display` for front ends | won't | The model reads content; structure rides along for front ends. |
| Rendering code for calls and results | `renderCall`, `renderResult` | UI slots | — | `ToolOutput.display` as data | won't | Front ends import no plugin and may be remote or in another language. |
| Shell environment | `spawnHook` | `shellEnv.register` | settings `env` | settings `env`; a `Rewrite` per call | have | Claude Code's settings key. |
| **Commands and flags** | | | | | | |
| Slash commands | `registerCommand` | `commands.register` | `commands/*.md`, skills | `commands` / `run_command`; `commands/*.md` | have | Front ends list and expand them (`noeta/core/commands.py`). |
| Argument completion | `getArgumentCompletions` | — | `argument-hint` | `Command.argument_hint` | have | A hint is data; a callback cannot reach a remote front end. |
| A command drives the session | command context | `ctx.sessions` | built-ins only | the client API | won't | New, fork and reload belong to front ends over the client, not to code inside a task. |
| Keybindings | `registerShortcut` | `shortcuts.register` | the person's `keybindings.json` | — | won't | Plugins never reach into front ends; Claude Code lets people, not plugins, bind keys. |
| Plugin command-line flags | `registerFlag` | `cmdlineArgs` | — | `[plugins.<name>]`, agent-file arguments | won't | A flag lives in one process; arguments are pinned per task and the same on every worker. |
| Load a plugin for one run | `pi -e` | `--patch` (a config overlay that may add plugins) | `--plugin-dir` | `--plugin-dir` | added | Claude Code has it. |
| **Context** | | | | | | |
| System prompt text | `systemPromptOptions` | `systemPrompt.section` | output styles, `--append-system-prompt` | `ContextPart` | have | Inside the one cached block. |
| Replace the system prompt | `SYSTEM.md` | `system-prompt/assemble` | `--system-prompt` | prompt override `system/<agent>`; agent file body | have | Overrides are recorded when a task starts, so a resumed task sends the same bytes. |
| Notes each turn | `sendMessage(nextTurn)` | `agent.inject` | `<system-reminder>` | `reminders` | have | Recorded, so a resumed task resends them. |
| Steer mid-turn | `sendUserMessage` | `agent.steer` | streaming input | `view.post`, `Say(when="now")` | have | An inbox item, taken at the next step. |
| Skills, prompt templates, instruction files | `SKILL.md`, `prompts/`, `AGENTS.md` | `skills.register`, agent instructions | skills, commands, `CLAUDE.md` | skills, `markdown_commands`, instructions (`NOETA.md`, `AGENTS.md`) | have | Claude Code's formats in noeta's own places (ADR-004). |
| Code plugin adds skills, commands, agents | `resources_discover` | `skills.registerProvider` | a plugin's directories | a Python plugin directory's `skills/`, `commands/`, `agents/` | added | A Python plugin directory may ship them beside its code. |
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
| Several providers, a model switched across them | `models.json` providers; `/model` across them | not checked | one provider at a time; `/model` among its models | `[providers.<name>]` tables; `/model`, stream-json `set_model` and ACP switch across them; the chosen provider and the model's limits are recorded in the task's config | added | The owner asked for any model, switched mid-task; recording the route keeps a resumed task on the same provider on every worker. |
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

## What the "won't" rows have in common

Most of what noeta leaves out falls under four rules, each a consequence of a task being a log
that any worker may continue:

- **The request is a function of the log.** Nothing may change the bytes sent to the model
  unless the log can reproduce it on another worker: no raw payload rewriting, no acting on
  stream deltas, no replacing the recorded reply, no hot reload inside a turn.
- **Plugins never reach into front ends.** Front ends may be remote, in another language, or
  several at once, so a plugin publishes data (records, `notify`, `ToolOutput.display`) and each
  front end decides how to show it: no panels, keybindings, themes or render callbacks.
- **Nothing lives in one process unless a claim keeps it there.** Per-session monitors, LSP
  servers and plugin-only providers would tie a task to one machine; a channel, a timer, an exec
  env or `ctx.keep()` is used instead.
- **What widens what code may do is the person's call.** Plugins cannot grant trust, the model's writes to
  `.noeta/` are asked about (so it cannot install a plugin it wrote), and installs are pinned and
  run nothing.
