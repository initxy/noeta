---
status: active
created: 2026-10-27
branch: v1-hold-plugins
---

# Background jobs across workers, plugin files, plugin questions

## Goal

Make background shells correct when several workers share a store, and make plugins as easy to
write and as able to talk to the person as pi's: a single `.py` file is a plugin, any plugin can
ask the person a question or tell them something, and front ends depend on no plugin.

## Non-goals

- Adopting a job from another worker (reading or killing it there): a local env on another
  machine is unreachable, and a parked task has nobody watching for the job's end.
- Hot reload, `noeta plugin install`, more hook points, custom UI components, services between
  plugins, dependencies for plugin files (a plugin with dependencies is a package).
- OS-level sandboxing, rewind, timers, log snapshots for long tasks, the 1.0 benchmark: each its
  own spec.

## Context

- Background jobs are process-local (`noeta/plugins/shell/jobs.py`). A task releases its claim
  whenever it waits, so another worker may pick it up next and can neither read nor stop the job;
  the shell docstring tells deployments to route a tree to one worker by queue by hand.
- Plugins come only from installed distributions (entry points); pi loads a TypeScript file
  dropped in `~/.pi/agent/extensions/`, project ones only once the project is trusted.
- The TUI and ACP import `noeta.plugins.ask_user` (question helpers) and ACP imports
  `noeta.plugins.mcp` (editor servers). A tool can already park on a question
  (`Park(on="question", detail=...)`) and front ends render it from the detail alone; only the
  helpers live in the plugin.

## Decisions

- **Tree hold (kernel).** A worker can hold a task tree: while held, other workers do not claim
  any task of that root. A hold lives in its own `holds` table (`root_id`, `worker`, `until`), so
  existing stores need no migration; `claim` skips trees held by another worker until `until`.
  `ToolContext.hold()` returns a release callable; the `Runtime` renews its holds every
  `lease_seconds / 3` and a hold lasts `2 × lease_seconds`, so a dead worker's hold runs out.
  The kernel knows nothing of shells.
  Rejected: a per-worker queue (a dead worker's queue strands its tasks); adopting jobs through
  files in the env (non-goal above).
- **Shell jobs hold their root** while this worker runs any of the tree's jobs. On start, a
  running job missing from this worker's table is lost: the claim itself shows its worker no
  longer holds the tree. The `elsewhere` path and the PID probe go. A crashed worker's tree waits
  up to one hold (60 s by default) before another worker takes it.
- **Plugin files.** `~/.noeta/plugins/` holds `*.py` files and directories with `__init__.py`
  (one level). Each `Extension` subclass defined there with a `name` registers under it. A
  project's `.noeta/plugins/` loads only when the project directory is listed in `config.toml`
  `trusted_projects` (`noeta trust` adds the current one); otherwise a notice says it was
  skipped. An installed package wins a name clash (the file is skipped with a warning). A file
  that fails to import is remembered, and an agent naming its plugin fails with that error.
  Files load once per process. Each worker loads its own home directory; packages remain the way
  to deploy plugins to many machines. `noeta plugins` lists every plugin and where it comes from.
- **Questions are a kernel form.** `noeta/core/questions.py` holds the question shape
  (`{"questions": [{id, question, header?, options?, multiSelect?}]}`, answers
  `{id: {"selected": [...]} | {"other": text}}`), `next_question`, `typed_answer` and validation
  against the parked detail. A question without options asks for free text. `ask_user` builds on
  it; the model-facing `AskUserQuestion` does not change.
- **Notices.** `TaskView.notify(text)` records a note for the person that is never sent to the
  model; the TUI shows it as a notice, ACP as an agent message.
- **Front ends import no plugin.** `Mcp` takes Claude Code's `mcpServers` mapping as plain data,
  so ACP passes the editor's servers through plugin settings. An import-linter contract forbids
  `noeta.apps` → `noeta.plugins`.

## Plan

- [ ] Tree hold: `holds` table, `hold` / `unhold` and the claim filter in SQLite and Postgres
      (contract tests, Postgres run locally), `Runtime` renewal, `ToolContext.hold`
- [ ] Shell jobs hold their root; the lost rule on start; two-runtime tests
- [ ] Plugin files: registry sources, `trusted_projects`, `noeta trust`, `noeta plugins`
- [ ] `noeta/core/questions.py`, `ask_user` on it, `TaskView.notify`, MCP servers as data,
      the apps ↛ plugins contract
- [ ] The example plugin asks a question and sends a notice
- [ ] README / CONTEXT / CHANGELOG

## Acceptance criteria

- `make check` green; the request golden unchanged; the store contract tests pass on Postgres.
- Two runtimes on one store: a tree with a running job is not claimed by the other runtime while
  the job's worker lives; once that worker is gone and its hold has run out, the other takes the
  tree and the model is told the job was lost.
- A plugin in `~/.noeta/plugins/x.py` is usable through `add_plugins` without packaging; a
  project's `.noeta/plugins/` is ignored until trusted.
- `lint-imports` forbids `noeta.apps` → `noeta.plugins` and passes.
- A third-party tool asks the person a question and gets the answer through the TUI and ACP with
  no front-end code for that plugin.

## Risks

- A renewal bug strands a tree; bounded by the hold's expiry.
- Loading a project's plugin files runs repository code; the trust list is the only gate.

## Progress log

- 2026-10-27: spec written.
