---
status: done
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

- Claude Code's session markers (`CLAUDECODE=1`, `CLAUDE_CODE_SESSION_ID`, `CLAUDE_PID`,
  `AI_AGENT`, `CLAUDE_EFFORT`): no need for a noeta equivalent (owner, 2026-10-30).
- Claude Code's opt-in `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB` (on by default only in GitHub Actions):
  noeta's sandbox `credentials.envVars` already unsets named variables for sandboxed commands.
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
  `NOETA_API_KEY` / the config file. `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` pass even when
  noeta falls back to one as its own key, as Claude Code passes `ANTHROPIC_API_KEY`.
- **Secrets noeta's config hands to its plugins and channels** (review, 2026-10-30). Others:
  Claude Code has no such config; its plugins' own hook and MCP `{env:}` references pass.
  noeta: every variable an `{env:NAME}` names in `config.toml`'s `[plugins.*]`,
  `[channels.*]` and `[agents.*]` tables (read in `load_settings`) and in noeta's own agent
  files (`~/.noeta/agents`, a trusted project's `.noeta/agents`, `--agents`; read when the
  agents are made) is withheld too (`local_env.withhold_env_refs`); a Claude Code plugin's
  bundled hook / MCP references still pass, as in Claude Code. Why: these are noeta's own
  credentials — a chat channel's shared secret (`CHAT_HOOK_TOKEN`) in a command's environment
  lets an injected command forge the chat app's requests, approvals among them.
- **libpq's credentials when the store is Postgres** (review, 2026-10-30). Others: no shared
  store. noeta: `PGPASSWORD`, `PGPASSFILE`, `PGSERVICE`, `PGSERVICEFILE` withheld once a
  `PostgresStore` is made in the process (`PGHOST`, `PGUSER`… pass); without a Postgres store
  they pass, so the person's own `psql` keeps working. Why: libpq logs in to noeta's store with
  them, the same exposure as `NOETA_STORE`.
- **No name-pattern filter on servers.** Others: Claude Code passes every other secret in its
  environment (cloud keys, `GITHUB_TOKEN`, `ANTHROPIC_API_KEY`) and offers an opt-in scrub;
  pi passes everything; DeepSeek Harness drops names matching `KEY|PASSWORD|SECRET|TOKEN`.
  noeta: Claude Code's behaviour on a worker too — only `OWN_SECRETS` are withheld. Why: a
  pattern drops variables the person's commands need (`GITHUB_TOKEN`, `NPM_TOKEN`,
  `HF_TOKEN`) and keeps secrets with other names, so it is neither safe nor compatible; an
  operator keeps a secret out with a clean worker environment, the reliable way (sandbox
  `credentials.envVars` covers sandboxed `Bash` commands only) (owner, 2026-10-30).
- **`GIT_EDITOR=true` on every command.** Others: Claude Code sets it on `Bash` (and its
  background and `Monitor` commands) over the inherited environment, so a user-set
  `GIT_EDITOR` is replaced; only its own per-command overrides (session variables hooks
  export) win; hooks and MCP servers do not get it. noeta: `shell.bash.COMMAND_ENV` laid under
  the host's `ShellEnv` variables on `Bash`, background jobs and `Monitor`; on a container exec
  env it travels as `docker exec -e` like any call variable (Claude Code's sandboxed commands
  get it the same way). Why: a command has no terminal, so `git commit` / `rebase -i` must not
  wait for an editor (owner, 2026-10-30).
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
- [x] `GIT_EDITOR=true` on shell commands (owner's call); test.
- [x] Gates: `make check`, `make parity`, `make cluster`.

## Acceptance criteria

- `env` in a `Bash` command shows the parent's variables except `OWN_SECRETS`.
- Hooks and stdio MCP servers get the same base.
- `noeta -p` reports the result even when its usage takes longer than the settle poll.
- `make check` green, `make parity` 0 unexplained, `make cluster` all pass.

## Risks

- A host that relied on the allowlist to keep other secrets (cloud keys in the worker's
  environment) out of commands now passes them, as Claude Code does. Mitigation: start the
  worker with a clean environment (sandbox `credentials.envVars` covers sandboxed `Bash` only);
  CHANGELOG says so.

## Progress log
2026-10-30 Claude Code 2.1.312 read from its bundle (`~/.local/share/claude/versions/2.1.312` strings): Bash/hooks/MCP env builders quoted in Context; pi-mono `utils/shell.ts`, DeepSeek Harness `subprocess/src/index.ts`.
2026-10-30 Why the allowlist existed: initial commit (0e8165bb), docstring only ("so a child never picks up the host's credentials by accident"); no ADR or spec.
2026-10-30 pydantic-ai-5842 (trial `pydantic__pydantic-ai-5842__guPatib` of the first SWE-rebench run): the stream ends with the final reply and the price warning, no `result`; harbor's `AgentSafetyRefusalError` is its regex matching the task's own text (`ContentFilterError` is a pydantic-ai class), not anything noeta printed. Race reproduced by a test with a 1.2 s usage read (fails before the fix).
2026-10-30 Container exec env unchanged in behaviour: its `docker` client now inherits the host environment (it is a command noeta starts), commands in the container still get only the image's plus explicit `-e`; `CLIENT_ENV` keeps the old names.
2026-10-30 Gates: `make check` green (2596 passed, 89 %), `make parity` 0 mismatches, `make cluster` 14/14.
2026-10-30 After `GIT_EDITOR`: `make check` green (2596 passed, 89 %), `make parity` 0 mismatches, `make cluster` 14/14; archived.
2026-10-30 Owner: add `GIT_EDITOR=true` as Claude Code does, no session markers, no name-pattern filter. Claude Code's precedence read from its `Bash` env builder: `{...env, SHELL, GIT_EDITOR: "true", ...envOverrides, ...sandboxEnv, ...markers}`, `envOverrides` being the shell's session variables.
2026-10-30 Review fixes: `{env:}` secrets of noeta's config and own agent files withheld; libpq credentials withheld with a Postgres store; a prompt sent while a result is being put together queues (test: its replayed `user` line comes after the result); tests for `headersHelper`, the config/agent-file secrets and libpq; stale texts (`parity/rules/tools/Bash.md` `tool.Bash.6`, `mcp/stdio.py`, the deviations row) and the mitigation text fixed.
2026-10-31: `make eval` at cc11f6dc (DeepSeek V4.1, 9 trials): 0.946 vs baseline 0.953, no regression (dips only in the known noisy scenarios: delegate_parallel_review timeouts, compaction_resume_edits, concise_direct_answer one trial).
