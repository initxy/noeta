# ADR-056: Dropped connections

- Status: accepted
- Date: 2026-09-25

## Context

Refines [ADR-034](034-multi-host-lease-fencing.md) (Multi-host Postgres deployments fence lease-checked writes with an in-transaction row-share check on the dispatcher row, not an epoch column). Where the two disagree, this record wins.

## Decision

Each Postgres adapter still holds one connection behind its own lock (no pool), but a dropped connection — server restart, idle kill, network reset, `pg_terminate_backend` — is no longer fatal for the rest of the process. The connection is reopened and the work retried at most once, under rules that keep [ADR-034](034-multi-host-lease-fencing.md)'s guarantees:

- A statement outside a transaction is re-sent once on the fresh connection.
- A transaction that loses its connection before `COMMIT` is sent is re-run from `BEGIN`. The server discarded it, and the advisory locks and the `FOR SHARE` fence probe are transaction-scoped, so the re-run takes them afresh and re-checks the lease; a zombie cannot slip through on a retry.
- A connection lost while `COMMIT` is in flight leaves the outcome unknown, so the error propagates (the connection is still reopened for the next call). The one exception is an append carrying an idempotency key: its re-run finds the stored event through the key instead of appending a second one, so it is retried. Nothing is retried blindly — no double append, no double lease.
- Only connection-level failures qualify (`OperationalError` / `InterfaceError` on a closed or broken connection, SQLSTATE class `08`, `57P01`–`57P03`). `LockNotAvailable` from the row-lock timeout, query cancels and data errors propagate as before.

Pooling stays a separate design.
