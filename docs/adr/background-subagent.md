# Background subagent: spawn a subtask without suspending the parent, run it concurrently, and wake the parent to continue when it terminates

## Context

noeta can already push a **program** to the background (`shell_run(run_in_background=True)`, see shell-permission-and-background.md): the process runs concurrently as a host side effect without blocking the task, and on exit "mechanism C" wakes the session, injects a notification, and the model continues from there. But a **subagent**—a Subtask with a Policy that runs its own multi-turn loop—has no equivalent capability: once a parent spawns a subtask, it **suspends on an all-of barrier** (`wake_on = SubtaskGroupCompleted`, see subtask-fanout-and-durable-wake.md) and doesn't resume until the whole group terminates. The parent cannot "delegate a subagent, keep chatting with the user, let the subagent work in the background, and check back when it's done."

subtask-parallel-execution.md already loosened one layer of concurrency: members of an opt-in concurrent group can run **truly concurrently** on the process-global thread pool during drain. But that layer of concurrency **always happens within the window where the parent is suspended on the barrier**—drain is triggered inline after the parent releases its lease, on that released worker. A subtask never coexists with a parent that is **still advancing its own turn**.

background-shell deliberately made background commands host side effects, and one of the reasons was "avoiding prematurely prying open the concurrency invariant that fan-out deliberately deferred." A background subagent wants exactly that deferred cut: let a subtask run to completion concurrently **while the parent is not suspended and is still taking user turns**.

## Decision

### A background subagent is a Subtask, not a host side effect

A background command has no Policy (no decision-maker), so it was made a side effect in the host process registry. A background subagent is the opposite—it **has** a Policy, is a first-class Task, and goes through the EventLog as usual. The upside is that it is naturally durable: after a crash it resumes and continues from its own EventLog, unlike an OS process that must be recovered through conservative PID identity checks.

### Prying open the "subtask concurrent with a non-suspended parent" invariant

