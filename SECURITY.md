# Security Policy

noeta runs agents that execute real tools — shell commands, file edits, web requests, MCP
servers — on the machine that runs them. We want security reports, and we want them privately.

## Supported versions

One package, `noeta`. 1.0 is in development on `main` (`1.0.0.dev0`) and not yet on PyPI.
Fixes land on `main`; there are no backports.

| Version | Supported |
|---|---|
| latest `main` | yes |
| the newest 1.x release, once published | yes |
| 0.x (`noeta-runtime` / `noeta-sdk`, up to `v0.6.42`) | no — a different codebase |

## Reporting a vulnerability

**Do not open a public issue for a vulnerability.** Report it through GitHub's private
vulnerability reporting: the repository's
[Security tab → Report a vulnerability](https://github.com/initxy/noeta/security/advisories/new).
If you cannot use GitHub advisories, email `initxy0@gmail.com`.

A useful report says what the issue is, the commit or version, a reproduction, and the impact
you think it has. We aim to acknowledge within a few business days and keep you updated; please
give us reasonable time to ship a fix before disclosing. We are happy to credit you.

## The security model

What noeta defends, so you can tell a vulnerability from intended behavior:

- **Model output and task input are untrusted.** They drive tool calls, so every call goes
  through the permissions plugin's gates (Claude Code's permission modes and `allow` / `ask` /
  `deny` rules; deny always wins, including over a hook or an approval). A way for model output
  to run a command, write a file, or reach a host that the rules and mode should have stopped
  is a vulnerability.
- **The sandbox** (`sandbox.enabled` in settings: bubblewrap on Linux, Seatbelt on macOS)
  confines `Bash` to the working directory and allowed paths, with network only to allowed
  domains through a filtering proxy. Paths that would run code later without a person's
  confirmation (`.noeta/`, `.git/hooks`, `.git/config`, package-manager and build-tool settings)
  stay read-only, and missing ones are covered by empty read-only placeholders so they cannot be
  created. An escape from the sandbox, or a write to one of these paths from inside it, is a
  vulnerability. Without the sandbox, commands run with your user's rights after approval —
  that is intended.
- **An untrusted project configures nothing.** A cloned repository's `.noeta/` settings,
  agents, prompts, skills and plugins load only after `noeta trust`; its MCP servers start only
  with consent in its own settings. noeta never reads Claude Code's files (`~/.claude`,
  `.claude/`, `CLAUDE.md`, `.mcp.json`).
- **`noeta serve` always requires a bearer token** (`NOETA_SERVER_TOKEN`, or one made on first
  start and printed once; `~/.noeta/server-token`, owner-only, keeps only its SHA-256);
  `--no-token` is accepted only on a loopback address. Only the WebSocket route also takes the token as
  `?token=` (a browser cannot set the header there); `noeta serve` logs it as `REDACTED`
  (host code serving `create_app` calls `noeta.host.http.redact_token_logs`).
  Other web origins and unexpected `Host` headers are refused, no request may start or switch a
  task into `bypassPermissions` unless the server allows it, and tasks run only inside the
  server's directory or `--allow-root` paths. Tenant tokens (`noeta token add`) are stored as
  digests and confine every request to that tenant's tasks, workspace and queues; a request
  sets only the task config and signals a client may. A way around any of this is a
  vulnerability.
- **Tenants are separated at the API, not by the OS.** Their tools run as the worker's OS
  user, so without a boundary a tenant can read what that user can (`~/.noeta`, the store, the
  worker's environment). `noeta serve` and `noeta worker` refuse other tenants unless the
  sandbox confines every command, or the operator sets `[server] shared_os_user_tenants =
  true` (tenants then trust each other). A non-default tenant's task may never reach noeta's
  home, the store's files, `/etc/noeta`, `/proc`, other tasks' spilled outputs or another
  tenant's workspace, whatever its rules, mode or approvals: not with a file tool (fenced
  in-process), and under the sandbox not with a command (`Bash`, a skill's script, `git` and
  its hooks), and it starts no local MCP server (`docs/server.md`, "What separates tenants",
  lists what is and is not a boundary). A tenant reaching another tenant's tasks through the
  API, or those places with a tool, is a vulnerability; a tenant reading the OS user's other
  files after the operator chose shared OS users, or through what the operator configured to
  run outside the sandbox (hooks, MCP servers of the default tenant), is not.
- **Plugins are code you install.** They run in-process with noeta's rights; installing one
  (`noeta plugin add`, a plugin file, a package) is trusting it. noeta runs nothing from a plugin
  while installing it, and a machine policy (`/etc/noeta/config.toml` `[policy]`) can allow or
  block plugins by name. The policy matches the name a plugin declares, not the file, package or
  repository it came from, so a blocked plugin under another name is not blocked (an allow list
  is what admits only known plugins).
- **Nothing is sent anywhere by default** except requests to your configured model provider.
- **Secrets** in configuration are written as `{env:NAME}` or `{keyring:service/user}`
  references and resolved only when a plugin is made, so they stay out of files and task logs.

Out of scope: what a person explicitly approves or configures (`bypassPermissions`, broad allow
rules, `dangerouslyDisableSandbox` approvals), the behavior of a model provider, and attacks
that need write access to `~/.noeta` or the store.
