# Task is the only first-class primitive

## Context

An agent runtime tends to sprout several parallel core abstractions: Run, Workflow, Session, ChildRun, and Conversation each become a first-class concept with its own spec / runner / state slice / event set. This decision compresses the core data model down to a single abstraction, so the abstraction count does not triple and orchestration is not outsourced out of the kernel.

## Decision

Noeta's core data model has exactly one abstraction: **Task**. Workflow, Session, ChildRun, and Conversation are **not** parallel first-class concepts — they are special cases or usages of Task:

- A fixed pipeline = a deterministic Policy.
- Multi-agent orchestration = a Task that spawns child tasks (each child task is its own task, its own EventLog stream, linked via `parent_task_id`).
- A multi-turn conversation = a single Task receiving user input across several turns.

The runtime has exactly one entity type: Task. The names `WorkflowSpec / WorkflowRunner / WorkflowPolicy / SessionStore / ConversationManager` **must not appear in the code** (mechanically enforced by `scripts/lint-naming.py`).

The Engine main loop has no second-class concept like handoff. A Decision is a set of **neutral mechanism variants** (not a fixed count): the canonical neutral variants + `SpawnSubtasksDecision` (fanout) + `StatePatchDecision` (a durable state write that continues the loop) + others. The test is "is it a neutral mechanism?", not "how many are there?". Product control tools like todo / plan / ask-user-question **do not get their own kernel variant**; the SDK expresses them through these neutral channels.

## Rationale

- **The count of core abstractions must not triple.** Three parallel families — Run + Workflow + Session — force every capability to be built three times, and `WorkflowRunner` additionally has to subscribe to the EventLog to coordinate cross-Run state and parent/child Run synchronization. Compressed into a single Task entity, a child task's wait / cancel / budget inheritance can all be handled uniformly inside the Engine.
- **Don't outsource orchestration.** A child task's join / cancel / budget inheritance must be handled uniformly inside the Engine to preserve single-EventLog semantics and keep fold/resume re-derivable.
- **"Neutral mechanism variant" is an extensible test, not a hard-coded enum.** The old phrasing ("there are only 7 Decisions") forced product control semantics to be jammed into the kernel as variants. Switching to "is it a neutral mechanism?" lets product control tools be expressed through neutral channels, so the kernel shape is not dragged along by product features.

## Alternatives considered

1. **Three parallel families — Run + Workflow + Session — each with its own spec / runner / state slice / event set.** Rejected: the core abstraction count triples; `WorkflowRunner` subscribing to the EventLog to coordinate cross-Run state and parent/child sync is complex; every capability must be built three times.
2. **Fully flat, no child tasks, orchestration outsourced to Temporal.** Rejected: responsibilities overlap with Temporal, and a child task's wait / cancel / budget inheritance cannot be handled uniformly inside the Engine.

## Consequences

- The mechanical guard lives in `scripts/lint-naming.py`: it forbids rejected names like `WorkflowRunner` from entering the source.
- The neutral Decision variants themselves live in `noeta.protocols.decisions`.
- The concrete landing of child-task fanout / join and `parent_task_id` linkage is covered in `subtask-fanout-and-durable-wake.md` (the SpawnSubtasks / StatePatch mechanisms are carried there).
