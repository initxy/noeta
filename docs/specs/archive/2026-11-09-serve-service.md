---
status: done
created: 2026-11-09
branch: feat/serve-service-2026-11-09
---

# One server command: `noeta serve`, `noeta service`, scratch sessions

## Goal

- `noeta serve` is the only way to run the backend for the web and desktop apps. Every start
  prints a ready-to-open address with the token (`http://<host>:<port>/#token=…`).
- `noeta service install|uninstall|start|stop|restart|status|logs` runs `noeta serve` as an OS
  user service (systemd user unit on Linux, launchd LaunchAgent on macOS): starts at login/boot,
  restarts on crash. `noeta update` restarts an installed service after upgrading.
- `--host 0.0.0.0` (or any non-loopback bind) answers this machine's own hostnames and IPs
  without `--allow-host`.
- A session created without a directory runs in `<NOETA_HOME>/scratch/<YYYY-MM-DD>/<short id>/`,
  and the web app lists those under one "Scratch" group by date.
- Released as **1.2.0** (removes a shipped command).

## Non-goals

- Desktop connecting to a remote server.
- System-wide (root) services, Windows services.
- Any restriction on which directory a task may run in.

## Context

- 1.1.2 added `noeta web` (background loopback server, pid file, `stop`/`status`); the owner
  decided one command (`noeta serve`) plus OS service management is the right shape.
- `--allow-root` and the cwd check in `noeta/host/_http_guard.py` (`check_cwd`) came from the
  tenant era (ADR-021, superseded by ADR-028). A token holder can run any shell command, so the
  check guards nothing; it only refuses folders in the web app.
- ADR-013 keeps `~/.noeta/server-token` as a digest so tenants on one OS user could not read
  the token; tenants are gone (ADR-028).
- This server: `systemctl --user` works and `Linger=yes`.

## Decisions

- Remove `noeta web` (CLI, docs, tests, its restart in `noeta update`) and `--allow-root`
  (CLI flag removed outright; `GuardedClient` no longer checks a cwd's location — only that a
  given `cwd` exists and is a directory; relative `cwd` still resolves against the server's
  directory). The desktop app stops passing `--allow-root` and no longer restarts its server for
  folders outside home.
- New ADR supersedes ADR-013: `~/.noeta/server-token` holds the token itself, mode 0600; an
  existing digest-only file is replaced by a new token on the next start (printed). Clients on
  the same machine (`noeta --server`, `noeta acp --server`) read it when `NOETA_SERVER_TOKEN` is
  unset. `NOETA_SERVER_TOKEN` set on the server still wins and is not written to the file.
- Every `noeta serve` start prints, on stderr, one line per reachable address with `#token=`:
  loopback bind → `http://127.0.0.1:<port>/#token=…`; non-loopback → one line per hostname/IP
  it answers. `--no-token` prints the address without a token.
- Non-loopback bind: allowed Host names = `--allow-host` values ∪ this machine's hostname, FQDN
  and its interface IPs (plus loopback names). The "any Host header is answered" warning goes.
- `noeta service install [serve flags…]` writes `~/.config/systemd/user/noeta.service`
  (`ExecStart=<absolute noeta> serve <flags>`, `Restart=on-failure`, `WorkingDirectory=$HOME`,
  environment: the `NOETA_*`/provider variables and `PATH` present at install time, written to an
  owner-only env file next to the unit) then `daemon-reload` + `enable --now`; macOS writes
  `~/Library/LaunchAgents/dev.noeta.serve.plist` (`KeepAlive`, `RunAtLoad`, logs to
  `~/.noeta/logs/serve.log`) and `launchctl bootstrap`. Re-running `install` replaces it.
  `status` prints the service state and the address lines (from the token file and the unit's
  flags). `logs` → `journalctl --user -u noeta -n 200 -f` / tail of the log file. Without
  systemd user or launchd: one line saying so and suggesting `tmux` + `noeta serve`. Linux
  without linger: say that the service stops at logout and print `loginctl enable-linger`.
