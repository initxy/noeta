# Subtask fanout is opt-in per group; real wall-clock parallelism only on the live drain

## Context

`subtask-fanout-and-durable-wake.md` laid out the skeleton for an N-way join (one parent task fans out several subtasks and rejoins at a group barrier), and `workflow-orchestration.md` gave the model the ability to write its own orchestration script that dispatches multiple agents. Both decisions explicitly deferred "concurrency" to a v2 follow-up: v1 only wired the group-join skeleton onto a single worker's **serial** drain. This decision delivers the "bounded concurrent executor + lease relaxation" that those two promised, and it is **opt-in per group**.

## Decision

- **Concurrency is opt-in per group, and the intent rides on the suspend condition.** `SubtaskGroupCompleted` gains a `concurrent: Optional[bool]` field with **conditional folding** (`__canonical_omit_none__`): `None`/serial does not appear in the canonical bytes, so every pre-v2 recording and every serial group stays byte-for-byte identical; only an opt-in concurrent group writes the extra `"concurrent":true` key. The intent is first expressed on the transient `SpawnSubtasksDecision.concurrent`, and the Engine's `handle_spawn_subtasks` copies it (`or None`) onto the persisted condition.

- **The executor lives inside the in-process live drain (`noeta.execution.subtask_drain`), not in a new worker pool.** A concurrent group submits each member subtree to a **process-global, bounded `ThreadPoolExecutor`** whose **`max_workers` is the concurrency ceiling** (`NOETA_MAX_SUBTASK_CONCURRENCY`, default `min(8, cpu)`) — there is no separate semaphore. The parent stays suspended (its lease released) until the group barrier fires, then resumes in one shot. A **nested** concurrent group (a member that itself fans out) drains **serially** within its own worker (`allow_concurrent=False`), so a pool worker never resubmits to the pool — and that is exactly what keeps the single shared pool deadlock-free (a worker never blocks waiting on a saturated pool that can never schedule the sub-job). As a result, no matter how deep the nesting goes, `max_workers` caps the total number of members in flight; nested **concurrency** itself is intentionally not offered (the v2 payoff is overlapping top-level groups). Neither the Engine nor the Dispatcher protocol changed — the lease relaxation is purely "the drain may hold N member leases at once for an opt-in group" (the dispatcher already leases out different tasks concurrently under its own lock; it was the drain that serialized them).

- **Concurrency is behavior that only exists during live timing, and that falls out for free.** The concurrent executor runs only while the group is **live**; everything downstream reads recordings, not the executor. The N `SubtaskCompleted` observer events land on the parent's EventLog in a fixed recorded order, so a later `fold` (on resume or inspection) re-derives the same parent state regardless of how the subtasks interleaved in wall-clock time. No mode flag has to be threaded through: the drain's executor exists only on the live path.

- **No determinism normalization is needed — the recorded order is authoritative.** `SubtaskCompleted` events are written to the parent's EventLog in arrival order (a fixed order), so a later `fold` always re-derives state from that same recorded order — wall-clock interleaving never reaches the recording layer. The parent's **use** of the results is itself spawn-order deterministic (`engine._render_subagent_group_result` rebuilds them by member id). The "canonical sort by subtask_id" that an earlier decision envisioned is therefore **redundant**: once arrival order is committed to the log, fold/resume reproduces it for free — ordering only matters when state is re-derived by re-executing the subtasks live, and Noeta does not do that.

- **Storage needed no rework.** `SqliteEventLog`/`SqliteDispatcher` were already built for concurrent threads (`check_same_thread=False`, WAL, `busy_timeout`, `BEGIN IMMEDIATE` retries; subscribers fire after commit and outside the writer lock — this is precisely the "cross-stream `ChildLifecycleObserver` pattern"). Writes are serialized through a per-adapter lock; the wall-clock win comes from **overlapping LLM/tool I/O**, not from parallel DB writes.

- **The observer is the only component that needed concurrency hardening.** `ChildLifecycleObserver` now serializes both its lineage mutations and its "read count — decide — wake" critical section under a single lock, and uses a `_group_woken` set keyed by `group_id` to guarantee each group barrier is claimed exactly once, so when N siblings finish on N threads the group wake fires exactly once and never races the lineage dict. The `SubtaskCompleted` emit stays **outside** that lock (it notifies subscribers synchronously, and would otherwise self-deadlock a non-reentrant lock).

