# ADR-059: Content retention stays outside the Protocol, and one ref carries no body

- Status: accepted
- Date: 2026-09-27

## Context

Refines [ADR-021](021-storage-protocols-l0.md) (The three storage seams are typed at L0, split by capability, with a dedicated system-write path). Where the two disagree, this record wins.

## Decision

The ContentStore Protocol is still `put` / `get` / `get_many`; what changed is where the lifetime question is answered and what a model call writes.

- **A model call stores no request body.** `LLMRequestStarted.request_ref` is minted from the canonical request bytes (`content_ref_for`, the one home of the hash rule every adapter now mints through) without a `put`. The request is the whole View — different on every step, derivable from folded state plus the composer, read back by nothing — so storing it grew the store by the square of a task's length for no reader. The hash is unchanged, so replay identity and derive-and-compare are too. `RuntimeLLMClient(record_requests=True)` keeps a transient copy for a debugging session.
- **Reclaiming is a maintenance affordance, not a Protocol member.** `sweep(live, *, grace_seconds, vacuum)` on the built-in stores — duck-typed exactly like the event log's `purge_task` — and `noeta.storage.gc.collect_garbage` (`Client.collect_garbage`) as the one mark-and-sweep over it. The mark walks every event payload for refs and closes over the bodies textually, because a tool output's shape is plugin-defined and keeping a blob whose hash merely appears in a tool's text is the cheap direction of the error. Two references are deliberately not roots: a request ref, and any `TaskSnapshot.state_ref` but a task's latest — fold rebuilds from the events when a snapshot body is gone, while the re-base bodies (`TaskRewound` / `StepAttemptAbandoned` / `TaskForked`) are never reclaimed.
- **The grace is what makes a sweep safe while turns run.** Every `put`, including a dedup hit, stamps the row's `touched_at`; only rows older than the grace are candidates; the delete re-evaluates the age under the adapter's write lock. The alternative — refusing while any lease is live — would make the sweep a never-runs on a busy host and still leave the dedup race (an orphan row re-`put` by a live turn between mark and sweep). A third-party store without `sweep` makes the call report `unsupported` rather than raise, so a host can wire it unconditionally.

The point in [ADR-021](021-storage-protocols-l0.md)'s Rationale that "a minimal ContentStore keeps reclamation out of the contract" still holds: the deployment still decides *when*; the runtime now supplies the *what* and the *how* once, instead of leaving every host to walk the refs itself.
