---
status: active
created: 2026-10-27
branch: v1-layout-split
---

# Layout split: a plugin toolkit, stores as backends, host pieces out of apps

## Goal

Make every top-level package of `noeta/` mean one thing, so "where does this code go" has one
answer and a check catches the wrong one:

- `noeta.core` holds only what the kernel itself uses and the interfaces plugins and front
  ends implement or call. Today about 750 lines of plugin code sit there (`shell.py`,
  `tool_helpers.py`, `helpers.py`) because the kernel was the only legal place for code two
  plugins share.
- The SDK is everything except `noeta.apps`: an embedder or a desktop / web front end never
  imports `noeta.apps` (today `LocalApp`, `RemoteClient` and the HTTP server live there).
- `noeta.backends` is three kinds of replaceable infrastructure, laid out alike, and a third
  party writing a store can run the same contract tests ours pass.

## Non-goals

- Any behavior change. This is a structure-only diff (AGENTS.md: never mix the two).
- Compatibility: no forwarding modules, aliases or re-exports at old paths. 1.0 is unreleased;
  host-app is on 0.6 and unaffected.
- New extension points (HTTP endpoints for commands and pending questions, TUI extension
  points, `noeta plugin install`, timers). Separate specs.
- Renaming `noeta.apps`: after the split it holds the `noeta` command (`worker` and `serve`
  included, which are not front ends) and the front ends; the name still fits.

## Context

Measured on `main` @e828f2e:

| Module | Lines | Imported by the kernel | Used by |
|---|---|---|---|
| `core/shell.py` (shell command classification) | 394 | no | shell, permissions plugins |
| `core/tool_helpers.py` (paths, errors, large output) | 243 | no | fs, shell, web, … (16 files) |
| `core/helpers.py` (token estimate, resident budget) | 116 | no | instructions, skills, memory |
| `core/models.py` (model catalog, prices) | 493 | yes: `runner/model.py` (default output limit) | providers, compaction, permissions, config |
| `core/store/sqlite.py` | 515 | no (registry entry point only) | — |

`lint-imports` checks import direction only, so nothing stopped shared plugin code from landing
in `core`. AGENTS.md says "respect the layering" in one line; CONTEXT.md's Layers entry lists
packages but not how to choose one. `noeta.apps` mixes front ends (`cli`, `tui`, `acp`) with
pieces an embedder needs (`local.py`, `remote.py`, `server.py`); `local.from_settings` reads
`~/.noeta` and is the only part of `local.py` that needs `apps.config` and `presets`.
`noeta.presets` is the four built-in agent files plus `default_agents`, `DEFAULT_PLUGINS` and
`OFF_UNLESS_SET`. The store contract tests (18) live in `tests/core/store/`, out of the wheel.

## Decisions

- **Packages and layers** (imports only downward; `|` = siblings that do not import each other):

  | Package | What goes there |
  |---|---|
  | `noeta.apps` | the `noeta` command, TUI, ACP, `~/.noeta` config (`config.py`, now with `from_settings`) |
  | `noeta.agents` | the agent-file format (`agent_files`, was `noeta.core.agent_files`) and the built-in agents (was `noeta.presets`): agent files, `default_agents`, default plugin list |
  | `noeta.plugins` \| `noeta.backends` \| `noeta.host` | capabilities in a task \| replaceable infrastructure \| running noeta in a program or as a service |
  | `noeta.toolkit` | code several plugins (or backends) share, public to third-party plugin authors |
  | `noeta.core` | the kernel and every interface the layers above implement or call |

  `noeta.testing` (scripted provider, store contract) may import any SDK package, not `apps`.
- **Moves** (old -> new; tests move to the matching `tests/` path):
  - `core/shell.py` -> `toolkit/shell.py`; `core/tool_helpers.py` -> `toolkit/tools.py`;
    `core/helpers.py` -> `toolkit/context.py`. `core/models.py` stays: the runner reads a
    model's default output limit from it.
  - `core/agent_files.py` -> `agents/agent_files.py` (found by the layout check: only
    `agents`, `apps` and embedders use it).
  - `core/store/sqlite.py` -> `backends/stores/sqlite.py`; `backends/postgres.py` ->
    `backends/stores/postgres.py`; `backends/execenv/` -> `backends/execenvs/`. `core/store/`
    keeps `base.py` only (flattened to `core/store.py` if nothing else is left there).
  - `apps/local.py` -> `host/local.py` without `from_settings`, which moves to `apps/config.py`;
    `apps/remote.py` -> `host/remote.py`; `apps/server.py` -> `host/server.py`.
  - `presets/` -> `agents/` (`agents/files/*.md` for the agent files, so the package's own
    modules and the data do not mix).
  - `testing.py` -> `testing/__init__.py`; the store contract tests -> `testing/store_contract.py`
    (test functions over a `store` fixture the caller defines); `tests/` runs them for sqlite
    and Postgres by importing them.
