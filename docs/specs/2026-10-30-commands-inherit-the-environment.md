---
status: active
created: 2026-10-30
branch: bench-env
---

# Commands inherit the environment, as in Claude Code

## Goal

A command noeta starts on this machine (`Bash`, a background job, a command hook, a stdio MCP
server, an MCP `headersHelper`) inherits noeta's own environment, as Claude Code's do, minus
noeta's own credentials. Second: a `-p` run whose result takes a while to put together still
reports it and exits 0.

## Non-goals

- Claude Code's added variables (`CLAUDECODE=1`, `CLAUDE_CODE_SESSION_ID`, `GIT_EDITOR=true`,
  `CLAUDE_PID`, `AI_AGENT`): a separate question (open, see Progress log).
- Claude Code's opt-in `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB` (on by default only in GitHub Actions):
  noeta's sandbox `credentials.envVars` already unsets named variables per command.
- `ContainerExecEnv`: a command there runs with the image's environment (`docker exec`), never
  the host's; unchanged.

## Context

SWE-rebench run (DeepSeek V4.1, noeta @54121930 vs Claude Code 2.1.312): `scrub_env` passed only
an allowlist (`PATH`, `HOME`, `LANG`, `LC_ALL`, `TERM`, `TMPDIR`, `PYTHONHASHSEED`, `PYTHONPATH`,
`VIRTUAL_ENV`), so image variables (`CARGO_HOME`, `RUSTUP_HOME`, `JAVA_HOME`, `GOPATH`, …) never
reached commands. gleam-5797: `cargo test` failed with "rustup could not choose a version of
cargo"; in 22/111 tasks the model set such variables by hand, 7 hit "toolchain not found";
Claude Code 0 and 0. The allowlist dates from the initial commit with the stated reason "so a
child never picks up the host's credentials by accident"; no ADR or spec records it.

What the others do (read from their code, 2026-10-30):

- Claude Code 2.1.312 (bundle): `Bash`'s environment is
  `{...subprocessEnv(), SHELL, GIT_EDITOR: "true", ...sandboxEnv, CLAUDECODE: "1",
  CLAUDE_CODE_SESSION_ID, CLAUDE_CODE_CHILD_SESSION: "1", CLAUDE_PID, AI_AGENT, CLAUDE_EFFORT}`;
  `subprocessEnv()` returns `process.env` unchanged unless one of Claude Code's own internal
  variables is set, and then deletes only those: its OAuth / session / bridge tokens
  (`CLAUDE_CODE_OAUTH_TOKEN`, `CLAUDE_CODE_SESSIONS_OAUTH_TOKEN`, `*_FILE_DESCRIPTOR`,
  `CLAUDE_CODE_SESSION_ACCESS_TOKEN`, …), internal `CLAUDE_CODE_*` / `CLAUDE_BG_*` switches,
  and `OTEL_*`. `ANTHROPIC_API_KEY` is passed (withheld only on the HIPAA tier or with
  `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB`). Hooks and stdio MCP servers get the same base
  (`{...subprocessEnv(), ...markers, CLAUDE_PROJECT_DIR, ...server.env}`). Seen live: a `Bash`
  command under Claude Code shows the session's `SSH_*`, `NVM_*`, proxy and `XDG_*` variables.
- pi-mono: `getShellEnv()` = `{...process.env, PATH: <its bin dir> + PATH}`.
- DeepSeek Harness: `scrubbedParentEnv()` = `process.env` minus names matching
  `/KEY|PASSWORD|SECRET|TOKEN/i` and minus `DSH_*`.

## Decisions

- **Base environment.** Others: Claude Code and pi pass the whole process environment; DeepSeek
  Harness passes it minus credential-shaped names. noeta: the whole environment minus noeta's
  own credentials (`toolkit/local_env.OWN_SECRETS`). Why: toolchains and project settings live
  in the environment (the bench failures); a name-pattern scrub would also drop `GITHUB_TOKEN`,
  `NPM_TOKEN` and the like, which the person's own commands rely on and Claude Code passes.
