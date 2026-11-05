# Built-in tools

Every tool the model can call in noeta, the plugin that provides it, its parameters and how the
permission system treats it. Names and schemas follow Claude Code, because models carry their
strongest habits on those names. Each tool's full model-facing text is its `description` in the
plugin's code (most under `noeta/plugins/<plugin>/descriptions/`).

How to read the tables:

- **Parameters**: required ones in bold.
- **Loaded**: *always* means the tool is sent with every request; *deferred* means only its name
  is listed and the model loads its schema with `ToolSearch` first
  ([deferred tools](#toolsearch)).
- **Read-only**: read-only calls of one reply may run side by side (up to ten at once); every
  other call runs alone, in order.
- **Permission**: what the [permissions plugin](../guides/permissions.md#how-a-call-is-decided)
  says in `default` mode, before any rule; `acceptEdits`, `auto` and `plan` change it as noted.
  Deny and ask rules, hooks and other modes apply on top. "Classified in auto" means `auto` mode
  hands the call to the classifier instead of letting it through.

## At a glance

| Tool | Plugin | Loaded | Read-only | Permission (`default` mode) |
|---|---|---|---|---|
| [`Read`](#read) | `fs` | always | yes | allowed inside the working directories; asks outside |
| [`Write`](#write) | `fs` | always | | asks; allowed in `acceptEdits` and `auto` inside the working directories |
| [`Edit`](#edit) | `fs` | always | | as `Write` |
| [`NotebookEdit`](#notebookedit) | `fs` | deferred | | as `Write` |
| [`Bash`](#bash) | `shell` | always | | read-only commands allowed; others ask |
| [`Monitor`](#monitor) | `shell` | deferred | | as `Bash`; a `ws` watch asks |
| [`TaskStop`](#taskstop) | `shell` | deferred | | allowed |
| [`EnterWorktree`](#enterworktree-exitworktree) | `shell` | deferred | | allowed |
| [`ExitWorktree`](#enterworktree-exitworktree) | `shell` | deferred | | allowed |
| [`Agent`](#agent) | `subtasks` | always | | allowed; classified in `auto` |
| [`SendMessage`](#sendmessage) | `subtasks` | deferred | | allowed; classified in `auto` |
| [`ListAgents`](#listagents) | `subtasks` | always | yes | allowed |
| [`SubagentHandback`](#subagenthandback) | `subtasks` | sub-agents only | | allowed; classified in `auto` |
| [`Skill`](#skill) | `skills` | always | | allowed |
| [`AskUserQuestion`](#askuserquestion) | `ask_user` | always | | allowed (it asks you itself) |
| [`WebFetch`](#webfetch) | `web` | deferred | yes | asks per host |
| [`WebSearch`](#websearch) | `web` | deferred | yes | asks |
| [`EnterPlanMode`](#enterplanmode) | `plan_mode` | deferred | yes | asks (firm); allowed in `auto` and `plan` |
| [`ExitPlanMode`](#exitplanmode) | `plan_mode` | deferred | | allowed (it asks you itself) |
| [`TaskCreate`, `TaskGet`, `TaskUpdate`, `TaskList`](#task-list-tools) | `todos` | deferred | `TaskGet`, `TaskList` | allowed |
| [`ScheduleWakeup`](#schedulewakeup) | `schedule` | always | | allowed |
| [`CronCreate`, `CronList`, `CronDelete`](#cron-tools) | `schedule` | deferred | `CronList` | allowed; `CronCreate` classified in `auto` |
| [`Workflow`](#workflow) | `workflow` | always | | allowed; classified in `auto` |
| [`ReportFindings`](#reportfindings) | `findings` | always | yes | allowed |
| [`StructuredOutput`](#structuredoutput) | `structured_output` | with an answer schema | yes | allowed |
| [`ToolSearch`](#toolsearch) | `tool_search` | while any tool is deferred | yes | allowed |
| [`ListMcpResourcesTool`, `ReadMcpResourceTool`, `ReadMcpResourceDirTool`](#mcp-tools) | `mcp` | deferred | yes | allowed |
| [`mcp__<server>__<tool>`](#mcp-tools) | `mcp` | deferred | | asks |
| [`run_skill_script`](#run-skill-script) | `skills` | only with `scripts` on | | asks |

The `main` agent runs every plugin above; sub-agents run fewer
([agent files](../guides/skills-and-agents.md#agent-files)). Turning a plugin off
(`[plugins.web] enabled = false`) takes its tools away; `--tools`, `--disallowedTools`, an
agent file's `tools` / `disallowedTools` and the permissions plugin's `allowed_tools` /
`disallowed_tools` filter them by name. Disallowing `Bash` also takes `Monitor` and `TaskStop`.

noeta offers no `Glob`, `Grep` or `LS` tool: the model searches with `Bash` (`rg`, `find`),
whose read-only commands run without asking. `Read(...)` permission rules
still cover those names.

## Files: the `fs` plugin

`noeta/plugins/fs/`. The plugin also remembers which files the model read: `Edit` and `Write`
refuse a file that was not read, or that changed on disk since (a formatter, another program),
until it is read again, and the model is told once when a file it read changes. Files a `Bash`
command showed (`cat`, `head`, `sed -n`) count as read.

### Read

Reads one file: text (with line numbers), an image, a PDF or a Jupyter notebook. Retried after a
crash instead of being answered "outcome unknown".

| Parameter | Type | Meaning |
|---|---|---|
| **`file_path`** | string | absolute path |
| `offset` | integer | line to start from (1 is the first) |
| `limit` | integer | how many lines |
| `pages` | string | PDF pages, `"1-5"`, `"3"`; at most 20 per call |
| `allow_large` | boolean | read past the usual size limits, up to what fits in the context |

Permission: allowed inside the working directories (and for the plan file, the memory directory
and saved tool outputs); outside them it asks, offering `Read(//<dir>/**)` as the "always" rule.

### Write

Writes a whole file, replacing it if it exists.

| Parameter | Type | Meaning |
|---|---|---|
| **`file_path`** | string | absolute path |
| **`content`** | string | the whole content |

Permission: asks in `default`; allowed in `acceptEdits`, `auto` and `bypassPermissions` inside the
working directories; outside them it asks in every mode but `bypassPermissions` (in `auto` the
classifier answers); a protected path (`.git/`, `.bashrc`, …) asks in every mode; asks in `plan`
except for the plan file. Markdown files in the memory directory are written without asking. A
checkpoint of the file is recorded first ([rewind](../guides/permissions.md#checkpoints-and-rewind)).

### Edit

Replaces an exact string in a file.

| Parameter | Type | Meaning |
|---|---|---|
| **`file_path`** | string | absolute path |
| **`old_string`** | string | the text to replace |
| **`new_string`** | string | its replacement (must differ) |
| `replace_all` | boolean | replace every occurrence (default false) |

Permission: as `Write`.

### NotebookEdit

Changes one cell of a `.ipynb`: replace its source, insert a cell, or delete one.

| Parameter | Type | Meaning |
|---|---|---|
| **`notebook_path`** | string | absolute path |
| **`new_source`** | string | the cell's new source |
| `cell_id` | string | the cell; with `insert`, the new cell goes after it (first when left out) |
| `cell_type` | string | `code` or `markdown`; required with `insert` |
| `edit_mode` | string | `replace` (default), `insert` or `delete` |

Permission: as `Write`.

## Shell: the `shell` plugin

`noeta/plugins/shell/`. Commands run in the task's exec env, in the task's working directory
([where commands run](../guides/sandbox.md)).

### Bash

Runs a bash command and returns what it printed. The shell keeps its directory between calls.
A result longer than 30,000 characters is saved to a file and the model gets a preview.

| Parameter | Type | Meaning |
|---|---|---|
| **`command`** | string | the command line |
| `timeout` | number | milliseconds; default 120,000 (two minutes), at most 600,000 in the foreground |
| `description` | string | what it does, in plain words, for the person |
| `run_in_background` | boolean | run detached; the model gets an id at once and is told when it ends |
| `dangerouslyDisableSandbox` | boolean | ask to run outside the sandbox |

Permission: a command whose every part is read-only (`ls`, `cat`, `git log`, `rg`, …) and reads
only inside the working directories is allowed; in `acceptEdits` file commands (`touch`, `mkdir`,
`mv`, …) inside them are too; any other asks, offering a rule for its parts. In `auto` the
classifier answers. A command naming `.noeta` asks in every mode but `bypassPermissions`; one
writing a protected path asks in every mode, `bypassPermissions` included; one leaving the
sandbox asks whatever the allow rules say (`bypassPermissions` lets it run). With the sandbox on
and `autoAllowBashIfSandboxed` (the default), a command that runs inside the sandbox is allowed
outside `plan` mode. `Bash(...)`
rules are matched part by part ([rules](../guides/permissions.md#rules)). A background job keeps
its task's claim on the worker that runs it.

### Monitor

Runs a script, or connects to a WebSocket, in the background and turns every line it prints into a
notification for the model, which goes on working meanwhile.

| Parameter | Type | Meaning |
|---|---|---|
| `command` | string | the script; each stdout line is an event, its exit ends the watch |
| `ws` | object | a WebSocket to connect to instead |
| **`description`** | string | a short label shown with every notification |
| **`timeout_ms`** | number | when to stop (default 300,000; at most 1,800,000) |

Permission: as `Bash` for its `command` (a read-only one is allowed, others ask; `Bash(...)`
rules cover it); a `ws` watch asks.

### TaskStop

Stops a background task by id: a `Bash` job, a `Monitor`, a sub-agent, or a `Workflow` run.

| Parameter | Type | Meaning |
|---|---|---|
| `task_id` | string | the id |
| `shell_id` | string | older name for `task_id` |

Permission: allowed.

### EnterWorktree, ExitWorktree

`EnterWorktree` creates a git worktree on its own branch (or moves into an existing one) and moves
the task's shell into it; `ExitWorktree` returns the shell to where it was.

| Tool | Parameter | Type | Meaning |
|---|---|---|---|
| `EnterWorktree` | `name` | string | a name for a new worktree: letters, digits, `.`, `_`, `-`, segments joined by `/`, at most 64 characters (random when left out); its branch is `worktree-<name>` |
| | `path` | string | an existing worktree of this repository to move into instead (not with `name`) |
| `ExitWorktree` | **`action`** | string | `keep` leaves it on disk; `remove` deletes it and its branch |
| | `discard_changes` | boolean | remove even with uncommitted files or unmerged commits |

Permission: allowed, also in `auto` (no classifier). Claude Code asks before entering a worktree
outside `.claude/worktrees/`, because there it moves the session's write access; noeta's
`EnterWorktree` moves only the shell, never the permission scope (`parity/deviations.md`).

## Sub-agents: the `subtasks` plugin

`noeta/plugins/subtasks/`. Sub-agents are child tasks that run in the background
([sub-agents](../guides/subagents.md)).

### Agent

Starts a sub-agent. `subagent_type: "fork"` starts a copy of the caller that inherits the whole
conversation; any other type, or none (`general-purpose`), starts fresh.

| Parameter | Type | Meaning |
|---|---|---|
| **`description`** | string | a 3–5 word label |
| **`prompt`** | string | the task |
| `subagent_type` | string | which agent (from the listing in the conversation) |
| `model` | string | `sonnet`, `opus`, `haiku` or `fable`, over its definition's model (a fork ignores it) |
| `effort` | string | `low`, `medium`, `high`, `xhigh` or `max` (a fork ignores it) |
| `isolation` | string | `worktree`: a temporary git worktree; `remote`: elsewhere, where available |

Permission: allowed; in `auto` the classifier judges it. `Agent(Explore)` rules name a type.

### SendMessage

Sends a message to a sub-agent it started, to its caller (`main`, from a background sub-agent), or
to another task `ListAgents` shows.

| Parameter | Type | Meaning |
|---|---|---|
| **`to`** | string | an agent id or name, `main`, or a task id |
| `message` | string | plain text; its first line should stand on its own |
| `summary` | string | a 5–10 word label in the sender's transcript |
| `notify_when_idle` | boolean | ask another open task to report back once its turn ends |

Permission: allowed; classified in `auto`.

### ListAgents

Lists the agents `SendMessage` can reach: the sub-agents this task started and other open tasks in
the store, each with its kind and state. Offered only to a top-level task. Parameters `channel`
and `q` exist for Claude Code's schema and are not used. Permission: allowed.

### SubagentHandback

Offered only to a sub-agent in `auto` mode: delivers its final report to its caller and ends its
run.

| Parameter | Type | Meaning |
|---|---|---|
| **`message`** | string | the complete report |

Permission: allowed; the classifier reviews it in `auto`. `SubagentStop` hooks run on it.

## Skills: the `skills` plugin

### Skill

Loads a skill's instructions into the conversation ([skills](../guides/skills-and-agents.md#skills)).

| Parameter | Type | Meaning |
|---|---|---|
| **`skill`** | string | the name as the listing gives it, no leading slash |
| `args` | string | text passed to the skill (`$ARGUMENTS`) |

Permission: allowed; `Skill(name)` rules name one. A skill's `allowed-tools` pre-approve those
tools while it is active.

### run_skill_script

Runs a script an active skill ships in its directory, with no shell. Offered only when the skills
plugin's `scripts` argument is on.

| Parameter | Type | Meaning |
|---|---|---|
| **`skill`** | string | the active skill that ships the script |
| **`relpath`** | string | the script's path inside that skill's directory (`scripts/check.sh`) |
| `args` | array | the script's arguments, one string each, no shell metacharacters |

Permission: asks.

## Asking the person: the `ask_user` plugin

### AskUserQuestion

Asks the person one to four multiple-choice questions and waits for the answers. The task parks
while it waits and holds no worker.

| Parameter | Type | Meaning |
|---|---|---|
| **`questions`** | array | 1–4 questions; each has **`question`**, **`header`** (at most 12 characters), **`options`** (2–4, each **`label`**, **`description`** and an optional `preview`) and **`multiSelect`**; an "Other" choice is always added |
| `answers`, `annotations` | object | filled in by the front end |
| `metadata` | object | statistics only |

Permission: allowed. Not offered to a task nobody follows live (`-p`, `interactive: false`) unless
a client answers prompts.

## The web: the `web` plugin

`noeta/plugins/web/`. Both tools reach the network from the worker; they are the only built-in
tools that do, apart from MCP servers and your commands.

### WebFetch

Fetches a URL, turns the page into Markdown, and answers `prompt` about it with a model
(`webfetch_model`, default the task's): the model gets the answer, not the page. Pages are cached
15 minutes; HTTP is upgraded to HTTPS (loopback addresses excepted); a redirect to another host is reported back instead of
followed.

| Parameter | Type | Meaning |
|---|---|---|
| **`url`** | string | the URL |
| **`prompt`** | string | what to find out |
| `offset` | integer | where to continue in a page too long for one call |

Permission: asks per host, offering `WebFetch(domain:<host>)` as the "always" rule.

### WebSearch

Searches the web and returns titles, URLs and snippets. Offered only with a search key
(`NOETA_WEB_SEARCH_API_KEY`, or the plugin's `search_api_key`; Tavily).

| Parameter | Type | Meaning |
|---|---|---|
| **`query`** | string | what to search for (at least 2 characters) |
| `allowed_domains`, `blocked_domains` | array | keep, or leave out, these domains |
| `mode` | string | `standard` (quick) or `extended` (thorough, costs more; the default) |

Permission: asks.

## Plan mode: the `plan_mode` plugin

Offered only to a top-level task ([plan mode](../guides/permissions.md#plan-mode)).

### EnterPlanMode

Switches to plan mode. No parameters. Permission: asks you (firm) in `default` and `acceptEdits`;
allowed in `auto` and `plan`.

### ExitPlanMode

Presents the plan file for approval and, on a yes, leaves plan mode in the mode you pick.
`allowedPrompts` is accepted and not used. Permission: allowed; the tool itself parks on your
answer.

## Task list tools

`TaskCreate`, `TaskGet`, `TaskUpdate`, `TaskList` (the `todos` plugin) keep Claude Code's task
list in the task's log. Offered by default only to Claude models older than Claude Code's lean
prompt (and Bedrock application inference profiles), and only to a top-level task (`[plugins.todos] all_models = true` gives every model them;
[the task list](../guides/memory-and-instructions.md#the-task-list)).

| Tool | Parameters |
|---|---|
| `TaskCreate` | **`subject`**, **`description`**, `activeForm`, `metadata` |
| `TaskGet` | **`taskId`** |
| `TaskUpdate` | **`taskId`**, `subject`, `description`, `activeForm`, `status`, `addBlocks`, `addBlockedBy`, `owner`, `metadata` |
| `TaskList` | none |

Permission: allowed. All four are retried after a crash.

## Scheduling: the `schedule` plugin

Timers are kernel state, so a scheduled wake-up survives every worker.

### ScheduleWakeup

Sets when the next iteration of a self-paced `/loop` runs.

| Parameter | Type | Meaning |
|---|---|---|
| `delaySeconds` | number | seconds from now, clamped to 60–3,600 |
| `reason` | string | one sentence, shown to the person |
| `prompt` | string | the `/loop` input the wake-up fires |
| `stop` | boolean | end the loop instead |
| `noop` | boolean | nothing changed this iteration |

The first three and `noop` are required unless `stop` is true. The wake-up time in the result is
in the task config's `timezone` (else the machine's zone). Permission: allowed.

### Cron tools

| Tool | Parameters | What it does |
|---|---|---|
| `CronCreate` | **`cron`** (5 fields, in local time: the task config's `timezone`, else the zone of the machine that runs the task), **`prompt`**, `recurring` (default true; a recurring job expires after 7 days), `durable` (accepted; every job is durable) | queue a prompt on a schedule, or once |
| `CronList` | none | the task's jobs |
| `CronDelete` | **`id`** | cancel one |

Next-run times in the results are shown in that zone too. A task may have at most 50 jobs at
once. Permission: all three are allowed; in `auto` `CronCreate` goes to the classifier, the other
two do not.

## Workflow: the `workflow` plugin

### Workflow

Runs a JavaScript workflow script that drives many sub-agents in a fixed way (`agent()`,
`parallel()`, `pipeline()`, `phase()`), in the background; a notification arrives when it is done.

| Parameter | Type | Meaning |
|---|---|---|
| `script` | string | the whole script, starting with `export const meta = {…}` |
| `name` | string | a saved workflow in `.noeta/workflows/` or `~/.noeta/workflows/` |
| `scriptPath` | string | a script file to run (wins over `script` and `name`) |
| `args` | any | the script's global `args` |
| `resumeFromRunId` | string | reuse an ended run's unchanged `agent()` results |
| `description`, `title` | string | ignored: they live in the script's `meta` |

Permission: allowed; classified in `auto`.

## Findings: the `findings` plugin

### ReportFindings

Hands a code review's findings to the front end as a typed list, when the review instructions ask
for it.

| Parameter | Type | Meaning |
|---|---|---|
| **`findings`** | array | the verified findings, most severe first, at most 32; each has **`file`**, **`summary`**, **`failure_scenario`** and optionally `line`, `short_summary`, `category`, `verdict`, `outcome` |
| `level` | string | the effort the review ran at |

Permission: allowed.

## StructuredOutput

Offered when a task has an answer schema (`--json-schema`, the task config's `output_schema`): the
model hands in its final answer through it, and the call's input is the answer. Its input schema is
the schema you gave. Offered whatever the agent's tool lists say. Permission: allowed.

## ToolSearch

Added by the `tool_search` plugin while a task offers any deferred tool. Deferred tools are named
in the conversation; a call to one before it is loaded fails with a hint. `ToolSearch` loads them:

| Parameter | Type | Meaning |
|---|---|---|
| **`query`** | string | `select:WebFetch,NotebookEdit` loads those; words search names and descriptions; `+mcp resource` keeps names containing `mcp` |
| **`max_results`** | number | how many to load from a keyword search (default 5) |

A loaded tool stays loaded for the rest of the task, a compaction included. Permission: allowed.

## MCP tools

The `mcp` plugin ([MCP servers](../guides/mcp.md)) offers, all deferred:

| Tool | What it does | Permission |
|---|---|---|
| `mcp__<server>__<tool>` | one tool of a connected server | asks; `mcp__<server>` rules cover a server |
| `mcp__<server>__authenticate`, `mcp__<server>__complete_authentication` | in place of a server's tools while it needs a sign-in | asks |
| `ListMcpResourcesTool` | the servers' resources (`server` to narrow) | allowed |
| `ReadMcpResourceTool` | read one resource (**`server`**, **`uri`**) | allowed |
| `ReadMcpResourceDirTool` | list a directory resource, one level down (**`server`**, **`uri`**) | allowed |

The three resource tools are offered only to a top-level task, whenever a server is configured
(even one that is down). A server's tools are not read-only, whatever the server says. Server
tools are listed after the built-in ones.
