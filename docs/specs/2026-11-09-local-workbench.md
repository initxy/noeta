---
status: active
created: 2026-11-09
branch: feat/local-workbench-2026-11-09
---

# Local workbench: one-command web app, self-update, quiet errors

## Goal

A user who ran `uv tool install 'noeta[tui,server]'` gets, with no build step:

- `noeta web` — the web app opens in the browser against a background local server; `noeta web stop` / `noeta web status`.
- A web and desktop UI shaped like a local workbench (Codex's desktop app): a left column of projects (folders), each with its sessions; add a project, start a session in it.
- `noeta update` — upgrades noeta in place, keeping the extras it was installed with.
- An unexpected error prints one line and where the full report went, not a Python traceback.
- Desktop installers attached to each GitHub Release.

## Non-goals

- Signing / notarizing the desktop installers (unsigned; maintainer's call later).
- Git worktrees, branch pickers, cloud environments, a terminal pane or a file tree in the web app.
- Auto-update inside the desktop app; update notifications in the TUI.
- Changing `noeta serve`'s deployment behavior or its defaults beyond serving the bundled web app.

## Context

- `noeta serve` (`noeta/cli/main.py`, `noeta/host/http.py`) serves the HTTP API and, with `--web DIR`, static files. The web app (`web/`) is built from source only; the wheel ships no web files.
- The web app was aligned with Claude Code's web app: a session list grouped by directory, no project concept. Desktop (`desktop/`) picks a folder only via File > Open Project Folder and restarts its server with `--allow-root` for folders outside home.
- `main()` catches `ConfigError`, `AgentFileError`, `PluginArgumentError` and missing extras; anything else prints a traceback.
- uv tool installs record their requirement (extras, pin) in `<tool dir>/uv-receipt.toml`; `--reinstall` keeps only the extras it names.

## Decisions

- `noeta serve` stays the deployment command (foreground, any bind, operator flags). `noeta web` is the local convenience over the same server: loopback only, background, its own token, opens the browser; a second `noeta web` reuses the running server.
- `noeta web` starts `noeta serve --host 127.0.0.1 --port <8642, else a free port> --allow-root <home>` with the bundled web app. Its state (pid, port, token, log path) is one file under `~/.noeta/` with mode 0600; the token is passed via `NOETA_SERVER_TOKEN` in the child's environment, never argv. Stale state (dead pid) is cleaned up silently.
- The wheel carries the built web app inside the `noeta` package; `noeta serve` serves it when `--web` is not given and the files are present. A wheel built without node still works (API only) and `noeta web` then says the web files are missing.
- Projects are server-side: `GET /projects` → the saved list plus the distinct directories of existing (unarchived) sessions, each `{path, name, saved, sessions}`; `POST /projects {path}` saves one (must pass the same check as a new task's `cwd`; 403 otherwise); `DELETE /projects {path}` unsaves it (sessions stay). Saved list persisted next to the store (`~/.noeta/projects.json` by default). `GET /dirs?path=` lists subdirectories of an allowed directory, for the web app's folder picker.
- Desktop keeps its native folder picker and `--allow-root` restart, then registers the folder via `POST /projects`.
- `noeta update`: uv tool install → `uv tool install --reinstall` with the receipt's extras and `--with`, the version pin dropped; pip install → `pip install -U 'noeta[<installed extras>]'`; a source/editable install → print the commands, change nothing. Says "already up to date" when PyPI's latest equals the installed version. A running `noeta web` server is stopped first and restarted after.
- Unexpected exceptions in `main()`: one line `noeta: unexpected error: <type>: <message>` plus the path of a crash report (traceback, version, argv) under `~/.noeta/logs/`; `-v` / `NOETA_DEBUG=1` prints the traceback too. `KeyboardInterrupt` exits 130 quietly.
- Desktop installers (macOS dmg, Linux AppImage + deb), unsigned, built in the release workflow and attached to the GitHub Release.

## Plan

- [x] Python: crash fallback in `main()` + crash report
- [x] Python: `/projects` and `/dirs` endpoints (+ OpenAPI, `RemoteClient`/`AppAPI` only if the TUI needs them — it does not)
- [x] Python: bundled web app in the wheel; `make build` and release `build` job build `web/` first; smoke step asserts the wheel has `index.html`
- [ ] Python: `noeta web`, `noeta web stop`, `noeta web status`
- [ ] Python: `noeta update`
- [ ] Web: workbench layout — projects column with sessions, add project (folder picker over `/dirs`, desktop native picker), new session in the selected project
- [ ] Desktop: register chosen folders via `/projects`; open on the last project
- [ ] Release: desktop installers job attached to the GitHub Release
- [ ] Docs (en + zh): front-ends, quickstart install/upgrade, CLI reference, troubleshooting; CHANGELOG Unreleased

## Acceptance criteria

- Fresh venv + built wheel: `noeta web --no-open` prints a URL that serves the app and the API answers with the printed token; `noeta web status` shows it; `noeta web stop` stops it; a second `noeta web` reuses the first server.
- `noeta update` in a uv tool install with `[tui,server]` reinstalls with both extras (verified by a test against a fake receipt and a recorded command line).
- A forced unexpected exception prints no traceback without `-v`, and the crash report file exists.
- The web app shows projects with their sessions, can add a project and start a session in it (vitest + Playwright).
- `make check` green; `make frontends` green.

## Risks

- Wheel size grows by the web build (~0.7 MB).
- Release now needs node in the build job; a node failure blocks the Python release.
- `--allow-root <home>` for `noeta web` lets the local web app start tasks anywhere under home — same as desktop today.

## Progress log

- 2026-11-09 owner: Codex-style local workbench; do all five proposals.
- 2026-11-09 crash fallback: `-v` is already `--version` (Claude Code's flag), so the traceback switch is `--verbose` (anywhere on the command line) or `NOETA_DEBUG=1`. The TUI hands an error that ended it back to `main()` (Textual's own traceback, with every frame's locals, is no longer printed); `-p` goes through `main()` already.
- 2026-11-09 `/projects` and `/dirs` (`noeta/host/projects.py`, `_http_routes.py`), as decided, plus these details the web app can rely on: `path` is the directory's real path (symlinks resolved; sessions are counted by their cwd's real path); the list is the saved projects in the order saved, then the other session directories, most recently used first; the count covers the 500 most recent root tasks; `POST /projects` answers 200 with the project (400 when the path is not a directory), `DELETE /projects` answers `{ok: true}`; `GET /dirs` without `path` lists the server's own directory, a relative `path` is taken from it, and `parent` is `null` when the parent is outside the allowed directories. The saved list is `projects.json` beside a sqlite store's file, else in noeta's home.
- 2026-11-09 bundled web app: `make web-bundle` builds `web/` and copies `web/dist` to `noeta/host/web_dist/` (ignored by git, a hatch `artifacts` entry so both the sdist and the wheel built from it carry it; wheel 1.36 MB). `make build` and the release `build` job run it first; the job fails when the wheel lacks `noeta/host/web_dist/index.html`. `noeta serve` without `--web` serves it when present (`noeta.host.http.bundled_web_dir`).
