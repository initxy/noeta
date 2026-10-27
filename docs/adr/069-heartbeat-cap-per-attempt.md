# ADR-069: The heartbeat cap bounds one step attempt, not one lease, and hitting it goes through recovery

- Status: accepted
- Date: 2026-10-26

## Context

Refines [ADR-029](029-worker-lease-model.md), where renewals were capped per lease (`heartbeat_max`, default 360 × 30 s ≈ 3 h) and the dispatcher force-released the task to `suspended(lease_quota_exceeded)`. A long healthy turn hit that ceiling, and the outcome depended on how it was driven: an in-request drive failed the task, a woken turn was re-driven while the old thread still ran, and a first turn left the row suspended while fold said running, so later goals were refused or silently dropped.

## Decision

- The cap counts heartbeats within one step attempt — one model call plus its tool calls — and restarts when the task composes its next model call. It lives in the worker's heartbeat runner, the only place that sees attempts start; the `Dispatcher` protocol is unchanged.
- It is host configuration: `HostConfig.attempt_heartbeat_max` (default 360; `None` disables it), reaching the in-request drive, the subtask drain and `WorkerLoop`.
- Hitting it hands the lease back to the ready queue and stops the step; the task is then recovered as a crashed attempt is ([ADR-032](032-step-attempt-recovery.md), [ADR-065](065-crash-recovery-keeps-finished-tool-calls.md)). Dispatchers default `heartbeat_max` to `None`; if set, reaching it returns the row to ready instead of `suspended(lease_quota_exceeded)`.
- A heartbeat that hits a non-lease storage error retries with backoff instead of ending the heartbeat thread; an `InvalidLease` marks the lease lost and the step's cancel check stops it at the next boundary.

## Rationale

- The cap exists to bound a wedged attempt, not a long conversation turn; counting per attempt keeps that protection without a wall-clock ceiling on legitimate work, and routing through recovery gives one outcome however the turn was driven.

## Consequences

- Heartbeat runner and recovery hand-off in `packages/noeta-runtime/noeta/runtime/worker.py`; new reliability kinds `heartbeat_failed` and `attempt_heartbeat_cap`.
