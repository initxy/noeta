# Built-in plugins

Every plugin noeta ships, with what it does, which agents run it, the tools and commands it
adds, the `Extension` hooks it implements, and its settings. The built-ins register in
`pyproject.toml` under `[project.entry-points."noeta.plugins"]` exactly as a third-party plugin
would. To write your own, see [Write a plugin](../guides/plugins.md).

A plugin's settings are its constructor's keywords. Set them in `~/.noeta/config.toml` under
`[plugins.<name>]`, or in an agent file's `plugins` list as `- <name>: {key: value}`; every
plugin also takes `enabled = false`, which switches it off for every agent. Settings marked
**SDK** take a Python object (a callable, a transport, a client) and can only be given from
host code (`noeta.default_agents(plugins=…)`, `build_agents`).

## Which agents run which plugin

The built-in agents are the files in `noeta/agents/files/`. A plugin file's `add_to` or
`[agents.<name>] add_plugins` adds more.

| Plugin | `main` | `general-purpose` | `Explore`, `Plan` | Notes |
|---|---|---|---|---|
| `ask_user` | ✓ | | | only offered where a question can be answered |
| `budget` | ✓ | ✓ | ✓ | counts only the caps that are set |
| `checkpoints` | ✓ | ✓ | | |
| `compaction` | ✓ | ✓ | ✓ | always last in the list |
| `empty_reply` | ✓ | ✓ | ✓ | |
| `environment` | ✓ | ✓ | ✓ (`git_status: false`) | |
| `findings` | ✓ | | | |
| `fs` | ✓ | ✓ | ✓ (`tools: [Read]`) | |
| `hooks` | ✓ | ✓ | ✓ | |
| `instructions` | ✓ | ✓ | ✓ | |
| `markdown_commands` | ✓ | ✓ | ✓ | added to every agent by the `noeta` command |
| `mcp` | ✓ | ✓ | ✓ | off until set; the `noeta` command sets it |
| `memory` | ✓ | | | off until set; the `noeta` command sets it |
| `permissions` | ✓ | ✓ | ✓ | |
| `plan_mode` | ✓ | ✓ | ✓ | |
| `quiet_nudge` | ✓ | | | only some Claude models get the nudge |
| `repetition_guard` | ✓ | ✓ | ✓ | off until `[plugins.repetition_guard]` is set |
| `schedule` | ✓ | | | |
| `shell` | ✓ | ✓ | ✓ | |
| `skills` | ✓ | ✓ | ✓ | |
| `structured_output` | ✓ | ✓ | ✓ | every agent built from agent files; idle without a schema |
| `subtasks` | ✓ (`agents: ["*"]`) | ✓ (`agents: ["*"]`) | ✓ (no `Agent` tool) | |
| `title` | ✓ | | | |
| `todos` | ✓ | | | only older Claude models get the tools by default |
| `tool_search` | ✓ | ✓ | ✓ | every agent built from agent files |
| `web` | ✓ | ✓ | ✓ | `WebSearch` only with a search key |
| `workflow` | ✓ | | | |

"Off until set" (`noeta/agents/__init__.py` `OFF_UNLESS_SET`): `memory` writes under a
directory, `mcp` starts the servers it is given, and the repetition guard is not Claude Code's,
so `default_agents` keeps them off until the host passes a setting for them. The `noeta`
command passes one for `memory` and `mcp` (`noeta/cli/config.py`), so in the terminal, `-p`,
`noeta worker` and `noeta serve` both are on; `repetition_guard` stays off. An agent file
that names no `plugins` (a plain Claude Code agent file) gets `general-purpose`'s set
(`DEFAULT_PLUGINS`). `tool_search` and `structured_output` are added to every agent built from
agent files that does not name them (`ALWAYS_PLUGINS`).

The `noeta` command also fills some settings from noeta's places and the settings files:
`memory.home`, `hooks.settings_files`, `mcp.settings_files` and `mcp.project`, the `skills`
directories and listing budget, `instructions.user_dir` and `managed_dir`, the `permissions`
rules and mode, `plan_mode`'s settings, `fs.write_roots` (the additional directories), and
`environment.language` and the attribution lines. Your `config.toml` table is merged over
those, key by key.

