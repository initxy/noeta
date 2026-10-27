# ADR-072: A keyed send_goal whose goal never landed is claimed again by its retry

- Status: accepted
- Date: 2026-10-26

## Context

Refines [ADR-070](070-caller-idempotency-key-on-goals.md). A keyed `send_goal` claims its key before the wake. When the call then dies before its goal lands — a crash, or a seed failure compensated back to the next-goal suspend — the claim stayed on the stream forever. Every retry found it, waited for a goal that would never come, and returned success with no goal written.

## Decision

- A claim whose goal never landed is dead once the task has suspended since the claim, or once the claim is older than the window a duplicate waits for its holder's goal (`_IDEMPOTENT_WAIT_S` in `noeta.execution.driver`). A retry with the same key then claims the key again and writes the goal once. A retry that meets a claim that is still live waits for it as before, and claims again if the claim goes dead while it waits.
- The new claim is deduplicated in the store on the key plus the dead claim's id. Retries that saw the same dead claim race for one slot, so exactly one of them writes the goal.
- A keyed call whose wake loses to another call gets the refusal unless a call holding the same key won. Its own claim does not count.

## Rationale

- The stream has no liveness signal for a claimer. A suspend after the claim proves the claimer is gone. Without one, the claim's age is the only evidence, and the wait window is the time duplicates already give the holder.

## Alternatives considered

1. **Release the claim with an event when a seed fails.** Rejected: a crash writes nothing, so a retry still needs a rule for a claim that was never released.

## Consequences

- A retry after a claim that died before its wake waits out the window before it writes the goal.
