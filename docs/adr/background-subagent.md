# A background subagent is a barrier-free Task that runs concurrently with the parent's turn and pushes its result back at a turn boundary

## Context

Delegating a subagent normally suspends the parent on an all-of barrier (see
subtask-fanout-and-durable-wake.md): the parent stops taking turns until the
group terminates. Some work should not hold the turn — a broad scan, a long
research job — and the user should be able to keep chatting while it runs.

A shell command covers that shape for programs the host can run detached (see
shell-permission-and-background.md). A subagent is a different case: it has a
Policy and runs its own multi-turn decide loop, so it cannot be modelled as a
process the host watches for an exit code.

## Decision

- **A background subagent is a Task, not a host side effect.** It carries a
  Policy, owns an EventLog stream, and goes through the recorded pipeline like
  any other subtask, which makes it durable: after a crash it resumes from its
  own log rather than being recovered through process-identity guesswork.

- **The spawn hangs no barrier.** `spawn_subagent(background=true)` returns a
  "started" receipt as the call's tool result, leaves the parent advancing its
  turn, and submits the child subtree to the shared fan-out executor (see
  subtask-parallel-execution.md), where it runs to terminal concurrently with the
  parent. The child is enqueued reserved, so only the targeted lease that seeds
  its goal can claim it and an untargeted poll cannot drive it with an empty
  history. The single-writer invariant holds: parent and child are two Tasks
  writing two streams, and what overlaps is each one's own model and tool I/O.

- **The result is delivered at a turn boundary, proactively.** On terminal, the
  child's result is snapshotted into the ContentStore and handed to a shared
  background-delivery seam — the same one the background shell path uses — which
  hops onto a daemon thread and folds the parent. A terminal session has no turn
  to wake, so the push is dropped and the durable exit event stands for audit; a
  session idle-suspended on its next-goal handle is woken and driven through one
  notice turn tagged as system-origin, carrying a one-line summary plus the
  subagent's result text dereferenced and inlined so the model reads the answer
  rather than a pointer it cannot resolve (the content reference is retained on
  the delivery anchor for provenance and re-delivery); a parent mid-turn is
  re-attempted until it settles or a bounded deadline elapses. The spawn's
  tool-result slot holds the "started" receipt, so completion cannot reuse it and
  must arrive as its own notice.

- **A background subagent is always a single one.** The `background` flag is
  valid only on a call carrying exactly one spawn entry; a fan-out batch is
  always foreground. The child's creation event carries the flag conditionally
  folded, so it is absent from a foreground child's canonical bytes.

- **Lifetime belongs to the session, lineage to the task.** The child may outlive
  the turn that started it, its parent link never blocks the parent's completion,
  and a per-session cap (default eight) is checked before any durable write, so
  an over-cap launch is refused outright rather than queued and leaves no trace.
  Session close tears in-flight children down cooperatively through the cancel
  cascade, writing each a terminal marker on its own stream so a later recovery
  scan does not re-drive it.

- **Recovery is a startup scan.** For every launch event on a parent stream
  without its matching delivery event, a non-terminal child is re-enqueued and
  re-driven from its own EventLog (the descent skips re-seeding a goal a child
  already has), and a terminal child whose notice was lost is re-delivered
  without re-driving.

- **Determinism rests on the delivery anchor.** When the child terminates
  relative to the parent's turn is genuinely non-deterministic in wall-clock
  terms, but the notice is injected at a turn boundary and the delivery event
  guarantees it is injected exactly once, so fold and resume reproduce the same
  parent state. Retrying a deferred push only shifts *when* the notice turn is
  injected.

- **Nested background is not offered.** The Engine reaches the background driver
  through a duck-typed launcher seam wired only in the top-level interactive
  engine; child engines and one-shot engines get none, so a background spawn
  inside a background child collapses to a foreground barrier spawn.

## Rationale

- **A subagent has a Policy, so it belongs on the Task substrate.** Modelling it
  as a host object would mean inventing "a host object that runs a Policy", which
  is heavier than a first-class Task and forfeits recorded durability and resume.

- **Delivery reuses the turn-boundary push, needing no new wake.** A background
  command finishing and a background subagent finishing are the same class of
  event: a detached activity terminates and its result must reach a session that
  is not waiting for it. Sharing the path means no new wake condition to
  serialize and no wider fold surface — and one implementation of the fiddly
  parts (the non-blocking hop, the terminal-parent check, the bounded mid-turn
  retry) instead of two that drift.

- **Conditional folding keeps the blast radius at zero.** Background is something
  a call actively requests, and a foreground spawn records no trace of the
  question.

- **"Fire and forget" needs no group semantics.** A single scalar subtask is the
  whole of it; N-way concurrent join is the barrier group's job. Keeping
  background and grouped orthogonal keeps both simple.

## Alternatives considered

1. **Make the background subagent a host side effect, held in the process
   registry like a background command.** Rejected: a subagent runs a
   Policy-driven decide loop, not an OS process whose exit code can be watched.
   Forcing it into a host side effect requires inventing a host object that runs
   a Policy, and loses durability and resume.
2. **Keep the barrier and drain the "background" subtask during the parent's next
   suspend window.** Rejected: that is running it *later*, not in the background
   — the subtask makes no progress while the parent takes user turns, which is
   the entire point.
3. **A long-lived worker pool plus a dispatcher driving background subtasks
   across multiple leases.** Rejected: a re-architecture far larger than the
   capability needs; the inline drain and its shared pool run subtasks
   concurrently on their own, and the only missing piece is submitting one
   without binding it to a barrier.
4. **Deliver the result by reusing the spawn call's tool-result slot.** Rejected:
   that slot carries the "started" receipt. Completion is a late-arriving event
   decoupled from the originating call and needs its own notice.
5. **Let a background subagent spawn further background children.** Rejected:
   nested concurrency is deliberately not offered, and background-on-background
   would let the number of in-flight activities run away, making the concurrency
   ceiling meaningless. A background subagent that fans out internally drains
   under the ordinary rules.

## Consequences

- The `background` flag lands on the spawn decision in
  `noeta.protocols.decisions` and, conditionally folded, on the child creation
  payload; the launch and delivery boundary events live in
  `noeta.protocols.events`.
- The non-blocking admission path lives in `noeta.core._decision_handlers`, the
  in-flight registry and its recovery scan in
  `noeta.execution.background_subagent` (reusing the drain's member-drive and
  shared executor), and the shared turn-boundary push in
  `noeta.execution.background_delivery`, which both background tenants funnel
  through with only their own completion-notice projection.
- A background spawn writes no subtask-spawned event, so it is not counted
  against the spawned-subtask budget; the per-session cap is the backstop.
- The delivery anchor is where the exactly-once guarantee bears weight on this
  path; any change to it must preserve "injected exactly once".
- Not offered: mid-flight progress polling of a background subagent, a
  model-visible kill tool, and nested background.