- **Still all-of only.** any-of / k-of-n / fail-fast are still not supported (they need subtask cancellation + dynamic group size). Concurrency for `pipeline()` is still deferred (see `workflow-orchestration.md`).

## Rationale

- **Live latency is the only payoff, so pay only the live cost.** resume reads recorded results back from the EventLog and gains nothing from concurrency; running the executor only on the live drain guarantees that every non-live path (fold / resume / inspection) is naturally single-threaded and naturally deterministic.

- **Opt-in per group + conditional folding = zero blast radius.** The default behavior, all existing recordings, and every serial group are byte-for-byte unchanged; concurrency is something a `parallel()` group actively asks for, gated by the `NOETA_SUBTASK_CONCURRENCY` environment variable (read via `_concurrent_fanout_enabled()`, default ON — only `0`/`false`/`off`/`no` forces the serial drain).

- **Committing arrival order to the log makes determinism far simpler than the earlier decisions assumed.** Those decisions reasoned as if some downstream path re-derived completion order by re-running the subtasks (in which case arrival-order non-determinism would be fatal). Noeta does not re-run subtasks: each `SubtaskCompleted` is persisted on arrival, so the recording is self-consistent no matter what order it was produced in. The honest boundary is "fold/resume reproduces **that** recorded order," not "two live runs are byte-identical" — and the former is unaffected by concurrency.

- **Putting the executor in the drain rather than a worker pool keeps the change local.** The product's entire execution model is a synchronous inline drain; turning it into a standing multi-worker pool would rewrite the cancel/resume seams wholesale for no added capability.

## Alternatives considered

1. **Canonical-sort the group's `SubtaskCompleted` sequence (plus multiset normalization of `subtask_results`).** Implemented first, then removed: each completion is persisted on arrival and fold reads it back in that same recorded order, so nothing needs re-normalizing — the sort was dead defensive code, contrary to the repo's "no speculative seams" principle.

2. **A real worker pool + multi-lease dispatcher to drive subtasks.** Rejected: far more re-architecture than the capability needs; the inline drain already has delegation.

3. **Normalize on write (buffer all of a group's completions, sort at the barrier, then emit).** Rejected: it would defer each completion's durable record to the barrier moment, regressing `subtask-fanout-and-durable-wake.md`'s "durable exactly-once wake" (a mid-group crash would lose the records of members that already finished). Incremental emit is kept.

4. **Use a `concurrent: bool = False` (non-optional) field.** Rejected: `False` is not `None` and would always serialize, drifting every recording. The field is `Optional[bool]` under `__canonical_omit_none__`.

5. **A per-group `ThreadPoolExecutor`.** Rejected: nested fanout would multiply pools and threads. Instead a single process-global pool bounds the total members in flight via `max_workers`; nested groups drain serially in their own worker and never re-enter the pool (which is exactly why the shared pool is deadlock-free — no separate semaphore needed).

## Consequences

- Field naming is uniform: the intent is `SpawnSubtasksDecision.concurrent` on the transient side and `SubtaskGroupCompleted.concurrent` (`__canonical_omit_none__`) on the persisted side, bridged by `handle_spawn_subtasks`. The `parallel()` orchestration strategy sets `concurrent=True`.

- The concurrency ceiling is capped at a single point by the process-global pool's `max_workers` (`NOETA_MAX_SUBTASK_CONCURRENCY`); nesting does not amplify it. To turn concurrency off entirely and revert to the serial drain, use `NOETA_SUBTASK_CONCURRENCY`.

- Remember the honest determinism boundary: what is guaranteed is "fold/resume reproduces that recorded order," not "two live runs are byte-identical." Any future change that introduces "re-derive state by re-running subtasks" would break this free determinism and must re-evaluate the ordering question.

- The only component that must be hardened as concurrency evolves is `ChildLifecycleObserver`; the new cross-thread invariants (lineage mutations serialized, group wake made exactly-once via `_group_woken`, `SubtaskCompleted` emit kept outside the lock) must be preserved whenever this area is touched.
