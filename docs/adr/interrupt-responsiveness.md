# Interrupt reaches the turn boundary promptly: abandonable provider waits, cancel-aware seams, and force-stop as enqueue

## Context

`interrupt` is a cooperative flag: a durable `TurnInterrupted` plus a
process-local registry mark, polled by the Engine at two turn boundaries — the
top of the compose→decide loop and right after `Policy.decide` returns
([mid-turn-goal-injection](mid-turn-goal-injection.md) established that
boundary as the cooperative interruption point). Everything between the polls
was a blind window, and the windows were long:

- The in-flight LLM round ran to completion. All builtin providers are
  synchronous `httpx` SSE drains on the step thread, and the client timeout
  measures *silence*, not total generation — a long answer streamed for
  minutes after Esc, deltas still reaching the UI for a round already doomed.
- The transient-retry loop sat inside the same window: up to 8 further
  provider calls plus ~2 minutes of bare `time.sleep` backoff, no cancel
  check anywhere.
- A `tool_calls` batch executed with zero polls, and a foreground shell
  (up to 600 s) was untouched — interrupt reaped only *background* jobs.
- The dispatcher lease — the session's mutual exclusion — released only when
  the step finally unwound, so the conversation stayed locked exactly as
  long as the blind window lasted.

The runtime is deliberately synchronous and thread-based (no asyncio), so an
async runtime's first-class abort (`AbortController`) is not available, and
Python threads cannot be killed.

## Decision

**Interrupt still lands at the turn boundary; the boundary is reached
promptly, because every blocking wait between the polls is made
cancel-aware.** Four seams plus one escalation:

1. **The LLM round is an abandonable wait.** `StepContext` carries the
   Engine's cooperative-cancel predicate (`cancelled`, `None` on
   resume/replay). When present, `RuntimeLLMClient` runs the provider call on
   a daemon I/O thread and waits in short slices, polling the predicate; a
   truthy poll walks away — mutes the delta sink, returns a
   `category="aborted"` error response (the recorded trio stays well-formed),
   and the Engine's existing post-`decide` poll abandons the whole decision.
   No new Engine control flow. Abandonment is *safe by contract*:
   `LLMProvider` is pure — no EventLog writes, no StepContext — so an orphan
   call's return value simply has no consumer. This is what makes interrupt
   land in milliseconds in **any** phase, including the pre-first-byte
   silence no chunk-loop check can cover.
2. **The orphan dies fast.** `StreamingProvider.complete_streaming` folds in
   an optional `should_abort` predicate (same no-probe-matrix rationale as
   `request_headers`); adapters poll it per SSE event and raise
   `AbortedError` from inside their stream context, closing the connection —
   damage control for the token burn, not the latency path. The runtime
   probes the adapter's signature and withholds the argument from pre-abort
   adapters, so third-party providers keep working unmodified.
3. **The retry loop is cancel-aware.** Each attempt re-checks the predicate,
   the backoff sleep is sliced around it, and `AbortedError` is never
   retried. An interrupt during a rate-limit backoff exits within one slice.
4. **Tool batches poll between calls; shells die.** A stop landing during
   call N of a batch closes calls N+1… with paired `success=False`
   interrupted results (the same balance-the-batch move the approval suspend
   makes) and unwinds; a foreground shell's process group is registered and
   reaped exactly like a background job's, so the blocked `communicate()`
   returns immediately.

Two arm-side gates were also wrong and are part of this decision:
`_turn_in_flight` treats a folded `running` status (the `release_yield`
hand-off window, where no lease is active) and a delegation-suspended root
(the turn lives in the children) as turns in flight; and an interrupted —
not cancelled — delegation drain now settles its non-terminal root at the
interrupted next-goal suspend, dangling spawn calls closed, mark discarded.

