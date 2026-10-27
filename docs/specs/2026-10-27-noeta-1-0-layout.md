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
  parts in order. The memory policy becomes the memory plugin's `context()`.
- **Reminders are one kernel mechanism.** A plugin declares reminders (`reminders(view)`: name,
  text, when: every user turn / every N assistant turns / after an event / after compaction /
  once). The runner records them as `system`-origin messages at one place in the request, with
  repeat suppression. Their text is a named prompt. Events that must wake a waiting task
  (background job finished) keep going through the inbox.
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
- [ ] Entry point registry; `Runtime` and the CLI resolve plugins/backends by name
- [ ] Prompt loader with layers; all twelve loaders go through it; prompts recorded at task start
- [ ] Agent definition files; presets as data; `.noeta/agents/`; memory policy via `context()`
- [ ] `reminders()` in the kernel; port todo, skills, memory, instructions, environment,
      compaction-restore reminders
- [ ] CLI `--system-prompt` / `--append-system-prompt`
- [ ] Feature: file-changed reminder; date-change reminder; `hooks` plugin with context and block
- [ ] CI, release workflow, Makefile, naming lint; README / AGENTS.md / CONTEXT.md for 1.0
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
