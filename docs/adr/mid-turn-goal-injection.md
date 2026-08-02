# Mid-turn goal injection: a lease-free request marker the running Engine drains

## Context

Every human-facing message verb before this — `send_goal`, `answer`,
`deliver_event` — required the task to be **suspended** on the matching wake
handle first (`_require_human_suspend` / `_require_external_suspend`). That is
correct for a turn-boundary conversation: the lease-based model
([worker-lease-model](worker-lease-model.md)) drives one turn under a lease and
parks on `NEXT_GOAL_WAKE_HANDLE` between turns, so a follow-up message is an
append to a parked conversation.

It leaves one thing impossible: handing a message to a task **while its turn is
running**. A caller who wants to say "also consider this" mid-turn had to wait
for the turn to finish. Two invariants make the naive fix — "just append a
`MessagesAppended`" — illegal:

- **Single-writer** ([single-writer-invariant](single-writer-invariant.md)): the
  Engine is the sole mutator of `RuntimeState.messages`. A second writer forks
  `fold(events)` from live state.
- **Lease fencing** ([multi-host-lease-fencing](multi-host-lease-fencing.md)): the
  message arrives on a request thread that holds no lease. It cannot write a
  turn-driving event without racing the lease holder.

So mid-turn delivery is not a missing feature so much as a place where
"deliver now" collides with "keep resume deterministic". The design buys the
first without spending the second.

## Decision

**A message handed to a running task is a durable *request marker*, not a
message write. The Engine — the lease holder — turns it into the real message
at a turn boundary.**

Concretely, one verb `inject_goal`, status-dispatched on the folded task:

- **running** → the request thread writes `InjectionRequested(injection_id,
  messages_ref, count)` via `event_log.system_emit` — the same lease-free
  control-plane seam `cancel` uses for `TaskCancelled` — and pokes a
  process-local `InjectionInbox`. It takes no lease and drives nothing.
- **suspended on the next-goal handle** → fall through to `send_goal` (there is
  no turn to inject into; this is an ordinary follow-up).
- **anything else** (terminal / other handle) → the typed `NotResumableError`.

The running Engine drains at the **top of its `run_one_step` loop**, next to the
cancel poll. Each pending injection is delivered as a real `MessagesAppended`
carrying `consumes_injection=injection_id`; fold **appends the message and pops
the pending marker in one reduction**. The drain source is the union of the
in-memory inbox (the live signal — the cross-thread `InjectionRequested` never
folded onto the Engine's in-memory task) and the durable
`GovernanceState.pending_injections` folded from the log (the resume source).

Three properties fall out:

- **Exactly-once, crash-safe.** The marker is durable; it is popped only by its
  consuming append. A crash between request and consume leaves it pending, and
  the resumed turn's drain re-delivers it exactly once. A re-delivered or
  already-consumed marker cannot duplicate the message.
- **No ordering corruption.** The drain runs only at top-of-loop, where the
  prior iteration's tool results are already appended — so an injected `user`
  message can never split an assistant `tool_use` from its `tool_result`.
- **Byte-identical when idle.** A turn with nothing pending emits nothing, so
  every pre-injection recording folds unchanged
  ([replay-verify-tolerance](replay-verify-tolerance.md) holds).

**The interrupted-attempt seal carries pending injections forward.** Step-attempt
recovery ([step-attempt-recovery](step-attempt-recovery.md)) re-bases a crashed
attempt to its pre-attempt baseline, folding the dead window away. An
`InjectionRequested` that arrived *during* that attempt (the common case — mid
LLM round) sits in the dead window and would be lost. Because the drain only
ever consumes at top-of-loop (never inside the dead window), the full-stream
fold's `pending_injections` is exactly the not-yet-delivered set; the seal
overwrites the bounded baseline's copy with it, so the re-drive delivers it once.

## Rationale

- **The request-marker indirection is forced, not chosen.** The single-writer and
  lease invariants rule out a direct append from the request thread. Given those,
  the only shapes are "block until the turn ends" (what we had) or "durable
  request the lease holder consumes" (this). `cancel` already proved the
  lease-free `system_emit` seam is safe for a control-plane fact the Engine later
  reconciles; injection reuses it.
- **The inbox is an accelerator, exactly like `CancellationRegistry`.** It carries
  data (the descriptor) rather than a flag only because the injected message —
  unlike a cancel, consumed by the Engine's own thread — was written on another
  thread and never entered the Engine's in-memory task. Losing the inbox on a
  fresh process costs nothing: the durable `InjectionRequested` drives the drain
  through `pending_injections`.
- **Top-of-loop is the only safe drain site.** Any mid-iteration site risks
  splitting a `tool_use`/`tool_result` pair, which most provider wire formats
  reject. The cancel poll already established this boundary as the cooperative
  interruption point.

## Alternatives considered

1. **Append `MessagesAppended` directly from the request thread.** Rejected:
   violates single-writer and lease fencing; forks fold from live state and
   surfaces as an `llm_args` divergence at resume, far from the cause.
2. **A pure in-memory inbox with no durable event** (the literal
   `CancellationRegistry` shape). Rejected as the *sole* mechanism: a lost cancel
   is a benign retry, but a lost user message vanishes silently with no
   authoritative record to replay from. The durable `InjectionRequested` is the
   truth; the inbox is only the speed-up.
3. **A dedicated message-bearing wake / a new suspend state for "injectable".**
   Rejected: injection must work *without* suspending — the whole point is
   delivery into a live turn — and a new wake type would duplicate the
   `MessagesAppended` machinery every read model and prefetch path already
   understands.
4. **Interrupt the in-flight LLM/tool call to deliver immediately.** Rejected as a
   non-goal: injection is cooperative and lands at the next turn boundary, exactly
   like the cancel poll. Interrupting a round mid-flight buys latency at the cost
   of a partial-round recovery problem the boundary drain avoids entirely.

## Consequences

- `noeta.protocols.events` carries `InjectionRequestedPayload` and a
  `consumes_injection` field on `MessagesAppendedPayload` (omit-none, so historical
  bytes are unchanged). `GovernanceState.pending_injections` is the fold-accumulated
  projection; `noeta.core.fold` reduces both events.
- `noeta.core.engine.Engine._drain_injections` runs at the top of `run_one_step`;
  the Engine takes an optional `injection_inbox` (duck-typed) at construction.
- `noeta.runtime.injection.InjectionInbox` is the per-host accelerator, wired onto
  the host beside `CancellationRegistry` and freed on `cancel` / `close`.
- `InteractionDriver.inject_goal` / `Client.inject_goal` are the verb; a running
  inject returns immediately without a lease, so — unlike `send_goal` — it does
  not drain further approvals.
- Any new event payload must still classify itself for the audit observer
  (`_SUMMARY_FIELDS_BY_EVENT` / `_TYPE_ONLY_EVENTS`); `InjectionRequested` is on
  the value allowlist (`injection_id`, `count`, `messages_ref` — the body stays
  behind the ref).
