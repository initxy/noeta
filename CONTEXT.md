# Noeta

Durable AI agents: every task is an append-only event log with one writer, so a task survives
crashes, waits for a person at no cost, and runs on one process or many machines unchanged.

## Task

The only unit of work. A task has an agent, a config, an event log, an inbox and a row in
`tasks` (a projection of the log written in the same transaction). Sub-agents are child tasks
with `parent_id` / `root_id`. There is no session, run or workflow entity.

## Event log

Ten event types (`noeta/core/events.py`). Plugin state is never a new event type: a plugin records
`ExtensionRecorded(extension, kind, data, message?)` and folds it in its own `reduce`. The loop
state is a pure fold of the log; the model request is a pure function of that state.

## Inbox

The only way into a task: `Say`, `Cancel`, `Answer`, `ChildEnded`, `TimerFired`, `External`,
each idempotent on an optional `dedupe_key`. Front ends, parents, timers and plugins all post;
only the task's runner reads.

## One writer

A runner claims a task from the store (`claim` / `renew` / `release`; Postgres uses
`SKIP LOCKED`). Every append is conditional on `expected_seq` and the claim's `owner_epoch`, so a
stale runner can never write. A tool call is logged as started before it runs; on recovery an
unfinished call is answered "outcome unknown", never re-run.

## Layers

`core` (log, inbox, loop, runner, store interface + sqlite, client, and every plugin interface),
capability plugins, backends (providers, exec envs, Postgres), presets (agent definitions and
prompts as data), apps (CLI, TUI, ACP, HTTP server). Imports go downward only; the kernel
carries no capability and a plugin imports nothing but the kernel (`lint-imports`).

## Extension

The one capability-plugin interface (`noeta/core/extension.py`): tools, context parts, commands,
`on_start`, `before_model`, `before_tool` (gates: Deny > Rewrite > Ask > Allow), `after_tool`,
`on_event`, `reduce`, `on_context_overflow`, `resident`. Every method is optional.

## Park

A tool call that waits (a question, an approval, children) parks: the task ends its step with
`Waiting` and holds no worker. An `Answer` or `ChildEnded` in the inbox resumes it.

## origin

A message's `origin` says who speaks in the user channel: `user`, or `system` / `memory` for
text the host or a plugin adds (reminders, notices, recalled memory). Providers render those as
`<system-reminder>` blocks (Anthropic) or with a preamble saying it is not the user (OpenAI
shapes); user text cannot forge the tag.

## Naming: Session is a scope, never an identity

No `session_id`, session list or session-keyed state in `noeta/`; use `task_id` or `root_id`.
Exempt: the ACP adapter, which uses the protocol's own names.
_Avoid_: `SessionStore`, `ConversationManager`, `class Session`

## Naming: Run, Workflow, Mutator, Pattern

Not concepts — say Task. A fixed procedure is an agent plus sub-agents.
_Avoid_: `class Run`, `class Workflow`, `WorkflowRunner`, `class Mutator`, `class Pattern`

## Naming: model-visible tool names

Tool names the model sees follow Claude Code (`Read`, `Glob`, `Grep`, `Edit`, `Write`, `Bash`,
`TodoWrite`, `AskUserQuestion`, `Task`, `WebFetch`, `WebSearch`, …) because models carry their
strongest prior on those names. Everything else is snake_case.

## Naming: enforcement

`scripts/lint-naming.py` (in `make check`) rejects the banned class names and identifiers above
everywhere, and compound `session` identifiers inside `noeta/`. Files that state the bans
(`CONTEXT.md`, `AGENTS.md`, `CHANGELOG.md`, the script) are exempt.
