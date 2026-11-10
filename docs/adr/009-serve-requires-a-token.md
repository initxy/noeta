# ADR-009: The HTTP API always requires a bearer token

- Status: accepted
- Date: 2026-10-28
- Refined by: [ADR-013](013-server-token-kept-as-a-digest.md) (the token is kept as a
  digest; clients take it from `NOETA_SERVER_TOKEN`), [ADR-028](028-no-tenants-in-noeta.md) (tenant tokens are gone),
  [ADR-034](034-the-server-token-file-holds-the-token-and-a-request-may-name-any-directory.md)
  (the file holds the token again; a request may name any directory)

## Context

The HTTP API creates tasks that run shell commands and edit files with the server's rights. A
server listening without authentication, even on loopback, can be driven by any local process
or by a web page through the browser (cross-origin requests, DNS rebinding).

## Decision

- `noeta serve` always requires a bearer token: `NOETA_SERVER_TOKEN`, else one generated once
  into `~/.noeta/server-token` (owner-only) and read by `noeta --server`. `--no-token` is
  accepted only on a loopback address.
- The SDK's `create_app` raises without a token unless the host passes `auth=False`.
- Requests from other web origins (unless allowed) and with an unexpected `Host` on a loopback
  address are refused; no request may start or switch a task into `bypassPermissions` unless
  the server allows it; tasks run only in the server's directory or allowed roots.
- Tenant tokens are stored as SHA-256 digests (ADR-007).

## Rationale

Secure by default costs a single-user setup nothing (the token is made and read
automatically) and closes the cheap attacks: local processes, malicious pages and rebinding.
Making the SDK refuse to build an unauthenticated app keeps embedders from shipping one by
omission.

Rejected: auth off by default on loopback (browsers and local processes reach loopback);
auth only in the `noeta` command and not the SDK (embedders inherit the hole).

## Consequences

- Every client of a server needs its token; `noeta --server` and `noeta acp --server` find it
  in the environment or `~/.noeta/server-token`.
