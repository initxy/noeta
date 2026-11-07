# Noeta

Durable AI agents: every task is an append-only event log with one writer, so a task survives
crashes, waits for a person at no cost, and runs on one process or many machines unchanged.

## Task

The only unit of work. A task has an agent, a config, an event log, an inbox and a row in
`tasks` (a projection of the log written in the same transaction). Sub-agents are child tasks
with `parent_id` / `root_id`; a task may start from another's conversation
(`TaskCreated.history_from`: a resumed or forked sub-agent). There is no session, run or workflow
entity. A store serves one party; there are no tenants (ADR-028).

## Event log

Ten event types (`noeta/core/events.py`). Plugin state is never a new event type: a plugin records
`ExtensionRecorded(extension, kind, data, message?)` and folds it in its own `reduce`. The core's
own entries use the same event under `extension="core"`, one typed payload per kind
(`noeta/core/records.py`), which the kernel writes and folds them through. The loop
state is a pure fold of the log; the model request is a pure function of that state. A claim
folds from the task's snapshot (`noeta/core/snapshots.py`) when one of its fold version exists.

## Inbox

The only way into a task: `Say`, `Cancel`, `Answer`, `ChildEnded`, `TimerFired`, `External`,
each idempotent on an optional `dedupe_key`. Front ends, parents, timers and plugins all post;
only the task's runner reads. A task never ends over an item it has not taken (the store
refuses the end, `InboxUnread`; the runner takes it and decides again). A plugin's background
model work comes back this way too (ADR-031).

## One writer

A runner claims a task from the store (`claim` / `renew` / `release`; Postgres uses
`SKIP LOCKED`). Every append is conditional on `expected_seq` and the claim's `owner_epoch`, so a
stale runner can never write. A tool call is logged as started before it runs; on recovery an
unfinished call is answered "outcome unknown", never re-run, unless its tool looks the outcome
up (`Tool.recover`, by `ToolContext.idempotency_key`) or is `retry_safe` (`ToolFlags`). A task that owns what lives only in
its worker process (a background job, a parked MCP call) keeps its own claim while it waits
(`ToolContext.keep`, `Patch.keep`): the row says waiting, the runner renews the lease and takes
the task's inbox in the background (`Runtime.run_claim`), so no other worker runs it. A lapsed
kept lease makes the task claimable, and the next claimer tells the model the job is lost;
a worker stopping (`Runtime.let_go`) leaves its tasks waiting, told with their next message.

## Layers

Top to bottom, imports only downward: `noeta/cli` (the `noeta` command, config) > `noeta/agents` (agent
files, built-in agents) > `noeta/plugins/<name>` (capabilities) | `noeta/backends` (providers, exec envs,
stores) | `noeta/host` (`LocalApp`, and the ways in: HTTP with `RemoteClient`, ACP) > `noeta/toolkit` (code
plugins share) > `noeta/core`. `|` siblings never import each other, nor do two plugins or two backends
(`lint-imports`). One plugin reaches another only through the kernel: a tool by name (`TaskView.find_tool`),
inherited config (`Extension.inherit`), runnable agents (`TaskView.agents`), or what a plugin publishes, read
only through a typed reader in `toolkit`: `plugin_records.py` (plugin names, each record and who reads it, the
methods a plugin keeps for others; `tests/toolkit/test_plugin_records.py` holds writers to readers),
`background_tasks.py`, `structured_output.py`, `permission_modes.py`. Front ends import no plugin; the TUI and ACP
read permissions' `mode` / `modes`, checkpoints' `snapshot`s, memory's `dir`, title's `title`, compaction's `threshold`, todos' records (kept by a replacement); the web app reads none (Plugin parts). SDK = all but `noeta/cli`.

## Ways in

One API, the client (`noeta/core/client.py`): create, send, answer, cancel, read, watch. HTTP
(`noeta/host/http.py`, `noeta serve`) is it on the wire; ACP (`noeta/host/acp.py`) and
`noeta/host/stream_json.py` (`noeta -p`, `/stream-json`) translate editors' and Claude Code's
headless protocols into it. A front end runs on `AppAPI` (`noeta/host/app.py`: client, agents,
commands, request preview), `LocalApp` in process or `RemoteApp` against a server (`--server`).
A new outside protocol is another translation in `host`, never a second API. A long-running
listener for an outside conversation (a chat) is a `Channel` (`noeta/core/channel.py`, group
`noeta.channels`), run by `noeta worker` / `noeta serve`; its posts are idempotent (`deliver`),
and replies go out from inside the task (a plugin).

## Where code goes

