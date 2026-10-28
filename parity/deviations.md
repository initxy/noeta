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

## Request envelope

| id | difference | reason |
|---|---|---|
| `41a7944c` `fd6eb0f6` | No billing header block before the identity line (Claude Code's `x-anthropic-billing-header`, its version and entry point). | It tells Anthropic's billing which Claude Code build sent the call; noeta is not Claude Code and an API key needs no such line. |
| `5a78a8ed` `0a9c6f15` | The two system cache marks sit on `system[0]` and `system[1]` instead of `system[1]` and `system[2]`. | Same blocks (identity, prompt); the index shifts only because the billing header is absent. |
| `ebcc79cb` | No `safeguards` (`dangerous_tool_use` with its classifier context). | It belongs to auto mode, which noeta does not have yet (Phase 3); its context (permission rules, trusted directories) describes Claude Code's permission system. |
| `a0b35679` `694d1d4d` `15d53766` | No `afk-mode-2026-01-31` / `dangerous-tool-use-2026-09-03` betas. | Claude Code adds them only in auto mode (the default-mode session `m2` sends neither); noeta sends the default-mode set until auto mode lands. |

## Main prompt and environment message

| id | difference | reason |
|---|---|---|
| `6bf9cea5` | No auto-mode guidance (shell for reading, searching and small edits) in the environment message. | It describes auto mode; it comes with auto mode (Phase 3). |
| | The `!`-prefix rule (ask the user to run an interactive command as `! <command>`) is left out of the main prompt (`main.session.1`). | No noeta front end runs `!` commands yet; the rule returns with that front-end feature. |
| | The environment section names noeta's own front ends instead of Claude Code's, and leaves out fast mode (`main.envp.2`, `main.envp.3`). | Product facts of Claude Code; noeta has no fast mode. |
| | The attribution note's PR footer links to noeta, and is left out for a model the catalog gives no display name. | The footer names the tool that wrote the PR; the trailer needs the model's name. |