**`interrupt(force=True)` — the double-Esc escalation — is three existing
primitives, no new machinery.** For a step wedged past every cooperative seam
(a tool ignoring its timeout): `dispatcher.enqueue` force-clears the wedged
lease — the documented force-clear, which fences the abandoned thread
([multi-host-lease-fencing](multi-host-lease-fencing.md)): its later writes
raise `InvalidLease` and land nowhere; a fresh targeted lease runs the
standard woken reconciliation, whose wake-less `running` branch **is**
step-attempt recovery ([step-attempt-recovery](step-attempt-recovery.md)) —
the dirty window is sealed, and the re-drive, running under the still-armed
registry mark, aborts on its first poll and settles
`suspended("interrupted")`. A window that classifies unsafe (an interrupted
approval) parks with a notice instead — equally a settled, resumable stop.
The lost-lease terminal converger (`_force_terminal_on_lost_lease`)
consequently no-ops on a *suspended* fold: a durable suspend is a resumable
landing, and the fenced zombie's own `InvalidLease` must not bulldoze it.

## Rationale

- **Abandonment over cancellation.** Threads cannot be killed and sync
  sockets cannot be aborted from outside, but a *pure* callee does not need
  either: stop waiting, and the result is garbage-collected disappointment.
  The provider purity contract — written long before this ADR for
  recording-layer reasons — is what makes the wait abandonable at zero
  correctness cost. Side-effectful callees (tools) get the opposite
  treatment: kill the subprocess where one exists, otherwise fence-and-seal.
- **This does not reopen mid-turn-goal-injection's rejection.** That ADR
  rejected aborting the in-flight round *to deliver a message into it* — a
  partial-round recovery problem, because injection wants the round's value.
  Interrupt discards the round by definition (the post-`decide` poll already
  abandoned the decision before this work); returning early loses nothing.
  The boundary semantics, the events, and the replay tolerance are unchanged
  — `cancelled=None` paths stay byte-identical.
- **Force-stop composes instead of inventing.** Enqueue's force-clear, the
  lease fence, and attempt recovery each already carried exactly the
  guarantee the escalation needs (strip, fence, seal). Wiring them behind an
  explicit `force=True` keeps the risky move out of the default path and
  keeps its blast radius inside machinery that crash recovery already tests.

## Alternatives considered

1. **Interruptible transports only (chunk-loop checks, no abandonable
   wait).** Rejected as the primary mechanism: covers mid-stream but not the
   pre-first-byte silence or a wedged connect, leaving the worst-case latency
   at the read timeout. Kept as the orphan-shutdown layer (decision 2).
2. **Rebuild the runtime on asyncio for first-class cancellation.** Rejected:
   the synchronous thread model is load-bearing (deterministic replay,
   lease-scoped worker threads, no event-loop coloring of every seam);
   abandonment buys the same user-visible latency without the rewrite.
3. **Release the session lease immediately on every interrupt** (the
   original product ask). Rejected as the default: with the cooperative
   seams in place the lease releases within milliseconds anyway on the
   dominant paths, while an unconditional early release would manufacture a
   dirty recovery window per interrupt and let a new turn race a
   still-running tool's side effects on the same workspace. Retained,
   scoped, as `force=True` for the genuinely wedged case.
4. **A cancel token in the base `LLMProvider.complete` signature.**
   Rejected: a breaking change to every adapter for a guarantee the runtime
   cannot enforce (a provider may ignore the token), when the runtime-side
   abandonable wait needs no provider cooperation at all.

## Consequences

- `noeta.protocols`: `StepContext.cancelled` (pure addition);
  `AbortedError` / `CATEGORY_ABORTED` in the error taxonomy;
  `StreamingProvider.complete_streaming` gains optional `should_abort`.
- `RuntimeLLMClient` owns the abandonable wait, the sliced backoff, and the
  delta-sink mute; recordings and the three-event trio are unchanged on
  every path, and `ctx.cancelled is None` reproduces the historical inline
  behavior byte-for-byte.
- `handle_tool_calls` takes the predicate and closes stopped batches;
  `driver.interrupt` takes `force`; the interrupted-delegation settle lives
  in the drain (`_settle_stopped_root`).
- The compaction summarize call inherits cancellability through its
  `StepContext`; the memory recall judge gets a bounded abort-aware wait in
  its host wiring.
- Deployment note: an in-request drive whose step was force-stopped surfaces
  `InvalidLease` to the original transport call when the zombie finally
  returns — by design; the task itself is already settled and resumable.
