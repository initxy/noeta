# ADR-070: A caller idempotency key on send_goal and inject_goal, recorded on the event it guards

- Status: accepted
- Date: 2026-10-26

## Context

Refines [ADR-040](040-mid-turn-goal-injection.md). `send_goal` and `inject_goal` minted a fresh id per call, so a client retrying after a timeout appended the user's message twice. The EventLog's dedup table existed but served lease-checked appends only.

## Decision

- Both verbs take `idempotency_key: Optional[str]`. A repeat with the same key on the same task appends nothing and returns the task's outcome as the first call left it. Unkeyed calls are byte-identical to before.
- The key is recorded on the event it guards — the goal's `MessagesAppended` or the `InjectionRequested` — so a repeat is found in the stream by any process.
- Racing duplicates are settled by the EventLog: `system_emit` takes an optional `idempotency_key`, deduplicated through the existing table under a reserved `system` scope, so exactly one writer appends. A keyed inject writes its request this way; a keyed `send_goal` first claims the key with an `IdempotencyKeyClaimed` event, after validation and before the wake, and losers wait briefly for the winner's goal.

## Rationale

- The stream is the only state every process shares, so the key belongs on the event; a claim before the wake is needed because two seeds racing on one task can otherwise take over each other's lease.

## Consequences

- A storage-protocol addition (`system_emit(idempotency_key=...)`) implemented by every EventLog backend, and one new event type. A repeat of a call that failed returns that failure, except a seed that recorded the goal before any step ran, which the repeat drives as an unkeyed retry would.

## Refined by

- [ADR-072](072-dead-idempotency-claim-is-claimed-again.md) — A keyed send_goal whose goal never landed is claimed again by its retry