Ask in order, take the first yes: the kernel itself uses it -> `core`; an agent uses it in a
task -> a plugin; several plugins (or backends) share it -> `toolkit`; it is replaceable
infrastructure -> `backends`; it runs noeta or lets the outside in (a protocol) -> `host`; it is
an agent or the agent-file format -> `agents`; the `noeta` command -> `cli`; a front end -> its
own top-level project (`tui/`: `noeta-tui` on `AppAPI` only, found through `noeta.frontends`;
web, desktop: HTTP). `scripts/lint-layout.py` (in `make check`) fails on any other top-level
package and on a `core` module the kernel never imports, unless listed there as kernel
surface with a reason. Two rules for every tool and front end are the kernel's, not
capabilities: the runner's result limits, saving through the task's exec env
(`core/large_output.py`), and the client's image limits for every provider (`core/images.py`).

## Registry

Plugins and backends are found by name through entry points (`noeta/core/registry.py`): groups
`noeta.plugins`, `noeta.providers`, `noeta.execenvs`, `noeta.stores`, `noeta.channels`,
`noeta.frontends` (`run(app, FrontEndStart)`, `noeta/host/app.py`). The
built-ins register in `pyproject.toml` exactly as a third-party package would
(`examples/hello-plugin` is one, installed and tested by `make check`). A plugin's registry name
is its `Extension.name`. A plugin (or channel) can also be a plain file in `~/.noeta/plugins`, a
trusted project's `.noeta/plugins` or a `--plugin-dir` for one run
(`registry.load_plugin_files`); an installed plugin wins a name clash. Such a file joins the agents its `Extension.add_to` names by itself (an installed
package's `add_to` is ignored). `noeta plugin add <git-url>@<commit>` installs one there pinned
(also by name from a Claude Code `marketplace.json`, only read: `~/.noeta/marketplaces.json`, the official `noeta-plugins` by default), running nothing from it.

## Plugin parts

One plugin name, one switch, up to these parts (ADR-033): its `Extension`; UI data every front end
draws (`Extension.ui`, `GET /tasks/{id}/ui`); a web part named by `.noeta-plugin/plugin.json` beside
its code (`noeta/host/plugins.py`), imported by the web page with React and `@noeta-ai/*` taken from
its import map (`web/index.html`, `web/shared-modules.ts`). A built-in's web view is its part in
`web/src/plugins/<name>/` (`builtin.ts`), through the same slots; `web/src/plugins/boundary.test.ts`
fails a page file that reads a plugin's records or names its tool. Task config `plugins_off` (taken
only between turns, inherited by children): those plugins still fold their records but are asked
nothing else; requests may name only non-shipped plugins and `SWITCHABLE_BUILTINS`.

## Plugin pin

A task records the plugins it runs with (`records.Plugins`: each one's `registry.pin` — name,
source, version or file hash, arguments hash) when it first starts and whenever a pickup finds
them changed. A worker missing a recorded plugin never runs the task without it: it lets the
lease run out, and fails the task with `plugin_missing` after `LOAD_ATTEMPTS` of its own claims
in a row with the log unchanged: each worker counts in memory (`Runtime._plugin_missing`), so the
limit is per worker and a restart starts it over. A reload (the TUI's
`/reload-plugins`, `LocalApp.reload_plugins`; on servers a rolling restart) re-imports plugin
files and remakes the agents: a task runs the new code from its next turn, recorded so.

## Plugin setting

What a host says about one plugin, by name: its arguments (`enabled: false` among them switches
it off), a ready `Extension`, or on/off. One table at every level (`build_agents`,
`default_agents`, `from_settings`, `config.toml` `[plugins.<name>]`); argument tables of one
plugin merge key by key. `add` (`[agents.<name>] add_plugins`, `"*"` for all) appends plugins
to an agent without copying its file. The built-in agents keep memory and MCP off until set.
A secret is written as a reference, `{env:NAME}` or `{keyring:service/user}`, resolved only when
the plugin is made (`agent_files.resolve_secrets`).

## Config places

noeta reads only its own places: `~/.noeta/` (`NOETA_HOME`), a project's `.noeta/` (most of it
only once trusted), `NOETA.md` and the cross-tool `AGENTS.md`, and the machine's managed
directory (`/etc/noeta`; its `config.toml` `[policy]` allows and blocks plugins by name over
every other setting, `plugin_policy.PluginPolicy`). The mechanisms and formats inside are Claude Code's
(instruction layering, settings, skills, agents, `.noeta/mcp.json`, plugin directories:
`noeta/cli/claude_plugins.py`); its own places (`~/.claude/`, `.claude/`, `CLAUDE.md`,
`.mcp.json`, `CLAUDE_*` variables) are never read (`noeta/cli/settings_files.py`,
`noeta/plugins/instructions/files.py`). Trusted projects are listed in `~/.noeta/trusted.json`
(`noeta trust`); a trusted project's MCP servers start only with Claude Code's consent keys in
its own settings, so a cloned repository never starts a process by itself.

## Extension

