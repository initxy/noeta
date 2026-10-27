# ADR-057: Construction-time recovery reads tails, and only for waiting parents

- Status: accepted
- Date: 2026-09-25

## Context

Refines [ADR-041](041-worker-queue-routing.md) (Worker queue routing: named queues route work, they do not fence it). Where the two disagree, this record wins.

## Decision

The construction-time pass that emits handoffs a crashed process left missing
no longer reads every stream in full. It reads the last 32 envelopes of each
stream (`read(task_id, after_seq=last_seq - 32)`, a form every adapter already
supports) to find the newest lifecycle event, and only a parent whose newest
event is a `TaskSuspended` on `SubtaskCompleted` / `SubtaskGroupCompleted`
yields candidates: its not-yet-recorded members, each checked for a terminal
event through the same tail read. Only those candidates go through the full
`_on_terminal` path with its durable dedupe. A stream whose tail holds no
lifecycle event falls back to a full read. On a store of 300 finished root
tasks this took the pass from 496 full reads (45,428 envelopes) to 347 reads,
46 of them full (10,061 envelopes).

The narrowing is also a semantic change, accepted on purpose: a child that
finished after its parent stopped waiting (interrupted and released, or woken
by something else) no longer gets a `SubtaskCompleted` + wake written onto the
parent stream at construction. That wake never matched the parent's current
condition, so the only loss is one audit row that recorded a completion nobody
was waiting for.
