# The plugin system

Everything noeta does beyond running a task's log is a plugin: the file tools, the shell,
permissions, hooks, skills, sub-agents, MCP, memory, compaction, the environment and instruction
files, deferred tool loading. This page explains the one interface they all implement, when each
of its hooks runs, how a plugin keeps state that survives crashes and moves between machines, how
plugins are found and set, and how a task pins the plugins it runs with. For a hands-on start see
[Write a plugin](../guides/plugins.md); for every signature see the
[Extension interface reference](../reference/extension.md).

## One interface

A capability plugin is a subclass of `Extension` (`noeta/core/extension.py`). Every method is
optional: a plugin that only adds a tool implements `tools`, one that only gates calls implements
`before_tool`. Providers, exec envs, stores and channels are backends with their own small
protocols, not plugins.

Why one interface ([ADR-002](https://github.com/initxy/noeta/blob/main/docs/adr/002-one-extension-interface-and-entry-points.md)):

- **A third-party plugin has every power a built-in has.** The 27 built-in plugins register
  exactly as an outside package would, so they prove the interface is enough. An interface per
  feature (one for tools, one for hooks, one for prompts) would mean more contracts and uneven
  powers.
- **The kernel carries no capability.** A change to the kernel needs a reason no plugin can meet.
- **Plugins never import each other.** They meet only through the kernel: a tool by its
  model-visible name (`TaskView.find_tool`), config handed to child tasks (`Extension.inherit`),
  the agents a worker can run (`TaskView.agents`), or a record another plugin publishes, read
  through a typed reader in `noeta/toolkit/plugin_records.py`. `lint-imports` enforces it. Each
  plugin can be replaced or switched off on its own.
- **No hook rewrites the raw provider payload,** and plugin code is not sandboxed in process (not
  achievable in CPython); isolation lives where it is real: the exec env, MCP servers, command
  hooks.

## Where the hooks run

<Diagram name="plug-hooks" caption="The hooks of one model call, and the ones around it" />

| Hook | When it runs | What it can do |
|---|---|---|
| `on_start(view)` | Each time a runner picks the task up, before its first step | Capture what must be read once (instruction files, the environment) and record it |
| `tools(view)` | Every step | Offer tools; the set may change from step to step. The first plugin offering a name wins |
| `tool_changes(view, offered)` | Every step, after every `tools` | Add tools built from the others', withhold offered ones (each with the error a call gets), add request entries that are no tool |
| `context(view)` | Every model call | System prompt text (`ContextPart`), inside the one cached block |
| `reminders(view)` | Before every model call | Notes recorded now and put in this request (`Reminder`) |
| `before_model(view, request)` | Before every model call, after the reminders | Return a changed request; raise `TurnDone` (no call, the turn ends), `RequestStale` (build the request again) or `TurnFailed` |
| `on_context_overflow(view, request)` | The model refused the request as too long | Make room (a compaction) and return `True`; the call is made again |
| `on_empty_reply(view, response)` | The reply had no text and no tool call | Return a message to go on with |
| `before_tool(view, call)` | Each tool call, in reply order | `Allow`, `Deny`, `Rewrite`, `Ask`, or `None` |
| `resolve_ask(view, call, asks)` | A call is about to park on an ask | Answer it in the person's place (a classifier, a hook, a callback) |
| `after_tool(view, call, output)` | After a call ran, before its result is recorded | Change the result |
| `on_event(view, recorded)` | After each appended event | Metrics, audit, bridges; `delivered_through` makes it at least once |
| `reduce(state, kind, data)` | When the log is folded | Fold this plugin's own records into its state |
| `continue_state(state)` | A task continues this one's conversation (a fork, a resumed sub-agent) | Choose what of the state carries over |
| `inherit(state)` | A child task is created | Config every child starts with |
| `before_compact`, `resident`, `lead_resident` | Asked by the compaction plugin | Add summary instructions or refuse; give back text a compaction must keep |
| `commands(ctx)`, `run_command(ctx, name, args)` | A front end lists or runs slash commands | Offer `/name` commands (`noeta/core/commands.py`) |

**Gating a call.** Every plugin is asked about the exact call; a `Deny` wins, then the first
`Rewrite`, then the asks (`gate` in `noeta/core/runner/calls.py`). A rewritten call is gated again,
so a person approves the call that really runs (at most three rewrites). An `Allow(approve=True)`
(a hook that vouches for the call) satisfies the other plugins' asks unless one is `firm`.
Before the call parks, each plugin's `resolve_ask` may answer. The calls of a reply are gated in
order and gating stops at the first ask; the calls after it wait ungated until it is answered.

## Tools

A tool is any object with `name`, `description`, `input_schema`, `read_only`, `clearable` and an
async `run(input, ctx)` returning a `ToolOutput` (`noeta/core/tool.py`); `function_tool` makes
one from a typed Python function (`noeta/toolkit/function_tool.py`). The kernel treats a tool by
a few rules:

- Its input is checked against its schema before any gate, unless its flags say
  `unchecked_input` (an MCP server's tools).
- Read-only calls next to each other in one reply run together, up to ten at once; every other
  call runs alone, in order.
- A tool that must wait returns `ToolOutput(park=...)`: `ask(...)` for a question to the person,
  or `Park(on="children")` for child tasks. The task then holds no worker, and the tool's `resume`
  gets the answer ([waiting](./waiting.md)).
- A call is logged as started before it runs. After a crash an unfinished call is answered
  "outcome unknown" unless the tool's `recover` looks the outcome up by
  `ctx.idempotency_key` (`<task id>:<call id>`, the same on every worker) or its flags say
  `retry_safe` ([recovery](./recovery.md)).
- Its text result is held to the [long-output rule](./context.md#long-tool-outputs).

`ToolFlags` carries the rest: `deferred` (loaded through `ToolSearch`), `listed_last`,
`run_last`, `past_agent_lists`, `retry_safe`, `silent_reply_after`, `unchecked_input`,
`safe_while_planning`, `max_result_chars`, `search_hint`, `loaded_description`. `ToolContext`
gives a running tool the exec env, `record`, `spawn`, `post`, `keep` (hold the task's claim for
work that lives in this process), `complete` (call the model, retried, not a step), `progress`
(live output, never recorded) and more; all are listed in the
[reference](../reference/extension.md#toolcontext).

## Plugin state lives in the log

<Diagram name="plug-state" caption="A record goes into the log with the step and is folded by the plugin's own reduce" />

A plugin never adds an event type. It records `view.record(name, kind, data)` (a tool:
`ctx.record(...)`), which the runner appends as an `ExtensionRecorded` event right after the hook
returns, in the same append as the step's other events, so a crash keeps all of them or none. On
every fold the plugin's `reduce(state, kind, data)` turns its records into its state, which hooks
read back with `view.ext_state(name)` and tools with `ctx.state[name]`.

- **Why.** The state is durable and the same on every machine for free: any worker that claims
  the task folds the same records, and the [snapshot](./event-log.md) holds plugin states too.
  A plugin that kept state in memory would lose it on a crash and differ between workers.
- **`message=`.** A record can carry a message, which joins the conversation (how instruction
  files, notices and reminders get there).
- **`folds = ("other",)`.** A plugin also folds records written under another name, for
  records front ends read by a fixed name (plan mode keeps its switches under `permissions`).
- **Erasing.** When a blocked prompt is erased, every reducer is called with
  `records.ERASED_KIND` and the erased seqs, so a plugin takes back what it recorded for those
  messages ([ADR-026](https://github.com/initxy/noeta/blob/main/docs/adr/026-a-blocked-prompt-is-erased-by-a-core-record.md)).
- **Continuing.** `continue_state` decides what a fork or a resumed sub-agent starts from; what
  belongs to the source's run (its timers, running jobs) stays there.
- **The kernel does the same.** Its own entries are `ExtensionRecorded` under
  `extension="core"`, one typed payload per kind (`noeta/core/records.py`).

A plugin that runs model work in the background records only a job id, and has the result come
back through the task's inbox, where `on_event` records it
([ADR-031](https://github.com/initxy/noeta/blob/main/docs/adr/031-background-plugin-work-returns-through-the-inbox.md);
compaction's [summary written ahead](./context.md#writing-the-summary-ahead) is the example).

## Finding plugins

<Diagram name="plug-registry" caption="From where a plugin's code is to the task that records it" />

Plugins and backends are found by name through Python entry points (`noeta/core/registry.py`):

| Group | A name points at |
|---|---|
| `noeta.plugins` | an `Extension` class (or factory); its registry name is its `Extension.name` |
| `noeta.providers` | `factory(**kw) -> Provider`, by provider family |
| `noeta.execenvs` | `factory(cwd, **kw) -> ExecEnv` |
| `noeta.stores` | `factory(url) -> Store`, by URL scheme |
| `noeta.channels` | a `Channel` class |
| `noeta.frontends` | `run(app, start)`, a front end the `noeta` command opens |

The built-ins register in `pyproject.toml` exactly as a third-party package would;
`examples/hello-plugin` is such a package, installed and tested by `make check`.

**Plugin files.** A plugin can also be one `*.py` file, or a directory with `__init__.py`, in
`~/.noeta/plugins`, a trusted project's `.noeta/plugins`, or a `--plugin-dir` for one run
(`registry.load_plugin_files`). Every `Extension` (or `Channel`) subclass with a `name` it
defines registers under that name. An installed package wins a name clash. A file that fails to
import is remembered with the reason and named when its plugin is asked for; one that imports a
module noeta's environment lacks is listed with the install line that adds it. A plugin file joins
the agents its `add_to` names by itself (`"*"` for every agent); an installed package's `add_to` is
ignored, so a dependency never changes your agents by being installed.

**Reloading.** `/reload-plugins` (`LocalApp.reload_plugins`) forgets the plugin files, imports
them again from source, reads the settings files again (their plugin settings apply; a change to
`env`, `model`, `agent` or `sandbox` is reported as needing a restart) and remakes the agents. A turn already running finishes on the old code;
each task runs the new code from its next turn, recorded as such. On servers a reload is a
rolling restart of the workers.

**Claude Code plugin directories.** A directory under `~/.noeta/plugins` (or a trusted project's
`.noeta/plugins`) with `.claude-plugin/plugin.json` or Claude Code's layout is loaded through the
plugins that read those formats (`noeta/cli/claude_plugins.py`): `skills/` by the skills plugin,
`agents/` as agent files, `commands/` by `markdown_commands`, `hooks/hooks.json` by hooks,
`.mcp.json` by MCP (never from a project's plugin, so a cloned repository starts no server by
itself). Its skills, agents and commands are named `<plugin>:<name>`, and
`${CLAUDE_PLUGIN_ROOT}`, `${CLAUDE_PLUGIN_DATA}` and its `userConfig` values are substituted as
Claude Code does. Monitors, `bin/`, LSP servers, themes and output styles are not loaded.

**Installing.** `noeta plugin add <git-url>@<commit>` (or a file, or an entry of a Claude Code
`marketplace.json`, which is only read) copies a plugin into `~/.noeta/plugins` pinned to that
commit. Installing runs nothing from the plugin: no install scripts, no git hooks, no dependency
installs.

## Front-end parts

A plugin also carries what front ends show of it, so one switch takes its tools and its UI
away together ([ADR-033](https://github.com/initxy/noeta/blob/main/docs/adr/033-one-plugin-carries-its-front-end-parts-and-is-switched-per-task.md)):

- **UI data** (`Extension.ui`) is computed from the folded state, like reminders, and every
  front end draws it, the terminal UI included, without running any plugin code.
- **A web part** is a built module the web and desktop apps import from the server at run
  time. The page hands it React and noeta's web SDK through an import map, so every plugin
  and the page share one copy of each, and the plugin names no host dependency of its own.
- **The built-ins' views are web parts too** (`web/src/plugins/<name>/`), through the same
  slots, so the slot API is proven the way the built-in backends prove `Extension`.
- **A task's switch lives in its log**: the `plugins_off` config key. A plugin named there is
  asked nothing from the next turn on but still folds its records, so the task's state and
  snapshots do not depend on it and switching back loses nothing.

Rejected: a plugin system per front end, plugins bundling their own React, iframes, and a
hosted marketplace (a marketplace is a static file whose entries pin commits).

## Plugin settings

What a host says about one plugin, by name: its arguments, a ready `Extension`, or on/off. There
is one table at every level (`build_agents`, `default_agents`, `from_settings`, `config.toml`
`[plugins.<name>]`), and the argument tables of one plugin merge key by key:

```toml
[plugins.compaction]          # arguments, by the plugin's name
auto_compact_clear_results = true

[plugins.web]
enabled = false               # off for every agent

[agents.main]
add_plugins = ["my_plugin", {hooks = {}}]   # after the agent file's own; "*" = every agent
```

- An agent file lists its plugins in its frontmatter, as names or `name: {arguments}`
  (`noeta/agents/agent_files.py`). Every agent built from agent files also gets `tool_search` and
  `structured_output` (`ALWAYS_PLUGINS`). The built-in agents keep `memory` and `mcp` off
  until set.
- One plugin instance per name and arguments is shared by every agent that names it.
- A secret is written as a reference, `{env:NAME}` or `{keyring:service/user}`, and resolved only
  when the plugin is made; the reference, not the secret, is what the pin hashes.
- The machine's `/etc/noeta/config.toml` `[policy]` (`allowed_plugins`, `blocked_plugins`)
  wins over every user and project setting.

The arguments of each built-in plugin are in [built-in plugins](../reference/plugins.md).

## The plugin pin {#the-plugin-pin}

A task records the plugins it runs with (`records.Plugins`) when it first starts and whenever a
pickup finds them changed. Each entry is `registry.pin(extension)`: the name, where the code comes
from (`package <dist>`, `file`, or `class <module.qualname>`), its version (the distribution's, or
a hash of the file's bytes, or of every `*.py` of a directory) and a hash of the arguments as
written.

A worker that lacks a recorded plugin never runs the task without it (`TaskRun._plugin_missing`,
`noeta/core/runner/task.py`): it lets its lease run out, so a worker that has the plugin can take
the task, and after three such claims in a row with nothing new in the log it fails the task with
`plugin_missing`. A plugin present in another version is recorded again and logged, so different
code on two workers for one task shows in its log, never silently.

Why: a fleet with mismatched plugins would otherwise send different requests for the same task
from different workers. Failing loudly is better than a task that behaves differently depending
on which machine picked it up. The snapshot's version covers the plugins too, so a snapshot made
by other plugin code is ignored and the log folded from the start.

## When a hook raises {#when-a-hook-raises}

What the runner does with an exception a hook lets out (`noeta/core/runner/task.py` `_steps`,
`calls.py`, `model.py`, `closing.py`). `TurnFailed`, `TurnDone` and `RequestStale` from
`reminders` or `before_model`, and `TurnFailed` from `on_context_overflow`, are not failures:
they fail the turn with their reason, end it, or build the request again.

| Hook | When it raises |
|---|---|
| `before_tool`, `resolve_ask` | The call is denied (`permission check failed: …`), as the model sees it |
| A tool's `run`, `resume` | The call's result is an error naming the exception; what the call staged (records, children) is dropped |
| A tool's `recover` | Logged; the outcome counts as unknown (a `retry_safe` tool runs again) |
| `after_tool` | Logged; the result stays as this hook was given it |
| `on_event`, `resident`, `lead_resident`, `on_empty_reply`, `commands`, `before_compact` | Logged and skipped: that plugin has no say this time |
| `run_command` | Raised to whoever ran the command |
| `inherit` | Raised from `view.spawn` or `ctx.spawn`, to the hook or tool that spawned |
| `reduce`, while the task is loaded | The task is claimed again once its lease runs out; after three such claims in a row it ends failed |
| Anything else in a step: `on_start`, `tools`, `tool_changes`, `context`, `reminders`, `before_model`, `on_context_overflow`, `reduce` | The turn fails (`internal_error`); a second failure in a row ends the task failed |

The rule behind the table: a broken check refuses rather than allows, a broken observer is
skipped, and a bug that would otherwise fail on every lease ends the task instead of leaving it
`running` forever.
