# Accepted deviations from Claude Code

A model-visible difference from the baseline is allowed only when noeta's kernel or product
forces it (`docs/specs/2026-10-28-claude-code-parity.md`, Decisions). Each row names the reason;
a backticked id is the `make parity` mismatch it accepts (the comparator does not count it).
A row without an id is a difference the replay cannot see.

## Kernel

| id | difference | reason |
|---|---|---|
| | Tools run after the assistant message is logged, not while it streams. | Log-first crash safety. |
| | A tool started before a crash answers "result unknown" instead of re-running. | A call may have had effects; running it twice is worse than asking. |
| | Waits (approval, sub-agent, timer) survive restarts and move between machines. | Durable tasks. |
| | `ScheduleWakeup` wake-ups and `CronCreate` jobs are kernel timers in the task's log: they survive restarts and fire on any worker, so `CronCreate.durable` changes nothing and its description says so (Claude Code keeps session jobs in memory and writes durable ones to `.claude/scheduled_tasks.json`). | Durable tasks. |
| | A `Workflow` run is replayed from its recorded agent results (the script is re-executed, finished `agent()` calls answer from the log) instead of running once in memory; it survives restarts like any wait. | Durable tasks. |

## Permissions

| id | difference | reason |
|---|---|---|
| | A `Write` / `Edit` outside the working and additional directories (and the plan directory) is refused, not asked about. | The file tools fence writes below the permission layer, so a host without the permissions plugin stays fenced; a directory is opened with `additionalDirectories`. |
| | "Always" answers in a trusted project are kept in `.noeta/settings.local.json` (Claude Code's format), and `.noeta/` is protected the way Claude Code protects `.claude/`. | noeta reads and writes only its own places (owner, 2026-10-28). |
| | Plan files live in `<noeta home>/plans/`. | noeta's own config directory. |

## Tools

| id | difference | reason |
|---|---|---|
| | `Workflow` scripts run in an embedded V8 (`mini-racer`), no system Node: a fresh context per replay with a time and memory limit; where mini-racer cannot load, the tool refuses and starts nothing. | Owner-confirmed (2026-10-28): the contract (JavaScript, the script API) is Claude Code's; Claude Code runs the script in its own JavaScript runtime, noeta in a capability-free engine inside the worker. |
| | `Workflow`'s `name` loads a saved workflow from the project's `.noeta/workflows/`, then `~/.noeta/workflows/`, never `.claude/workflows/`, and its parameter description says so. | noeta reads only its own config places (owner, 2026-10-28). |
| | `Workflow`'s description carries a short script-API reference (`tool.Workflow.8`): there is no bundled `workflow-authoring` skill to load. | noeta ships no bundled skills yet; the reference returns to a skill when it does. |

## Request envelope

| id | difference | reason |
|---|---|---|
| `41a7944c` `fd6eb0f6` | No billing header block before the identity line (Claude Code's `x-anthropic-billing-header`, its version and entry point). | It tells Anthropic's billing which Claude Code build sent the call; noeta is not Claude Code and an API key needs no such line. |
| `5a78a8ed` `0a9c6f15` | The two system cache marks sit on `system[0]` and `system[1]` instead of `system[1]` and `system[2]`. | Same blocks (identity, prompt); the index shifts only because the billing header is absent. |
| `7678a2ee` | A sub-agent's request has no billing header block either. | As for the main request. |
| `098ae292` | A sub-agent's two system cache marks sit on `system[0]` and `system[1]`. | As for the main request: the billing header is absent. |

## Main prompt and environment message

| id | difference | reason |
|---|---|---|
| | The `!`-prefix rule (ask the user to run an interactive command as `! <command>`) is left out of the main prompt (`main.session.1`). | No noeta front end runs `!` commands yet; the rule returns with that front-end feature. |
| | The environment section names noeta's own front ends instead of Claude Code's, and leaves out fast mode (`main.envp.2`, `main.envp.3`). | Product facts of Claude Code; noeta has no fast mode. |
| | The attribution note's PR footer links to noeta, and is left out for a model the catalog gives no display name. | The footer names the tool that wrote the PR; the trailer needs the model's name. |

## noeta's own files

noeta reads only its own places (owner, 2026-10-28): Claude Code's mechanisms and formats, noeta's
file names. Each row is a name the model sees; the files themselves are product facts.

| id | difference | reason |
|---|---|---|
| | The instructions reminder lists `NOETA.md`, `.noeta/NOETA.md`, `.noeta/rules/`, `NOETA.local.md`, the user's `~/.noeta/NOETA.md` and `~/.noeta/rules/`, and the managed `NOETA.md` (`/etc/noeta`, macOS `/Library/Application Support/Noeta`), in Claude Code's order, where Claude Code lists `CLAUDE.md` and `.claude/` paths; a directory with neither project file shows its `AGENTS.md` (`rem.instructions.5`). | noeta never picks up Claude Code's instructions; `AGENTS.md` is the cross-tool convention. |
| | Texts that name the instruction file say `NOETA.md` where Claude Code says `CLAUDE.md`: the memory section (`main.mem.15`), the sub-agent prompts and caller messages (`sub.common.1`), the hand-back frame (`sub.result.4`), `Agent`'s `effort` description, and the auto-mode classifier's instructions block, which is a `<user_noeta_md>` element (`cls.input.1`). | The file the text means is noeta's. |
| | `Agent`'s description names agent files in `.noeta/agents/` (`tool.Agent.20`). | noeta's agent directory. |
| | `Edit` / `Write` leave the "file state is current" note off for `NOETA.md` (`tool.Edit.r.2`). | The rule is about the instruction file, which is `NOETA.md` here. |
| | An isolated agent's worktree is under `.noeta/worktrees/`, not `.claude/worktrees/`, and the auto-mode policy names `.noeta/` trees and `NOETA.md` as the agent's configuration. | noeta's own directory. |
| | An MCP result cut to its budget names `NOETA_MAX_MCP_OUTPUT_TOKENS`. | noeta's environment variables carry its prefix (`NOETA_MCP_TIMEOUT`, `NOETA_MCP_TOOL_TIMEOUT`, `NOETA_AUTO_COMPACT_WINDOW`, `NOETA_AUTOCOMPACT_PCT_OVERRIDE` likewise). |

## Instruction files

| id | difference | reason |
|---|---|---|
| | An `@path` import from a repository file that leaves the repository directory and the working directory is skipped, not asked about (`rem.instructions.7`). | No noeta front end has Claude Code's approval dialog for external imports; skipping is the safe side of it. |