- `noeta update`: after a successful upgrade, if the service is installed, restart it.
- Scratch: a task created through HTTP / `Client` with no `cwd` gets
  `<NOETA_HOME>/scratch/<YYYY-MM-DD>/<first 8 of task id>/`, created on demand. `GET /projects`
  never lists scratch directories as projects; it appends one entry
  `{path: <scratch root>, name: "Scratch", saved: false, sessions: n, scratch: true}` when n > 0
  (other entries get `scratch: false`). The web app shows it as a "Scratch" group, subgrouped by
  date, after the projects; "New session" with no project selected creates a task without `cwd`.
- Version 1.2.0.

## Plan

- [x] Python: remove `noeta web` + `--allow-root` + cwd location check (desktop's server args: the Desktop item)
- [x] Python: token file holds the token (new ADR superseding 013), clients read it, address lines on start, auto Host names
- [x] Python: `noeta service` (systemd user / launchd) + `noeta update` restart
- [x] Python: scratch directory for tasks without `cwd`; `/projects` scratch entry
- [x] Web: Scratch group; new session without a project; drop `noeta web` mentions and refusal-hint UI that no longer applies
- [x] Desktop: no `--allow-root`, no restart for outside-home folders
- [x] Docs (en + zh): front-ends, server guide, quickstart, CLI reference, troubleshooting, HTTP API; CHANGELOG
- [x] Release 1.2.0 (upgrading this server and installing its service follow the publish, outside the repo)

## Acceptance criteria

- `noeta serve` prints a working `#token=` address on every start; restarting keeps the same token.
- On this Linux server, `noeta service install --host 0.0.0.0` → `systemctl --user is-active noeta`
  is active, the page answers on the machine IP, `status` prints addresses, `uninstall` removes it.
- `POST /projects` and task creation accept any existing directory; a task without `cwd` lands in
  today's scratch directory and the web app shows it under Scratch.
- `noeta web` and `--allow-root` are gone from code and docs (CHANGELOG notes the removal).
- `make check` and `make frontends` green.

## Risks

- Token file in plain text: anyone who can read the OS user's files can drive the server — the
  same user can already run anything as that user.
- Removing a shipped command and flag in a minor release.
- Service env snapshot: provider keys exported in a shell rc file at install time are copied into
  an owner-only env file; changing keys later needs `noeta service install` again.

## Progress log

- 2026-11-09 owner: drop `noeta web` and `--allow-root`, serve + OS service, scratch dirs by date, release 1.2.0.
- 2026-11-09 Python half (branch `feat/serve-service-py`): items 1–4 done; docs except
  `docs/*/guides/front-ends.md` (the UI agent's) updated, CHANGELOG Unreleased written. The
  Docs item stays open until front-ends lands. ADR-034 supersedes ADR-013 and refines
  ADR-009/028 (the directory check went with the token's digest).
  Choices within the Decisions:
  - A cwd that does not exist or is not a directory is `400` (a WebSocket closes `4403`, as
    other refusals); `POST /projects` also refuses a scratch directory (`400`).
  - The scratch directory's id is known before creation: the guard picks the task id itself
    (`uuid4().hex`, as `Client.create` would) and makes the directory before creating the task;
    a repeated `dedupe_key` removes the empty directory it made.
  - Only `GuardedClient.create` (POST /tasks, stream-json, rewind without a cwd) gets scratch
    directories; `create_app(scratch=None)` keeps the old behaviour for embedders.
  - Address lines: a wildcard bind prints hostname, FQDN, addresses (routing-table probe plus
    the hostname's resolved addresses, looked up in a thread, 5 s cap), `--allow-host` names,
    then `127.0.0.1`; a specific non-loopback address prints itself and `--allow-host` names.
  - `noeta service install` makes the token before starting the unit (unless `--no-token` or
    `NOETA_SERVER_TOKEN` is in the copied env), so `install` and `status` show working
    addresses before the server's first start; `status` takes `NOETA_SERVER_TOKEN` from the
    unit's env file when it is set there. Shared flags (`--model` …) go before `service`
    (`noeta --model m service install …`) and are written into `ExecStart` before `serve`.
  - `logs` follows by default as the spec says; `--no-follow` prints the last 200 lines.
  - Friction: the token appears in the service's journal / log file on every start (the
    address lines go to stderr); same user, same exposure as the token file.
- 2026-11-09 integration: the first scratch session never showed (projects fetched only on mount/add/remove); the app now re-asks /projects once for a session folder no project covers. Web e2e 20/20, desktop e2e 4/4.