- **Top-level exports**: `noeta` also exports `LocalApp` (with `RemoteClient`, already there).
- **Entry points** in `pyproject.toml` follow the moves; registry names do not change.
- **Rule for new code** — a CONTEXT.md entry "Where code goes", asked in order: does the kernel
  itself use it -> `core`; is it something an agent uses in a task -> a plugin; is it shared by
  plugins or backends -> `toolkit`; is it replaceable infrastructure -> `backends`; does it run
  noeta inside a program or as a service -> `host`; is it a built-in agent -> `agents`; is it the
  `noeta` command or a front end -> `apps`. AGENTS.md's layering line points to it.
- **Check for placement** — `scripts/lint-layout.py`, run by `make check`:
  - the top-level packages of `noeta/` are exactly the set above;
  - every module in `noeta/core` is imported by another `noeta/core` module, or is listed in the
    script's kernel-surface list with a one-line reason (`questions`: the question form plugins
    ask with and front ends answer with; `commands`: front ends list and expand plugins' slash
    commands). A new core module only plugins use
    fails with "move it to noeta/toolkit".
- **import-linter**: the layers contract becomes
  `apps > agents > plugins | backends | host > toolkit > core`; the "front ends do not import
  plugins" contract stays; "backends do not import each other" applies to
  `noeta.backends.providers`, `.execenvs`, `.stores`.

## Plan

- [x] Toolkit: move the three modules, update imports, tests (`tests/toolkit/`).
- [x] Backends: `stores/` (sqlite in, Postgres in), `execenvs/` rename, entry points, tests.
- [x] Host: `host/` with `local`, `remote`, `server`; `from_settings` to `apps/config.py`;
      `LocalApp` exported from `noeta`.
- [x] Agents: `presets/` -> `agents/` with `agents/files/`; tests (`tests/agents/`).
- [x] Testing: `noeta/testing/` package, `store_contract.py`, the repo runs it for both stores.
- [x] Contracts: new layers in `pyproject.toml`; `scripts/lint-layout.py` and its `make check`
      wiring; a test that the script fails on a planted core module only a plugin imports.
- [x] Docs: CONTEXT.md (Layers rewritten, new "Where code goes"), AGENTS.md pointer, README
      (Extend it / embedding imports), CHANGELOG (every move under Changed), docstrings naming
      old paths.
- [x] `make check` green unpiped; the full suite against a local Postgres; `make example`.

## Acceptance criteria

- No file under `noeta/` or `tests/` names an old module path (`grep` for each of
  `noeta.presets`, `noeta.apps.local`, `noeta.apps.remote`, `noeta.apps.server`,
  `noeta.core.store.sqlite`, `noeta.core.shell`, `noeta.core.tool_helpers`, `noeta.core.helpers`,
  `noeta.core.agent_files`, `noeta.backends.execenv`, `noeta.backends.postgres` finds nothing outside
  CHANGELOG).
- `lint-imports` passes with the new contracts; `scripts/lint-layout.py` passes, and fails on a
  planted `noeta/core/x.py` imported only by a plugin.
- An embedding script using only `from noeta import ...` (agents, runtime, `LocalApp`,
  `RemoteClient`) runs without importing any `noeta.apps` module (checked via `sys.modules`).
- A third-party store test module needs only `from noeta.testing.store_contract import *` plus a
  `store` fixture.
- Same test count as before the move (± the new layout tests); `make check` green; Postgres run
  green; `make example` green.

## Risks

- Large mechanical diff (≈150 files touched): done one Plan step per commit, each green, so a
  bad move is easy to find.
- Golden request tests (`tests/presets/golden`) must not change: they hold no module path, so
  any golden diff means the move changed a request and is a bug.
- Pickled or stored references to module paths: none known (entry point names are stable,
  events store no module paths); verified by running the suite against an old `tasks.db`.

## Progress log

- 2026-10-27: proposed after the layout review; owner agreed to the direction, spec for review.
- 2026-10-27: active. `core/models.py` stays in core (the runner uses it; the first measurement
  missed `from noeta.core import models`). The layout check found `core/agent_files.py` (moved to
  `agents`) and `core/commands.py` (kernel surface). Each step its own commit, suite 1367 -> 1370
  (the three layout-check tests); request goldens unchanged.
- 2026-10-27: all Plan items done (archiving is a separate gc). `make check` green (1370 passed, 87.26 %), Postgres 1396 passed none
  skipped, `make example` green; no old path left outside CHANGELOG; an embedding script
  (`default_agents`, `LocalApp`, `RemoteClient`) loads no `noeta.apps` module; a store written
  by `main`'s code reads and resumes with the new code.
- 2026-10-28: follow-up on owner review ("why two protocols", "is apps just the TUI"): ACP
  is a translation of the client API like HTTP, so `apps/acp.py` -> `host/acp.py` and
  `host/server.py` -> `host/http.py`; what is left of `apps` is the `noeta` command and its
  TUI, renamed `noeta/cli` (`cli.py` -> `main.py`). CONTEXT.md gains "Ways in".