The one capability-plugin interface (`noeta/core/extension.py`): tools, `tool_changes` (tools
computed from the others'), context parts, commands, reminders, `on_start`, `before_model`, `before_tool` (gates: Deny > Rewrite > Ask > Allow;
an approving Allow satisfies asks that are not firm), `resolve_ask` (answers asks before the
call parks), `after_tool`, `on_event` (at least once past `delivered_through`), `reduce`, `continue_state`, `inherit`, `on_context_overflow`,
`on_empty_reply`, `before_compact` (asked by the compacting plugin), `resident`, `lead_resident`. Every method is
optional. Commands (`commands(ctx)` / `run_command`) are slash commands front ends list and
expand through `noeta/core/commands.py`. A `before_model` that raises `TurnDone` ends the turn
without a model call (`/compact`); `RequestStale` has the request built again (a compaction).
A tool asks the person by parking on `ask(...)` (`noeta/core/questions.py`) and tells them
something with `notify`; neither reaches the model as the user.

## Deferred tool

A tool whose `flags` say `deferred=True` is named to the model (`TaskView.deferred_tool_names`) and
sent only once the model loads it with `ToolSearch`. The `tool_search` plugin
(`noeta/plugins/tool_search`) adds `ToolSearch` and withholds the tools not loaded through
`Extension.tool_changes`; a tool is loaded once a `ToolReference` result names it (recorded with
that result, so a compaction keeps it). Every agent built from agent files runs it. MCP tools are deferred by default
(ADR-017).

## Agent file

An agent is a markdown file (`noeta/agents/agent_files.py`): YAML frontmatter (Claude Code's
fields, plus `plugins` as names or `name: {arguments}` and `one_shot`) and the system prompt as
body. A sub-agent runs in the background; in auto mode it ends its run with `SubagentHandback`,
whose report reaches its caller as an `agent`-origin message (one that ends without it delivers
no report), elsewhere its last reply is the result in its completion notice
(`noeta/plugins/subtasks`). One plugin instance per name and arguments is shared
by every agent naming it. The built-ins are `noeta/agents/files/*.md`.

## Prompt name

Every model-facing text has a name: `system/<agent>`, `identity/<agent>`, `tool/<Tool>`,
`<plugin>/<key>`, `reminder/<key>`. The owning code holds the default; overrides (host prompt dirs <
`<cwd>/.noeta/prompts` < task config `prompts`) are recorded when the task first starts, so a
resumed task sends the same bytes (`noeta/core/prompts.py`).

## Reminder

Text for the model that is not the user speaking. A plugin returns `Reminder`s from
`reminders(view)`, called before every model call; the runner records them and puts them in
that request: `role="user"` ones as `system`-origin text in the user's turn, then `role="system"`
ones as system turns by `order` (adapters join adjacent system turns, as Claude Code sends its
environment and budget notes). Text fixed at task start is captured in `on_start`; what must
wake a waiting task goes through the inbox.

## Park

A tool call that waits (a question, an approval, children) parks: the task ends its step with
`Waiting` and holds no worker. An `Answer` or `ChildEnded` in the inbox resumes it. A reply's
calls are gated in order and gating stops at the first ask: the calls after it wait ungated.

## origin

A message's `origin` says who speaks in the user channel: `human` (the person), `system` / `memory` for
text the host or a plugin adds (reminders, notices, recalled memory), or `agent` for another
agent's framed message (a sub-agent's hand-back, sent as plain text), or `inline` for host text
joined into the person's turn as plain text (a typed command's skill body, the interrupted
marker). Providers render `system` / `memory` as
`<system-reminder>` blocks (Anthropic) or with a preamble saying it is not the user (OpenAI
shapes); user text cannot forge the tag. `system` text taken into a running turn (a job's end
notice) folds as a system turn, unless it would directly follow the model's reply.

## Naming: Session is a scope, never an identity

No `session_id`, session list or session-keyed state in `noeta/`; use `task_id` or `root_id`.
Exempt: the ACP adapter, which uses the protocol's own names.
_Avoid_: `SessionStore`, `ConversationManager`, `class Session`

## Naming: Run, Workflow, Mutator, Pattern

Not concepts — say Task. A fixed procedure is an agent plus sub-agents.
_Avoid_: `class Run`, `class Workflow`, `WorkflowRunner`, `class Mutator`, `class Pattern`

## Naming: model-visible tool names

Tool names the model sees follow Claude Code (`Read`, `Edit`, `Write`, `Bash`, `Agent`,
`AskUserQuestion`, `Skill`, `ToolSearch`, `WebFetch`, `Workflow`, …) because models carry their
strongest prior on those names; they are exempt from the bans above. Everything else is
snake_case.

## Naming: enforcement

`scripts/lint-naming.py` (in `make check`) rejects the banned class names and identifiers above
everywhere, and compound `session` identifiers inside `noeta/`. Files that state the bans
(`CONTEXT.md`, `AGENTS.md`, `CHANGELOG.md`, the script) are exempt.