- **What noeta withholds.** Others: Claude Code withholds only its own credential channels
  (OAuth/session tokens), not `ANTHROPIC_API_KEY`. noeta: `NOETA_API_KEY`, `NOETA_STORE`,
  `NOETA_SERVER_TOKEN`, `NOETA_MCP_CLIENT_SECRET`, `NOETA_WEB_SEARCH_API_KEY` — the variables
  only noeta reads. Provider variables (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`) pass, as in Claude
  Code. Why: `NOETA_STORE` on a worker opens every task of the shared store to a command (a
  multi-machine risk Claude Code does not have); the rest are noeta's credentials, which a
  project never needs. A host that wants a provider key out of commands sets it through
  `NOETA_API_KEY` / the config file, or lists it in sandbox `credentials.envVars`.
- **One function for every local spawn.** `child_env()` replaces `scrub_env()`; `LocalExecEnv`,
  command hooks, stdio MCP servers and `headersHelper` all use it. `LocalExecEnv(inherit_env=)`
  is removed (nothing left to widen).
- **`-p` result race.** `StreamJson._finish` cleared the open turn before awaiting the result's
  usage; `settled()` polls every 0.5 s, saw no open work and returned with no result: no
  `result` line, exit 1 (pydantic-ai-5842: the price warning printed by the usage computation is
  the last line). Fix: the turn counts as open until its result is delivered.

## Plan

- [x] `child_env()` / `OWN_SECRETS` in `toolkit/local_env.py`; `LocalExecEnv`, hooks, MCP stdio
      and `headersHelper` use it; `inherit_env` removed; docstrings say so.
- [x] Tests: a command, a hook and a stdio MCP server see an unlisted variable and not
      `NOETA_STORE`.
- [x] `StreamJson` keeps the turn open until its result is delivered; test with slow usage.
- [x] `parity/deviations.md` row; CHANGELOG.
- [ ] Gates: `make check`, `make parity`, `make cluster`.

## Acceptance criteria

- `env` in a `Bash` command shows the parent's variables except `OWN_SECRETS`.
- Hooks and stdio MCP servers get the same base.
- `noeta -p` reports the result even when its usage takes longer than the settle poll.
- `make check` green, `make parity` 0 unexplained, `make cluster` all pass.

## Risks

- A host that relied on the allowlist to keep other secrets (cloud keys in the worker's
  environment) out of commands now passes them, as Claude Code does. Mitigation: sandbox
  `credentials.envVars`, or start the worker with a clean environment; CHANGELOG says so.

## Progress log
2026-10-30 Claude Code 2.1.312 read from its bundle (`~/.local/share/claude/versions/2.1.312` strings): Bash/hooks/MCP env builders quoted in Context; pi-mono `utils/shell.ts`, DeepSeek Harness `subprocess/src/index.ts`.
2026-10-30 Why the allowlist existed: initial commit (0e8165bb), docstring only ("so a child never picks up the host's credentials by accident"); no ADR or spec.
2026-10-30 pydantic-ai-5842 (gpu-cn `jobs/rb-ds41/pydantic__pydantic-ai-5842__guPatib`): the stream ends with the final reply and the price warning, no `result`; harbor's `AgentSafetyRefusalError` is its regex matching the task's own text (`ContentFilterError` is a pydantic-ai class), not anything noeta printed. Race reproduced by a test with a 1.2 s usage read (fails before the fix).
2026-10-30 Container exec env unchanged in behaviour: its `docker` client now inherits the host environment (it is a command noeta starts), commands in the container still get only the image's plus explicit `-e`; `CLIENT_ENV` keeps the old names.
2026-10-30 Gates: `make check` green (2596 passed, 89 %), `make parity` 0 mismatches.
Open: whether noeta should set markers like Claude Code's (`GIT_EDITOR=true` keeps `git commit`/`rebase` from opening an editor; a `NOETA_SESSION_ID`-style marker) — owner's call.