## Tools and commands at a glance

| Plugin | Tools the model sees | Slash commands |
|---|---|---|
| `ask_user` | `AskUserQuestion` | |
| `compaction` | | `/compact` |
| `findings` | `ReportFindings` | |
| `fs` | `Read`, `Write`, `Edit`, `NotebookEdit` (deferred) | |
| `hooks` | | `/hooks` |
| `markdown_commands` | | `/init`, `/review`, and each `commands/*.md` |
| `mcp` | `mcp__<server>__<tool>` (deferred), `ListMcpResourcesTool`, `ReadMcpResourceTool`, `ReadMcpResourceDirTool` (deferred, top-level task only); `mcp__<server>__authenticate` / `complete_authentication` for a server that needs a sign-in | `/mcp`, and each server prompt |
| `plan_mode` | `EnterPlanMode`, `ExitPlanMode` (both deferred) | `/plan` |
| `schedule` | `ScheduleWakeup`; `CronCreate`, `CronList`, `CronDelete` (deferred) | `/loop` |
| `shell` | `Bash`; `Monitor`, `TaskStop`, `EnterWorktree`, `ExitWorktree` (deferred) | |
| `skills` | `Skill`; `run_skill_script` with `scripts = true` | `/<skill>` for each skill |
| `structured_output` | `StructuredOutput` (only when the task has an output schema) | |
| `subtasks` | `Agent`, `ListAgents` (top-level task only), `SendMessage` (deferred); `SubagentHandback` in a task `Agent` started | |
| `todos` | `TaskCreate`, `TaskGet`, `TaskUpdate`, `TaskList` (deferred) | |
| `tool_search` | `ToolSearch` (while any deferred tool is offered) | |
| `web` | `WebFetch` (deferred); `WebSearch` (deferred, with a key) | |
| `workflow` | `Workflow` | |

