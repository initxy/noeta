# ADR-034: The server token file holds the token, and a request may name any directory

- Status: accepted
- Date: 2026-11-09
- Supersedes: [ADR-013](013-server-token-kept-as-a-digest.md)
- Refines: [ADR-009](009-serve-requires-a-token.md), [ADR-028](028-no-tenants-in-noeta.md)

## Context

ADR-013 kept only the token's digest in `~/.noeta/server-token` so that tenants sharing one OS
user could not read a credential reaching every task; clients had to carry the token printed
once at first start in `NOETA_SERVER_TOKEN`. ADR-009 also kept tasks in the server's directory
or `--allow-root` directories. Both guarded against tenants, which ADR-028 removed. A token
holder can run any shell command as the server's user, so the directory check guarded nothing
and only refused folders in the web app; the digest made a lost token unrecoverable and every
restart of a service unable to show its address again.

## Decision

- `~/.noeta/server-token` (owner-only, mode 0600) holds the token itself. A file an older
  noeta left holding only a digest is replaced by a new token on the next start.
- `NOETA_SERVER_TOKEN` set on the server still wins and is written nowhere.
- Clients on the same machine (`noeta --server`, `noeta acp --server`) take
  `NOETA_SERVER_TOKEN`, else the file (`noeta/cli/server_token.py`).
- Every `noeta serve` start prints the addresses to open with the token in the fragment
  (`http://<host>:<port>/#token=…`); `--no-token` (loopback only) prints them without.
- A request's `cwd` (a task's, a project's, the folder picker's) may be any directory; only one
  that does not exist or is not a directory is refused. `--allow-root` is gone.
- On a bind that is not loopback the server answers the loopback names, this machine's
  hostname, fully qualified name and addresses, and `--allow-host` names; any other `Host` is
  refused (DNS rebinding).

The rest of ADR-009 stands: a token is always required unless `--no-token` on loopback, and
`create_app` refuses to build an unauthenticated app unless told `auth=False`.

## Rationale

Whoever can read the OS user's files can already run anything as that user, so a plain-text
file owner-only gives away nothing new once no tenants share the user; it lets a restarted
service print a working address and lets local clients connect with no setup. Without tenants
a directory fence is a refusal without a threat.

Rejected: keeping the digest and a second client-side token file (two places to protect and
keep in step); keeping the directory check as an option (an option guarding nothing).

## Consequences

- Deleting the file and restarting makes a new token; the old one stops working.
- A backup of the home directory carries a live credential for the server.