On `spawn_subagent(background=True)`, the Engine **does not hang a barrier for this subtask and does not suspend the parent**: it immediately submits the subtask subtree to the existing process-global thread pool (subtask-parallel-execution.md's `_global_executor`) and immediately returns a "started" `tool_result` to the parent. The parent continues advancing this turn; the background subtask runs to terminal state **concurrently** with it on the thread pool.

This is exactly where the deferred invariant is explicitly opened: before this, "concurrent subtasks" existed only in the window where the parent was suspended; now a background subtask can coexist with a live parent turn. **The single-writer invariant is not broken** (single-writer-invariant.md): the parent and the subtask are two different Tasks, each writing its own EventLog stream; the storage layer was built for concurrent threads from the start (WAL, per-adapter write locks, see subtask-parallel-execution.md), and what runs concurrently is each one's own LLM / tool I/O, not concurrent writes to the same stream.

### Delivery via mechanism C: on termination, wake the parent session and inject the notification at a turn boundary

When the background subtask reaches terminal state, it offloads its return result into the ContentStore, then **reuses mechanism C** (shell-permission-and-background.md): it wakes the parent session through the next-goal wake handle, and a host-side background driver injects, at a turn boundary, a notification tagged `origin="system"` (a one-line summary + the result ContentRef). The parent agent sees it on its next turn and continues accordingly—**active delivery, without the user having to ask again and without the model polling**.

The `tool_result` slot of `spawn_subagent` is already occupied by the "started" receipt at spawn time, so completion **cannot** reuse that tool_result and must go through mechanism C's separate notification message—exactly isomorphic to how background commands deliver.

### The `background` flag folds conditionally, and a background subtask is always a single one

The intent hangs on the transient `SpawnSubtaskDecision.background`, folded conditionally (omit-when-falsey, mirroring how subtask-parallel-execution.md handles `concurrent`): on the non-background path, all existing recordings are byte-for-byte unchanged; only a background spawn writes one extra key. A background subagent is always a **single** one—"fire one and forget it" is the semantics of background; for a group of concurrent joins, use the existing `parallel` / `SpawnSubtasksDecision` barrier group.

### Lifetime belongs to the session, lineage to the task; crash recovery resumes; kill goes through cancel cascade

Mirroring background commands: a background subagent may outlive the parent turn that started it, its lifetime owner is the session, and `spawned_by_task_id` is only a lineage label that **never blocks the parent's completion**. After a host restart, background subtasks that "have a `BackgroundSubagentStarted` but no `BackgroundSubagentDelivered` / terminal state" are scanned and resubmitted to the thread pool to continue (the subtask itself resumes from its EventLog). On session close, in-flight background subagents are stopped via the cancel cascade. Per-session background subagents have a concurrency limit (default 8, mirroring the background command job limit); over the limit is rejected outright, not queued.

### Red line: deterministic fold / resume is not broken

The termination of the background subtask, the `BackgroundSubagentDelivered` dedup anchor, and the mechanism-C-injected notification are all events with a **deterministic recorded position** on the parent stream. "When the subtask completes relative to the parent turn" is genuinely non-deterministic in wall-clock terms, but noeta's honest boundary (subtask-parallel-execution.md) has always been "fold / resume reproduces **that one** recording, not two live runs byte-for-byte identical"—background completion is injected once at a turn boundary and the `Delivered` anchor guarantees it is injected only once, so fold / resume reproduces the same parent state.

## Rationale

- **A background subagent should be a Task rather than a host side effect because it has a Policy.** Background commands were made host side effects because a process has no decision-maker, and forcing it into "Task = has a Policy" is awkward. A subagent carries its own Policy, so making it a Task goes with the grain—and gets EventLog durability / resume for free, sparing it the conservative PID recovery of background commands.

- **Delivery reuses mechanism C rather than inventing a fresh wake for subagents.** noeta-agent is request-driven with no long-lived WorkerLoop; the wake + turn-boundary injection for "background completion" was already solved once for background commands, and its durable footprint is byte-for-byte isomorphic to a single `send_goal` turn. The completion of a background subagent and the completion of a background command are the same class of event (a background activity terminates and its result must be pushed to a session that isn't waiting for it), so reusing the same path means no new WakeCondition serialization and no enlarged fold surface.

- **Conditional folding = zero blast radius.** Default behavior and every existing recording are byte-for-byte unchanged; background is a flag that a `spawn_subagent` call actively requests. Same folding discipline as `concurrent`.

- **Only a single background subagent is supported because "group" semantics live elsewhere.** The essence of background is "fire and forget," and a single scalar subtask is enough; for N-way concurrent join, use the existing barrier group. Keeping "background" and "grouped" orthogonal keeps both sides simple.

## Alternatives considered

1. **Make the background subagent a host side effect too (stuffed into the process registry like a background command).** Rejected: a subagent has a Policy and runs an EventLog-driven decide-loop; it is not an OS process you can "fire and monitor the exit code of." Forcing it into a host side effect would require inventing a "host object that runs a Policy," which is heavier than making it a first-class Task and also loses durability / resume.

2. **Don't pry open the concurrency invariant; instead "serially drain the background subtask during the parent's next suspend window."** Rejected: that isn't "run in the background," it's "run later"—the subtask doesn't move while the parent is still taking user turns, so you don't get the "chat while it works in the background" experience, which misses the alignment goal.

3. **A true long-lived worker pool + dispatcher driving background subtasks across multiple leases.** Rejected: a re-architecture far larger than this capability needs; the existing inline drain + thread pool can already run subtasks concurrently, and the only missing piece is "submit one without binding it to the parent barrier."

4. **On completion, reuse the spawn `tool_result` slot to deliver the result.** Rejected: that slot is already occupied by the "started" receipt at spawn; completion is a late-arriving thing decoupled from the original tool call and must go through a separate mechanism-C notification (consistent with background commands).

5. **Let a background subagent spawn further backgrounds (nested background).** Rejected: nested concurrency is deliberately not provided (subtask-parallel-execution.md); background-on-background would let the number of in-flight background activities run away and render the concurrency ceiling meaningless. If a background subagent fans out internally, it drains serially per the existing rules.

## Consequences

- Protocol side: `SpawnSubtaskDecision.background` (conditionally folded) lands in `noeta.protocols.decisions`; the boundary events `BackgroundSubagentStarted` / `BackgroundSubagentDelivered` land in `noeta.protocols.events` (`BackgroundShell*` is their shape template).
- Handling / execution side: the non-blocking admission of the background branch lands in `noeta.core._decision_handlers` (`handle_spawn_background_subtask`, loop-continuing, via a special case in `Engine.run_one_step` rather than `dispatch_exit`); mechanism C's "background completion notification" must not carry the background-command-specific PID / process-registry assumptions into the subagent path.

  **Implementation landing (deviation from the design assumption)**: the background subagent registry lands in **`noeta.execution.background_subagent.BackgroundSubagentRegistry`**, not in `noeta.runtime`—it reuses drain's `_drive_member_to_terminal` / `_global_executor` (both in `noeta.execution`), and in the import-linter layer order `execution` sits above `runtime`, so the registry cannot land in `runtime`. The Engine obtains it through a duck-typed `background_subagent_launcher` (two seams: `.launch` / `.capacity`), wired only in the top-level interactive Engine (which has the multi-turn `policy_wrapper`)—sub-Engines / oneshot get `None`, so "a background subagent opening further background" naturally degrades to foreground serial drain (a non-goal in v1). Mechanism-C delivery (`InteractionDriver.notify_background_subagent_exit` + `SdkHost._drive_background_subagent_exit`) is isomorphic to background shell's `_on_background_exit`, and the notifier is wired by `noeta.sdk.Client.__init__` via `set_background_notifier` (which incidentally activated the previously-unwired background-shell mechanism C).

  **Two v1 simplifications (documented)**: ① a background spawn does not write `SubtaskSpawned`, so it is not counted toward `Budget.max_spawned_subtasks`—concurrency is backstopped by the per-session background limit (default 8). ② When a subagent completes while the parent turn is still in flight (a rare race), the delivery thread does a bounded retry-until-idle (default 30s) rather than background shell's "single attempt + no reschedule"—purely wall-clock timing, with recording bytes unaffected.

- Governance: the per-session background subagent concurrency limit, lifetime belonging to the session, and kill going through the cancel cascade are all the same shape as background-command job governance.
- Remember the honest boundary: what is guaranteed is that fold / resume reproduces the recorded order, not that two live runs are byte-for-byte identical; the `BackgroundSubagentDelivered` dedup anchor is where this boundary bears weight on the background path, and any change to it must preserve "injected exactly once."
- Non-goals (v1): mid-flight progress polling of a background subagent, a model-visible `subagent_kill` tool, and nested background—none of these are done.
