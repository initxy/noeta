---
status: active
created: 2026-10-27
branch: v1-layout
---

# Noeta 1.0 layout: kernel, plugins, prompts, reminders

## Goal

Make the repository the 1.0 package alone, and make its structure show what is kernel and what is
a plugin: the kernel carries no capability, every capability is a plugin found the same way a
third-party one is, prompt text is named and overridable in layers, and Claude Code-style
reminders are one kernel mechanism instead of ten hand-rolled ones.

## Non-goals

- Compatibility with 0.6 (code, stores, APIs). 0.6 stays reachable at tag `v0.6.42`.
- Migrating host-app.
- Splitting plugins into separate distributions (each is written so it could be).
- Declarative Claude Code plugin bundles (a directory of skills, agents, commands and MCP
  config with no Python); later, on top of the agent and command files below.

## Context

On `main` after the `next` merge, 0.6 (`packages/noeta-sdk`, `packages/noeta-runtime`, root
`tests/`, docs, examples) and 1.0 (`packages/noeta`, its own venv) sit side by side. Inside 1.0
the `Extension` interface exists, but:

- the kernel imports `noeta.tools.base` and `noeta.ext.base`, which live next to capability code;
- plugins import each other (`subtasks` -> `fs`, `permissions`, `instructions`, `workspace`;
  `ask_user` -> `permissions`);
- which plugins an agent has is hard-coded in `presets/__init__.py`; there is no discovery;
- prompt text is read by twelve separate loaders with fixed paths; the memory policy is glued onto
  the main prompt by string concatenation in the preset; `Extension.context` has no user;
- reminders (origin `system` messages) are recorded by each plugin with its own timing logic.

## Decisions

- **Repository = the 1.0 package.** 0.6 code, tests, docs, examples and the 0.6 eval engine are
  deleted. The package moves to the repository root (`noeta/`, `tests/`, one `pyproject.toml`).
- **Layers**, imports only downward, enforced by `lint-imports`:
  - `noeta.core` — types, events, errors, loop, runner, store interface + sqlite, client, and
    every interface a plugin implements or uses (extension hooks, tools, gates, provider, exec
    env, prompts, reminders, agent definitions, the plugin registry).
  - `noeta.plugins.<name>` — capability plugins (`Extension`). Each imports `noeta.core` only,
    never another plugin.
  - `noeta.backends.<name>` — providers, exec envs, the Postgres store. Import `noeta.core` only.
  - `noeta.presets` — agent definitions and prompts as data.
  - `noeta.apps` — `cli`, `tui`, `acp`, `server` / remote client. They use the client API.
- **Two kinds of plugin, one registry.** Capability plugins are per task and keep state in the
  log; backends are per process. Different interfaces, same discovery: entry point groups
  `noeta.plugins`, `noeta.providers`, `noeta.execenvs`, `noeta.stores`. Built-ins register in
  this package's `pyproject.toml` exactly as a third-party package would.
