# ADR-013: The server token is kept as a digest; clients bring it themselves

- Status: superseded by ADR-034
- Date: 2026-10-28
- Refined by: [ADR-028](028-no-tenants-in-noeta.md) (one token, no tenant tokens)
- Refines: [ADR-009](009-serve-requires-a-token.md)

## Context

ADR-009 had `noeta serve` generate its bearer token once into `~/.noeta/server-token` and
`noeta --server` read it back from there. A file holding the token itself lets anything that
can read the OS user's files drive the server as the default tenant, which can reach every
task, and on a server with tenants their tools run as that same OS user.

## Decision

- `~/.noeta/server-token` (owner-only) keeps only the token's SHA-256, as `sha256:<hex>`,
  like tenant tokens in `tokens.json`. The token is printed once, when it is made.
- A file an older noeta left holding the token itself is rewritten to its digest on the next
  start, which prints the token one last time.
- Clients (`noeta --server`, `noeta acp --server`) take the token from `NOETA_SERVER_TOKEN`
  (`noeta/cli/server_token.py`); the file serves them only while it still holds an
  older noeta's plain-text token, until a server start migrates it. Deleting the file and
  restarting makes a new token.

The rest of ADR-009 stands: a token is always required, `--no-token` only on loopback, and
`create_app` refuses to build an unauthenticated app unless told `auth=False`.

## Rationale

No file on the server then holds a credential that a tenant's file tool or a stray backup
could use; the server compares digests only. The cost is one step for a single-user setup:
copying the token printed at first start into the client's environment.

Rejected: keeping the plain-text file for local clients (it is the credential a tenant on a
shared OS user could read); a separate client-side token file (a second place to protect,
and two copies to keep in step).

## Consequences

- A lost token cannot be recovered, only replaced (delete the file, restart).
- `docs/guides/server.md` documents the client side.