A deferred tool is named to the model and sent only once `ToolSearch` loads it
([Built-in tools](./tools.md) has every tool's input). The other plugins add no tools: they
work through prompt text, reminders, gates and records.

## The plugins

### `ask_user`

`noeta/plugins/ask_user` — offers `AskUserQuestion` where a question can be answered: in a task
a person follows (`interactive` is not `false`), or one whose client answers prompts
(`permission_prompt_tool`). A question already asked stays answerable if that changes. Child
tasks inherit the task's say on asking (`inherit`). No settings.

Hooks: `tools`, `inherit`.

### `budget`

`noeta/plugins/budget.py` — lifetime caps on a task: model calls, tool calls, cost, sub-agents
spawned and sub-agent depth (Claude Code's SDK has `maxTurns` and `maxBudgetUsd`). A call past
a cap is refused. The counts are folded from the task's log, so every worker enforces the same
caps.

Hooks: `before_model`, `before_tool`, `after_tool`, `on_event`, `reduce`.

| Setting | Default | Meaning |
|---|---|---|
| `max_iterations` | none | model calls; `max_turns` is another name for it |
| `max_turns` | none | same as `max_iterations` |
| `max_tool_calls` | none | tool calls |
| `max_cost_usd` | none | cost, from the model catalog's prices |
| `max_spawned_subtasks` | none | sub-agents started, counted on the `spawn_tools` calls |
| `max_subtask_depth` | `3` | how deep sub-agents may nest |
| `spawn_tools` | `["Agent", "Task"]` | the tools that start sub-agents |

### `checkpoints`

`noeta/plugins/checkpoints.py` — file checkpoints for rewind. Right before `Edit`, `Write` or
`NotebookEdit` runs, the file's content is recorded, once per file and prompt, content-addressed
(a blob is recorded once per task). Front ends read the `snapshot` records
(`noeta/host/rewind.py`) and send a `restore_files` signal, which writes files back through
the task's exec env, wherever it runs. It only restores what this task's log (and the
conversations it continues, and their sub-agents) snapshotted. Changes made through `Bash` are
not tracked, as in Claude Code. No settings.

Hooks: `on_event`, `reduce`.

### `compaction`

`noeta/plugins/compaction` — keeps a long task inside its model's context window: summarizes
the conversation when it nears the threshold, on `/compact`, or when the provider reports an
overflow, then puts back recently read files. A resumed task must get the same settings, since
they shape the request bytes. How it works: [Context, caching and compaction](../how-it-works/context.md).

Hooks: `commands`, `run_command`, `reminders`, `on_start`, `before_model`, `on_event`,
`reduce`, `continue_state`, `on_context_overflow`.

| Setting | Default | Meaning |
|---|---|---|
| `auto_compact_window` | `$NOETA_AUTO_COMPACT_WINDOW`, else the model's window | cap, in tokens, on the window the threshold derives from |
| `auto_compact_percent` | `$NOETA_AUTOCOMPACT_PCT_OVERRIDE` | fire at this percentage of the effective window when that is lower than the default threshold |
| `auto` | `true` | compact automatically (`/compact` works either way) |
| `compaction_model` | the task's model | another model for the summarize call only |
| `restore_files` | `true` | re-read recently used files after the summary |
| `read_tool` | the task's `Read` | **SDK**: the tool that re-reads them |
| `transcripts` | `true` | write the whole conversation to `<home>/projects/<workspace>/<task id>.jsonl` at each compaction (local workspaces only) and point the summary at it |
| `home` | noeta's home | where those transcripts go |
| `auto_compact_precompute` | `true` | write the summary in the background from about 80% of the effective window, to swap in at the threshold |
| `auto_compact_clear_results` | `false` | once more than `auto_compact_clear_idle_seconds` passed since the last model call (the cache is cold), clear older results of clearable tools |
| `auto_compact_clear_idle_seconds` | `3900` | the idle time for the above |
| `auto_compact_idle` | `false` | with the one-hour prompt cache, compact a large idle task before its cache expires |
| `name` | | **SDK**: the plugin's name |
| `clock` | | **SDK**: tests |

### `empty_reply`

`noeta/plugins/empty_reply.py` — Claude Code's empty-reply nudge. A reply with no text and no
tool call (reasoning alone counts as nothing) is not kept, and the model is asked to go on, once
per step; asked already with nothing new since, the turn ends without an answer. Without this
plugin the first empty reply ends the turn. No settings.

Hooks: `on_empty_reply`.

### `environment`

`noeta/plugins/environment` — what Claude Code tells the model about where it runs: a git
snapshot and the commit-attribution note in the first user turn, the environment (model line,
deferred tools, token budget, today's date) as a system turn after it, and a `<total_tokens>`
budget note after every prompt and tool result. Everything is captured through the task's exec
env, recorded when the task first starts, and taken again for a compaction, so it never changes
under the prompt cache in between. Only models Claude Code knows as Claude models get the
budget note.

Hooks: `reminders`, `on_start`, `before_model`, `on_event`, `reduce`, `inherit`, `resident`,
`lead_resident`.

| Setting | Default | Meaning |
|---|---|---|
| `model` | | the agent's model, named when the task's config sets none |
| `model_name` | `true` | include the model line |
| `git_status` | `true` | put the git snapshot in the first user turn |
| `git_refresh` | `true` | give the snapshot again at the start of a later turn when the branch or HEAD moved |
| `attribution` | `true` | put the commit / PR attribution note there |
| `attribution_commit`, `attribution_pr` | | the settings' `attribution.commit` / `attribution.pr` lines (`""`: none) |
| `language` | | the settings' `language`: the model is told to answer in it |
| `token_budget` | `15000000` | the budget a turn starts with; `0`: no budget notes |
| `date_change` | `true` | tell the model when the date is no longer the one it was given |
| `today` | the date in the task's `timezone`, else UTC | **SDK**: a callable returning `YYYY-MM-DD` |
| `critical_reminder` | `""` | text sent with every model call (an agent file's `criticalSystemReminder_EXPERIMENTAL`) |

### `findings`

`noeta/plugins/findings.py` — `ReportFindings`: a code review's findings handed to the front end
as typed data. They reach the person as a `core` / `notice` record of kind `findings`; the
model is told only how many were recorded. No settings.

Hooks: `tools`.

### `fs`

`noeta/plugins/fs` — the file tools `Read`, `Write`, `Edit` and `NotebookEdit`, run through the
task's exec env; `@path` mentions in the person's message attach those files. Where a write may
land is the permission layer's question, as in Claude Code.

Hooks: `tools`, `reminders`, `before_model`, `before_tool`, `after_tool`, `on_event`, `reduce`,
`continue_state`.

| Setting | Default | Meaning |
|---|---|---|
| `tools` | all four | which file tools to offer, by name (`[Read]` for a read-only agent) |
| `write_roots` | `[]` | directories besides the working directory writes may land in (Claude Code's `additionalDirectories`) |
| `block_writes_outside` | `false` | refuse, in every mode and before anyone is asked, a write outside the working directory and `write_roots` (`Bash` is not fenced: a sandbox does that) |
| `mentions` | `true` | attach the files the person names with `@path` |
| `home` | unset | the person's home, which `@~/…` names; front ends on the person's machine set it, a server does not |

### `hooks`

`noeta/plugins/hooks` — Claude Code's hooks: `command`, `http`, `prompt`, `agent`, `mcp_tool`
and client callbacks run at fixed points of a task. The events noeta fires: `PreToolUse`,
`PostToolUse`, `PostToolUseFailure`, `PostToolBatch`, `UserPromptSubmit`, `Stop`,
`StopFailure`, `SubagentStart`, `SubagentStop`, `SessionStart`, `SessionEnd`, `PreCompact`,
`PostCompact`, `PostModelSwitch`, `PermissionRequest`, `PermissionDenied`, `Notification`,
`CwdChanged`, `InstructionsLoaded`. Claude Code's other events are skipped with a warning
(`noeta/plugins/hooks/config.py`). Writing hooks: [Permissions and hooks](../guides/permissions.md).

Hooks: `commands`, `run_command`, `reminders`, `on_start`, `before_tool`, `resolve_ask`,
`after_tool`, `on_event`, `reduce`, `continue_state`, `inherit`, `before_compact`.

| Setting | Default | Meaning |
|---|---|---|
| `hooks` | | Claude Code's `hooks` object |
| `settings_files` | `[]` | settings files whose `hooks` are added (the `noeta` command passes its settings files) |
| `permission_prompt_tool` | | an MCP tool (`mcp__<server>__<tool>`) that answers every permission request, as `--permission-prompt-tool` |
| `callbacks` | | **SDK**: hooks a connected client registers and answers |
| `runner`, `http_transport` | | **SDK**: tests |

### `instructions`

`noeta/plugins/instructions` — instruction files in Claude Code's layers and noeta's places:
`NOETA.md` (managed, user, project, local) with `@path` imports and `.noeta/rules`, the
cross-tool `AGENTS.md`, and the auto memory index. See
[Instructions and memory](../guides/memory-and-instructions.md).

Hooks: `on_start`, `on_event`, `reduce`, `inherit`, `lead_resident`.

| Setting | Default | Meaning |
|---|---|---|
| `root` | `true` | record the files when a conversation starts |
| `file` | | read this path instead of walking the repository's directories |
| `discovery` | `true` | show the files that apply to a file a tool touches |
| `filenames` | `["AGENTS.md"]` | read (the first non-blank) in a directory with no `NOETA.md` / `.noeta/NOETA.md` |
| `user_dir` | none | the user's directory (`~/.noeta`): its `NOETA.md` and `rules/` |
| `managed_dir` | none | the machine's managed directory |
| `local` | `true` | read `NOETA.local.md` files |
| `walk` | `true` | read the working directory's ancestors too |
| `rehang` | `true` | put the files ahead of a compaction's summary |
| `origin` | `"system"` | origin of the recorded messages |
| `managed_only` | `false` | read only the managed directory's files (an agent file's `omitClaudeMd`) |

### `markdown_commands`

`noeta/plugins/markdown_commands.py` — slash commands written as markdown (Claude Code's
`commands/*.md`). Typing `/<name> <args>` adds the body with the arguments filled in, runs its
`` !`command` `` lines through the permission checks and pre-approves the tools its
`allowed-tools` names. Comes with `/init` (write `NOETA.md`) and `/review` (review changes).

Hooks: `commands`, `before_tool`, `on_event`, `reduce`.

| Setting | Default | Meaning |
|---|---|---|
| `commands` | `[]` | one table per command: `name`, `body`, and optionally `description`, `argument_hint`, `allowed_tools` |
| `builtin` | `true` | offer `/init` and `/review` too |

### `mcp`

`noeta/plugins/mcp` — tools, resources and prompts from MCP servers over stdio, Streamable
HTTP, SSE or WebSocket. The tool list is fetched when a turn opens, recorded, and fixed for the
turn; a resumed task keeps its tools; credentials never reach the log. Using it:
[MCP servers](../guides/mcp.md).

Hooks: `tools`, `commands`, `run_command`, `reminders`, `on_start`, `on_event`, `reduce`,
`resident`.

| Setting | Default | Meaning |
|---|---|---|
| `servers` | `[]` | servers in Claude Code's `mcpServers` shape; from host code also a function of the task |
| `settings_files` | `[]` | settings files whose `mcpServers` are added |
| `project` | `false` | also the task's `<cwd>/.noeta/mcp.json` servers its settings consent to; from host code a function of the directory |
| `agent_servers` | | an agent file's own `mcpServers`, for that agent's tasks only |
| `scope` | one shared partition | **SDK** (or a string): partitions shared connections, so a stateful server never carries one user's state into another's task |
| `pool` | one per worker process | **SDK**: the connection pool (idle connections closed after 30 minutes) |

### `memory`

`noeta/plugins/memory` — Claude Code's auto memory: a directory of markdown files the model
writes with `Write` / `Edit`, indexed by `MEMORY.md`, under
`<home>/projects/<workspace>/memory/`. `MEMORY.md` is read when the task starts (first 200 lines
and 25,000 characters) and again after a compaction. See
[Instructions and memory](../guides/memory-and-instructions.md).

Hooks: `context`, `reminders`, `on_start`, `after_tool`, `reduce`, `continue_state`, `inherit`,
`lead_resident`.

| Setting | Default | Meaning |
|---|---|---|
| `home` | `$NOETA_HOME` or `~/.noeta` | noeta's home |
| `directory` | | use this directory instead |
| `recall_model` | off | a model asked, on each user message, which memory files to attach (up to five) |
| `rehang` | `true` | read `MEMORY.md` again after a compaction |
| `agent_scope`, `agent_name` | | an agent file's `memory` field: the agent's own directory (`user`, `project` or `local`) |

### `permissions`

`noeta/plugins/permissions` — Claude Code's permission system over every tool call: the modes,
`allow` / `deny` / `ask` rules, working directories and protected paths, the auto-mode
classifier, plus declarative PreToolUse rules and `can_use_tool`. A deny rule wins over
everything, then an ask rule, then an allow rule. Using it:
[Permissions and hooks](../guides/permissions.md).

Hooks: `reminders`, `on_start`, `before_model`, `before_tool`, `resolve_ask`, `on_event`,
`reduce`, `continue_state`, `inherit`.

| Setting | Default | Meaning |
|---|---|---|
| `mode` | `"auto"` | the starting permission mode (`defaultMode`) |
| `allow`, `deny`, `ask` | `[]` | rule strings: `Bash(git push *)`, `Read(./.env)`, `WebFetch(domain:example.com)`, `mcp__server` |
| `additional_directories` | `[]` | directories file tools may read and edit as if inside the working directory |
| `auto_mode` | | Claude Code's `autoMode` object (`allow`, `soft_deny`, `hard_deny`, `environment`) for the classifier |
| `disable_bypass_mode` | `false` | refuse `bypassPermissions` |
| `classifier_model` | `claude-sonnet-5` for a Claude task, else the task's model | the auto-mode classifier's model |
| `plans_dir` | `<home>/plans` | plan files, written and read without asking |
| `trusted_workspace` | `false` | "always" answers also written to the project's `.noeta/settings.local.json`; instruction files count as intent to the classifier (a callable from host code) |
| `remember_to_project` | `true` | the above writing, when trusted |
| `shell_allowlist` | `[]` | `{rule = "git push *"}` entries: shorthands for `Bash(...)` allow rules |
| `webfetch_allowed_hosts` | `[]` | `example.com` / `*.example.com`: shorthands for `WebFetch(domain:...)` allow rules |
| `risk`, `default_risk`, `max_risk` | `{}`, `"high"`, none | which tools need permission at all (`low` does not), and a ceiling that denies tools above it |
| `allowed_tools`, `disallowed_tools` | | filter the offered tools by name (removed from the request, denied if called anyway); disallowing `Bash` also removes `Monitor` and `TaskStop` |
| `pre_tool_use` | `[]` | declarative PreToolUse rules (`noeta/plugins/permissions/guards.py`) |
| `can_use_tool` | | **SDK**: `(tool, input) -> bool` that answers asks instead of a person |

### `plan_mode`

`noeta/plugins/plan_mode` — Claude Code's plan mode: `/plan`, `EnterPlanMode`, `ExitPlanMode`
(the approval question), the plan file and the plan-mode notes. The mode itself is the
permissions plugin's `mode` record; without that plugin, this one keeps planning read-only by
itself.

Hooks: `tools`, `commands`, `run_command`, `reminders`, `on_start`, `before_tool`, `on_event`,
`reduce`, `inherit`, `resident`.

| Setting | Default | Meaning |
|---|---|---|
| `plans_dir` | `<home>/plans` | where plan files go |
| `auto_during_plan` | `true` | a plan begun in auto mode runs with the auto-mode classifier (`useAutoModeDuringPlan`) |

### `quiet_nudge`

`noeta/plugins/quiet_nudge.py` — Claude Code's quiet-stretch nudge: after several steps in a
row that only called tools, with no text for the person, the model is told to keep them posted.
Only `claude-opus-5-5`, `claude-sonnet-5-5`, `claude-fable-5-1` and `claude-mythos-5-1` get it,
as in Claude Code.

Hooks: `reminders`.

| Setting | Default | Meaning |
|---|---|---|
| `steps` | `5` | tool-only steps in a row before each nudge |
| `most` | `3` | nudges per quiet stretch |

### `repetition_guard`

`noeta/plugins/repetition_guard.py` — a call identical to the ones right before it (same tool,
same arguments), `threshold` times in a row counting itself, is asked about or refused. It reads
the conversation, so any worker sees the same run of calls. Claude Code has no such guard, so it
is off until `[plugins.repetition_guard]` is set.

Hooks: `before_tool`.

| Setting | Default | Meaning |
|---|---|---|
| `threshold` | `3` | identical calls in a row; `<= 0` is off |
| `action` | `"require_approval"` | `require_approval` asks the person, `deny` refuses |
| `window` | `8` | the recent calls looked at |

### `schedule`

`noeta/plugins/schedule` — scheduled prompts as Claude Code has them: `ScheduleWakeup` paces a
self-paced `/loop`; `CronCreate` / `CronList` / `CronDelete` run a prompt on a cron schedule or
once; `/loop` sets either up. Every wake-up and job is a kernel timer in the task's log, so it
survives restarts and fires on whichever worker holds the task. See
[Sub-agents and background work](../guides/subagents.md). No settings (`clock` is for tests).

Hooks: `tools`, `commands`, `on_start`, `on_event`, `reduce`, `continue_state`.

### `shell`

`noeta/plugins/shell` — `Bash` (whose working directory carries over between commands), and
background jobs: `run_in_background`, `Monitor` and `TaskStop`; `EnterWorktree` /
`ExitWorktree` move the shell into a git worktree under `.noeta/worktrees/` and back. Jobs
belong to the worker process: while one runs, the task keeps its claim on that worker. See
[Sub-agents and background work](../guides/subagents.md) and
[Where commands run](../guides/sandbox.md).

Hooks: `tools`, `reminders`, `on_start`, `on_event`, `reduce`, `continue_state`, `resident`.

| Setting | Default | Meaning |
|---|---|---|
| `background` | `true` | offer background jobs (`run_in_background`, `Monitor`); off = foreground `Bash` only (`TaskStop` stays, for background agents and Workflow runs) |
| `background_ends_with_turn` | `false` | end a task's jobs when its turn ends (one-shot workers) |
| `bash` | `{}` | keywords for `Bash`: `timeout_s`, `output_cap`, `shell_env`, `hardening_rules` |
| `max_jobs` | 8 per task | with `log_dir`, a private job table instead of the process-wide one |
| `log_dir` | | where that table keeps job logs |
| `registry` | the process-wide table | **SDK**: the job table |

### `skills`

`noeta/plugins/skills` — skills from `SKILL.md` directories: the listing the model sees, the
`Skill` tool, `/<name>` for each skill, `run_skill_script` (when `scripts` is on) and
`allowed-tools` enforcement. Using them: [Skills, agents and commands](../guides/skills-and-agents.md).

Hooks: `tools`, `commands`, `reminders`, `on_start`, `before_model`, `before_tool`,
`after_tool`, `on_event`, `reduce`, `continue_state`, `resident`.

| Setting | Default | Meaning |
|---|---|---|
| `dirs` | `[]` | the lowest tier: built-in and borrowed skill directories |
| `plugin_dirs` | | directory → plugin name: a Claude Code plugin's skills, named `<plugin>:<skill>` |
| `global_agents_dir`, `global_dir` | none | the user's `~/.agents/skills` / `~/.noeta/skills` |
| `global_commands_dir` | none | the user's `~/.noeta/commands`: each `*.md` is a skill |
| `managed_dir` | none | the machine's managed directory; its skills go first |
| `workspace` | `true` | the working directory's (and its parents' up to the repository root) `.noeta/skills`, `.agents/skills`, `.noeta/commands` |
| `additional_dirs` | `[]` | the task's other directories, after the workspace's |
| `skills_dir` | | one directory replacing the workspace tiers, not trust-gated |
| `plugin_vars`, `plugin_commands` | | a Claude Code plugin's variables and commands |
| `trusted` | open | **SDK**: `trusted(path) -> bool` for the repository tiers |
| `budget_chars` | | listing budget in characters (`NOETA_SLASH_COMMAND_TOOL_CHAR_BUDGET` in the `noeta` command) |
| `budget_fraction` | `0.01` | else this share of the model's window (`skillListingBudgetFraction`) |
| `max_desc_chars` | `1536` | each listed description's cap (`skillListingMaxDescChars`) |
| `rank`, `usage_from` | | **SDK**: rank the listing; the `noeta` command ranks by use across its store's tasks |
| `preload` | `[]` | skills active from the task's start |
| `scripts` | `false` | offer `run_skill_script` |
| `tool_enforcement` | `"off"` | `allowed-tools` of active skills: `off`, `approval` or `deny` |
| `rehang` | `true` | put the listing and active skills right after a compaction's summary |
| `origin` | `"system"` | origin of the recorded listing and skill text |
| `shell_commands` | `true` | run a skill body's `` !`command` `` lines when it loads |

### `structured_output`

`noeta/plugins/structured_output.py` — a task whose config sets `output_schema` (`noeta -p
--json-schema`, an HTTP request's config, a workflow's `agent({schema})`) is offered a
`StructuredOutput` tool whose input schema is that schema. A call that fits ends the turn with
the value as its answer; one that does not gets an error naming what is wrong. Does nothing for
a task without a schema.

Hooks: `tools`, `on_start`, `before_model`, `on_event`, `reduce`.

| Setting | Default | Meaning |
|---|---|---|
| `output_schema` | | the schema when neither the task's config nor its agent sets one |
| `max_retries` | `5` | refused calls in a turn before it fails |

### `subtasks`

`noeta/plugins/subtasks` — sub-agents: `Agent` starts a child task (in the background or the
foreground), `SendMessage` reaches a running one, `ListAgents` lists them, and a child started by
`Agent` gets `SubagentHandback`. See [Sub-agents and background work](../guides/subagents.md).

Hooks: `tools`, `reminders`, `on_start`, `before_model`, `before_tool`, `on_event`, `reduce`,
`continue_state`, `resident`.

| Setting | Default | Meaning |
|---|---|---|
| `agents` | `[]` | agent types the model may start; `"*"` names every agent with a description except the task's own; none: no `Agent` tool |
| `roster` | `[]` | **SDK**: agent types as `SubagentSpec`s |
| `max_depth` | `3` | a task at this depth may not delegate (root = 0) |
| `max_background` | `20` | agents one task may have running at once |
| `exclusive_with` | `[]` | tool names an `Agent` call may not share a reply with |
| `inherit_config` | `["cwd", "model", "effort"]` (`main`: plus `permission_mode`) | parent config keys a child copies; `model` and `effort` only when neither the call nor the agent sets them |
| `queue` | the caller's | the queue children are created on |
| `progress_labels` | `true` | every 30 s a running sub-agent asks for a 3–5 word label of what it is doing, shown to front ends only |

### `title`

`noeta/plugins/title.py` — names the task once, as Claude Code names a session: from the first
prompt of at least 10 characters, in a side request. A task nobody follows live
(`interactive: false`) is not named. Front ends read the `title` record; a person renames with
the `rename` signal.

Hooks: `on_event`, `reduce`.

| Setting | Default | Meaning |
|---|---|---|
| `model` | the task's model | the model for the side request |

### `todos`

`noeta/plugins/todos` — Claude Code's task list: `TaskCreate`, `TaskGet`, `TaskUpdate`,
`TaskList`, and a reminder after ten model turns without them. As in Claude Code 2.1.312, only
the Claude models older than its lean prompt (and Bedrock application inference profiles) get
them by default; the Claude 5 family and every
other model get none. Only a task without a parent gets the tools.

Hooks: `tools`, `context`, `reminders`, `reduce`, `continue_state`.

| Setting | Default | Meaning |
|---|---|---|
| `all_models` | `false` | offer the tools to every model (Claude Code's `CLAUDE_CODE_ENABLE_TODO_TOOLS`) |

### `tool_search`

`noeta/plugins/tool_search` — deferred tool loading. While a task offers any deferred tool it
adds `ToolSearch` and withholds the deferred tools not loaded yet; a tool is loaded once a tool
result names it, recorded so a compaction does not unload it. No settings.

Hooks: `tool_changes`, `after_tool`, `reduce`.

### `web`

`noeta/plugins/web` — `WebFetch` (fetches a page and answers the call's `prompt` with a digest
from the task's model, or a cheaper one) and `WebSearch` (through the Tavily Search API, only
with a key).

Hooks: `tools`, `on_event`.

| Setting | Default | Meaning |
|---|---|---|
| `webfetch_model` | the task's model | the model that digests fetched pages |
| `search_api_key` | `$NOETA_WEB_SEARCH_API_KEY` | the Tavily key; with neither, no `WebSearch` |
| `digest_timeout` | `60` | seconds before a digest is given up (the raw page is returned) |
| `digest`, `fetch_transport`, `search_transport` | | **SDK**: replace the digest call or the HTTP transports (tests, a proxy, a sandbox egress) |

### `workflow`

`noeta/plugins/workflow` — `Workflow`: the model writes a JavaScript script against the workflow
API (`agent()`, `parallel()`, `pipeline()`, `phase()`, `log()`, `args`); each `agent()` is a
child task, the run goes on in the background, and only the results live in the log, so a run
survives restarts and moves between workers. Also Claude Code's `ultracode` switch. See
[Sub-agents and background work](../guides/subagents.md).

Hooks: `tools`, `reminders`, `on_start`, `on_event`, `reduce`, `continue_state`.

| Setting | Default | Meaning |
|---|---|---|
| `default_agent` | `"general-purpose"` | the agent type `agent()` runs, else the task's own |
| `max_running` | CPUs − 2, between 2 and 16 | agents running at once per run |
| `home` | `~/.noeta` | noeta's directory, holding the user's saved workflows |
| `engine` | | **SDK**: tests |

## The other entry-point groups

Plugins are one of six groups noeta finds by name (`noeta/core/registry.py`). What it ships in
the others:

| Group | Name | What |
|---|---|---|
| `noeta.providers` | `anthropic`, `openai_compat`, `openai_responses` | model APIs ([Models and providers](../guides/models.md)) |
| `noeta.execenvs` | `local`, `container` | where commands run ([Where commands run](../guides/sandbox.md)) |
| `noeta.stores` | `sqlite`, `postgres` (also `postgresql`) | where tasks are stored ([Run it as a server](../guides/server.md)) |
| `noeta.frontends` | `tui` (from the `noeta-tui` package) | the terminal UI |
| `noeta.channels` | none built in | outside conversations; `examples/chat-bridge` is one |