- **No plugin-to-plugin imports.** What one needs from another goes through `core`: the shared
  piece moves down (for example the approval gate types), or it is read from task config or the
  agent definition (for example a sub-agent's plugin list).
- **Agent files use YAML frontmatter** (PyYAML is now a dependency), so plugin arguments can be
  nested: `- fs: {tools: [Read, Grep, Glob]}`. One plugin instance per (name, arguments) per
  build, shared by every agent naming it; host arguments (`config`) lie under the file's; host
  instances are used where a file gives no arguments. A file without `plugins` gets the
  general-purpose set. Sub-agent rosters name agents (`subtasks: {agents: [...]}`) and read
  their descriptions from `TaskView.agents`.
- **Agents are definitions, not code.** An agent is a markdown file: frontmatter (`name`,
  `description`, `model`, `plugins`, `tools`, …) and the system prompt as body. Built-in agents
  (`main`, `general-purpose`, `explore`, `plan`) are such files in `presets`; `.noeta/agents/` and
  `~/.noeta/agents/` add or replace agents. Claude Code's agent file format is accepted.
- **Prompts are named and layered.** Every prompt text has a name (`main`, `tool/Read`,
  `memory/policy`, `compaction/summarize`, `reminder/todo`, …). One loader in `core` resolves a
  name through: the owning plugin's default < preset < `~/.noeta/prompts/` < `.noeta/prompts/` <
  task config. `<name>.md` replaces, `<name>.append.md` appends. `noeta` gains
  `--system-prompt` and `--append-system-prompt`.
- **Prompts are fixed at task start.** The resolved texts a task uses are recorded in its log when
  it starts, so a resumed task sends the same bytes; edits apply to new tasks.
- **The system prompt is assembled from parts.** Agent prompt, then each plugin's `context()`
  parts in order, joined by a newline into one block (a part with `cache` ends a block). The
  memory policy becomes the memory plugin's `context()`.
- **Defaults stay with their owners.** Each plugin keeps its own default texts; only the lookup is
  shared (`TaskState.prompt` / `TaskView.prompt` / `ToolContext.prompt`; tool descriptions in
  `build_request`). Named so far: `system/<agent>`, `tool/<Tool>`, `compaction/summarize`,
  `memory/policy`, `memory/policy-read-only`, `memory/recall_judge`, `web/fetch_digest`.
- **The override record is always written** (`core` / `prompts`, possibly empty) on the first
  start, so "fixed at start" holds even when no file existed then.
- **Reminders are one kernel mechanism.** `Extension.reminders(view)` is called before every
  model call, after every `before_model`, on the state those left; the plugin decides "when"
  from that state and returns `Reminder(text, kind, data, origin)`s. The runner records them and
  splices them into that request after the recorded messages, with the breakpoints a fresh fold
  gives, so no plugin patches requests. A reminder's fixed text is a named prompt
  (`reminder/<name>`) the plugin resolves. Text recorded once at task start (environment,
  instructions, skill listing, memory index) stays in `on_start`; events that must wake a
  waiting task (a background job finished) keep going through the inbox.
- **New reminders** (feature commits, after the structural ones): file changed on disk since the
  model read it; date changed; a `hooks` plugin whose user-configured commands run on
  user-prompt-submit, before/after tool, and turn stop, and may add context or block.
- **Structure and behavior are separate commits.** Every structural step keeps
  `tests/test_request_golden.py` byte-identical.

## Plan

- [x] Golden test of the default agents' request bytes (all three adapters)
- [x] Delete 0.6 (packages, tests, docs, examples, eval engine, CI legs); move 1.0 to the root;
      one `make check`
- [x] `noeta.core`: move the interfaces down; `plugins/`, `backends/`, `apps/`; import-linter
      contracts (core imports no plugin/backend/app; plugins and backends import only core)
- [x] Remove plugin-to-plugin imports
- [x] Entry point registry; `Runtime` and the CLI resolve plugins/backends by name
- [x] Prompt overrides with layers, resolved at one point (`TaskState.prompt`), recorded at task
      start
- [x] Agent definition files; presets as data; `.noeta/agents/`; memory policy via `context()`
- [x] `reminders()` in the kernel; port the todo reminder
- [x] CLI `--system-prompt` / `--append-system-prompt`
- [x] Feature: file-changed reminder; date-change reminder; `hooks` plugin with context and block
- [x] CI, release workflow, Makefile, naming lint; README / AGENTS.md / CONTEXT.md for 1.0
- [ ] `make check` green; `make eval` no regression

## Acceptance criteria

- The repository contains no 0.6 code; `make check` is one pass over one package.
- `lint-imports` enforces the layer rules above, including no plugin-to-plugin imports.
- Removing any one directory under `noeta/plugins/` leaves the rest importable and their tests
  passing (except tests of presets that list it).
- A third-party package exposing an entry point in `noeta.plugins` is usable from an agent file
  by name with no code change in Noeta.
- Any prompt text can be replaced or appended to by a file in `.noeta/prompts/`; a running task
  is unaffected by such an edit.
- `tests/test_request_golden.py` is unchanged through every structural commit.
- `make eval` shows no regression versus the current 0.982.

## Risks

- **Silent prompt drift** while moving prompt loading. Mitigation: the golden test.
- **Over-abstracting the registry.** Mitigation: entry points plus a dict; no plugin manager.
- **Big-bang diff.** Mitigation: one commit per Plan line, each green.

## Progress log
2026-10-27 owner: drop 0.6 entirely, no compatibility; every capability a plugin; prompts and reminders extensible. Golden request test committed (a768a70).
2026-10-27 0.6 deleted, 1.0 at the root; one make check (1300 passed). A root `.ignore` (listing tests/) had kept ruff off the 1.0 tests: removed, lint debt fixed. 0.6 byte-parity tests dropped (the golden covers the bytes).
2026-10-27 Layered: core / plugins / backends / presets / apps, tests mirror it; four lint-imports contracts kept. Plugin links cut through the kernel: `TaskView.find_tool(name)` (compaction's file restore uses the task's `Read`), `Extension.inherit(state)` (permission rules reach children), `core.pending` (open approvals/questions), `core.execenv.is_local` / `scrub_env`, `core.registry` (entry points; the kernel's default exec env). Bash moved from `fs` to the `shell` plugin with its own state; sub-agents get a foreground `Shell` when the host passes none. Golden unchanged.
2026-10-27 Prompt overrides landed: host dirs (`Runtime.prompt_dirs`, the CLI passes `~/.noeta/prompts`) < `<cwd>/.noeta/prompts` < config `prompts`; recorded once per task. `--system-prompt` / `--append-system-prompt` via `Settings.prompts` -> `LocalClient(task_config=...)`; CLI and TUI share `apps.local.from_settings`. Memory policy moved to `Memory.context()`; `with_memory_policy` removed. Golden unchanged.
2026-10-27 Agent files landed: `noeta/presets/agents/{main,general-purpose,explore,plan}.md` (bodies byte-identical to the old prompts), `core.agent_files` (parse, read a directory, build), `Agent.description` / `allowed_tools`, `TaskView.model` / `agents`, `FileTools(tools=[names])`, extension names equal registry names (`environment`, `structured_output`). The CLI and TUI read `~/.noeta/agents` and `<cwd>/.noeta/agents`. Host extensions now go last on every agent (they were mid-list on sub-agents). Golden unchanged.
2026-10-27 `Extension.reminders` landed; the todo reminder uses it (its before_model request patching and `simulated_state` use are gone). `cache_breakpoints` is public; `TaskView.allowed_tools` makes `find_tool` honor the agent allowlist. Golden unchanged (it contains a todo reminder).
2026-10-27 Feature: file-changed reminder (fs plugin, turn start, last 20 read files, mtime then sha256, once per change, `reminder/file_changed`) and date-change reminder (environment plugin, turn start, `reminder/date_changed`). Context-plugin tests live in `tests/plugins/context`.
2026-10-27 Feature: `hooks` plugin (Claude Code command hooks: UserPromptSubmit, PreToolUse, PostToolUse, Stop, Notification; exit 2 / JSON decisions; `~/.noeta/settings.json` via the CLI). It replaces the permissions plugin's side-effect-only `Hooks` (PostToolUse / Notification rules); declarative PreToolUse rules stay in `Permissions`. Hook commands run on the worker host, not in the task's exec env. Payload key is `task_id` (no session id).
2026-10-27 `noeta` top-level names (lazy: Agent, Client, Runtime, Extension, Reminder, TaskView, Tool*, default_agents, provider, store, connect, agent-file helpers). README "Extend it" and CONTEXT entries (Layers, Registry, Agent file, Prompt name, Reminder). Checked: each plugin blocked in turn, every other plugin imports and constructs.
