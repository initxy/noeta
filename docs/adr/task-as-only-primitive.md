# Task is the only first-class primitive in the core data model

## Context

An agent runtime attracts several parallel core abstractions — a run, a workflow, a session, a child run, a conversation — each arriving with its own spec, runner, state slice and event set. Left unchecked, every capability has to be built once per family, and coordinating them pushes orchestration outside the Engine.

## Decision

The core data model has exactly one entity: **Task**. A pipeline, a multi-agent tree and a multi-turn conversation are usages of Task, not siblings of it:

- A fixed pipeline is a deterministic Policy. A Policy is any function from the composed `View` to a `Decision`; nothing in the kernel requires an LLM behind it.
- Multi-agent orchestration is a Task spawning child tasks. Each child is a Task with its own EventLog stream, linked to its parent by `parent_task_id` and carrying its own `subtask_depth`.
- A multi-turn conversation is one Task receiving user input across several turns.

`scripts/lint-naming.py` keeps the vocabulary from re-splitting: the class names `Run`, `Workflow`, `Session`, `Mutator` and `Pattern`, and the compounds `WorkflowRunner`, `WorkflowPolicy`, `WorkflowSpec`, `SessionStore` and `ConversationManager`, fail the lint in project sources. The same lint bans naming an *identity* after a session below the host layer, because the engine knows only tasks and that identity already has a name.

The Engine loop likewise has no second-class concept such as handoff. `Decision` is a set of **neutral mechanism variants**, and the admission test is "is this a neutral mechanism?", not a headcount: alongside the canonical set sit the fan-out of a spawn, the loop-continuing state write and the loop-continuing compaction request. Product control tools — todo, plan mode, ask-user-question — get no kernel variant of their own; the SDK expresses them through the neutral channels.

## Rationale

- **One entity keeps every capability built once.** Parallel run / workflow / session families multiply the surface by three and force a coordinator to subscribe to the EventLog just to keep cross-entity and parent/child state in step.
- **Orchestration stays in the Engine so single-EventLog semantics survive.** A child's join, cancel and budget inheritance must be derivable by fold; delegating them to an outside coordinator puts state that fold cannot see on the critical path of resume.
- **"Neutral mechanism" is an extensible test; a fixed variant count is not.** A hard-coded enum forces product control semantics to be jammed into the kernel as new variants. Testing for neutrality instead lets product control tools ride the existing channels, so the kernel shape is not dragged along by product features.

## Alternatives considered

1. **Three parallel families — run, workflow and session — each with its own spec, runner, state slice and event set.** Weighed and rejected: the core abstraction count triples, every capability has to be built three times, and the workflow runner ends up subscribing to the EventLog to coordinate cross-entity and parent/child state.
2. **A fully flat model with no child tasks, orchestration delegated to an external workflow engine.** Rejected: responsibilities overlap with that engine, and a child's wait, cancel and budget inheritance can no longer be handled uniformly inside the Engine.

## Consequences

- The neutral variants live in `noeta.protocols.decisions`; the union's shape and the decision-handler design contract are pinned by structural tests, so a product-shaped variant cannot slip into the kernel unnoticed. The naming guard runs as part of the verification gate.
- Child-task fan-out, join and the `parent_task_id` linkage are elaborated in `subtask-fanout-and-durable-wake.md`.
