# ADR-068: A deny from any Guard wins over an earlier require_approval

- Status: accepted
- Date: 2026-10-26

## Context

Refines [ADR-010](010-guard-observer-hooks.md), where the first non-allow verdict decided and lower-priority Guards were not consulted. The permission Guard (priority 20) returns `require_approval` in `default` mode, so a host's `PreToolUse` deny rule (the hook Guard, priority 100) was never reached: approving the call ran exactly what the deny rule existed to block, and the stricter mode was the weaker one.

## Decision

- The chain evaluates every Guard. Any `deny` — including a Guard that raises — decides. Otherwise the first `require_approval` decides; otherwise `allow`.
- Guards stay pure functions of the proposed action and context; the approval request is raised by the Engine from the returned verdict, so evaluating a Guard whose `require_approval` is then discarded raises nothing.

## Rationale

- A deny rule is a statement that a call must never run; no human approval of a different question ("may this run?") should override it. Claude Code applies deny rules before asking, for the same reason.

## Consequences

- `HookManager.check` in `packages/noeta-runtime/noeta/core/hooks.py`. A custom Guard with side effects in `check` now runs even when an earlier Guard asks for approval.
