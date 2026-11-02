# Bash

Claude Code version: 2.1.312 (baseline session `m1`).

| source | sha256 (normalized) |
|---|---|
| `m1/0001 tools[Bash].description` | `f3d0add9a1ce27f9ff798fd5b4357d2f80267af85286cf39482d787615138cd9` |
| `m1/0001 tools[Bash].input_schema` | `a2c80950bb81d3b5d03c96a677c24171b3b5a3457f8efc2745cceb93e096fad1` |
| `m2/0002 tools[Bash].description` (default-mode variant) | `f7d4107a7b0344eb8553374e4aad65fd0090959ad4f4ac7f23691d9da54ce3e1` |

## Purpose and placement

Runs one shell command and returns what it printed. Always loaded (no `defer_loading`); third in
the baseline tool list (`Agent`, `AskUserQuestion`, `Bash`, ...). The list is alphabetical by
name except that `DeferredToolPlaceholder` sits before `Write` until a deferred tool is loaded.
The same description and schema are sent to sub-agents (`m1/0029`).

noeta equivalent: `noeta/plugins/shell/bash.py` (`BashTool`, `BackgroundBash` in
`noeta/plugins/shell/tools.py`), description `noeta/plugins/shell/descriptions/shell_run.md`.

## Parameters

Schema is JSON Schema 2020-12 (`$schema` set), `additionalProperties: false`, and every
property carries its own `description`.

| id | name | type | required | enum / default / limits | noeta today |
|---|---|---|---|---|---|
| `tool.Bash.p.command` | `command` | string | yes | the command line to run | covered |
| `tool.Bash.p.timeout` | `timeout` | number | no | milliseconds; property text names the 600000 ceiling for a foreground command | covered |
| `tool.Bash.p.description` | `description` | string | no | see `tool.Bash.12`-`tool.Bash.15` for what it must say | covered |
| `tool.Bash.p.run_in_background` | `run_in_background` | boolean | no | true detaches the command | covered |
| `tool.Bash.p.dangerouslyDisableSandbox` | `dangerouslyDisableSandbox` | boolean | no | true asks to run outside the sandbox | covered: with the sandbox on, the command leaves it after an approval (`tool.Bash.s.6`) |
| `tool.Bash.p.schema` | (schema shape) | - | - | `$schema` 2020-12, `additionalProperties: false`, a `description` on each property | covered |

## Rules

| id | rule | noeta today |
|---|---|---|
| `tool.Bash.1` | Purpose line: runs a bash command and returns its output. | covered |
| `tool.Bash.2` | A `cd` persists into the next call only in the main conversation (not in sub-agents), only when the command succeeds, and only when the final directory is inside the working directories. | covered |
| `tool.Bash.3` | When a command ends outside the working directories, the next call starts back in the starting folder; otherwise the directory carries over. | covered (resets to workspace root, adds a reset note) |
| `tool.Bash.4` | Absolute paths are preferred; a `cd` inside a compound command may trigger a permission prompt. | covered |
| `tool.Bash.5` | Environment variables and shell functions do not persist between calls. | covered |
| `tool.Bash.6` | The shell is initialized from the user's login profile. | deviation — no rc snapshot or login profile; `bash -c` with noeta's environment minus its credentials (`deviations.md`) |
| `tool.Bash.7` | Command output goes to the model; the user may not see it, so anything the user needs must be restated. | covered |
| `tool.Bash.8` | `timeout` is in milliseconds, default 120000, maximum 600000 for a foreground command. | covered |
| `tool.Bash.9` | `run_in_background` detaches the command; it keeps running across turns and wakes the model when it exits. No trailing `&` is needed. | covered |
| `tool.Bash.10` | While a background command runs, the model is notified on exit and must not poll its output file. | covered |
| `tool.Bash.11` | A foreground `sleep` is blocked; waiting on a condition is done with `Monitor` and an until-loop. | covered |
| `tool.Bash.12` | `description` says what the command does in the active voice, in plain words. | covered |
| `tool.Bash.13` | `description` avoids the words "complex" and "risk". | covered |
| `tool.Bash.14` | `description` does not repeat the command text, its flags or file paths, because the user reads it without the command. | covered |
| `tool.Bash.15` | `description` is 5-10 words for simple commands; for piped or obscure commands it adds enough context to be understood (the property text gives three short and three long examples). | covered |
| `tool.Bash.16` | Git: interactive flags (`-i`, e.g. interactive rebase or add) are not supported. | covered |
| `tool.Bash.17` | Git: use the `gh` CLI for GitHub work (PRs, issues, API). | covered |
| `tool.Bash.18` | Git: an `@name` in a PR, issue or comment notifies that GitHub user; a bare `@name` is used only when that real account should be pinged; anything else (roles, placeholders, code, text taken from project files) is backticked or loses the `@`. | covered |
| `tool.Bash.19` | Git: commit or push only when the user asks; on the default branch, create a branch first. | covered |
| `tool.Bash.20` | Git: when the attribution `<system-reminder>` is present, its lines close every commit message and PR description. | covered in the description (the attribution reminder itself: see `reminders.md`) |
| `tool.Bash.21` | Default-mode variant only (`m2`): avoid `cat`, `head`, `tail`, `sed`, `awk`, `echo` unless told to or after confirming no dedicated tool fits; use the dedicated tool instead. In `auto` mode (baseline) this line is absent and the environment message instead says Bash is fine for reading, searching and small mechanical edits. | deviation (`parity/deviations.md`, Tools) — the line is in the description unless the task starts in `auto` or `bypassPermissions` mode (Claude Code decides it once per session: auto active or bypass at launch); noeta decides it at the task's first request and records it (`shell` / `steer`), so a later mode switch leaves the description alone |
| `tool.Bash.22` | The description states no output-size cap. (Observed: a 12005-character output came back whole.) | covered (no cap stated; output past 30000 characters is still spilled to a file) |
| `tool.Bash.23` | (bundle) A command matching the in-place write markers (`--write`, `--fix`, `sed -i`, formatters such as `black`, `ruff format`, `cargo fmt`) ends its result with the files the model had read that it changed (five, then a count) and a request to `Read` them before editing; the changed-file note still follows. | covered (`fs/shell_writes.py`; the timing differs: `deviations.md`) |

## Result shape

Observed in `m1`, `m2`, `m3`, `s1`.

| id | case | shape | noeta today |
|---|---|---|---|
| `tool.Bash.r.1` | exit 0 with output | the output text only, trailing newline trimmed; no exit-code line; `is_error` false | covered |
| `tool.Bash.r.2` | non-zero exit | first line `Exit code <n>`, then the output; `is_error` true | covered |
| `tool.Bash.r.3` | stdout and stderr | both streams merged into one text in the order printed, no stream labels (`(eval):37: command not found: python` after `Exit code 127`) | covered |
| `tool.Bash.r.4` | exit 0, no output | the fixed text `(Bash completed with no output)` | covered |
| `tool.Bash.r.5` | background start | one paragraph: running in background with an ID (9 lower-case alphanumerics); the absolute path of the output file (under the per-project temp dir, `.../<session>/tasks/<id>.output`); the model will be notified on completion and must not poll or sleep; read the file only if the user asks for progress or a line (such as a server's ready line or port) is needed before going on | covered |
| `tool.Bash.r.6` | background output file | when the job ends, the file holds the output followed by a blank line and `[exited with code <n>]` (seen via `cat` in `s1`) | covered |
| `tool.Bash.r.7` | background completion | arrives later as a background-task notification, not as a tool result (see `reminders.md`) | see `reminders.md` |
| `tool.Bash.r.9` | exit 1 of a search or comparison | when the last command of the line is `grep`, `rg`, `egrep`, `fgrep`, `find`, `diff`, `test`, `[` or `git diff` / `git grep`, exit status 1 (no match, a difference, a false condition) is not a failure: no `Exit code` line, `is_error` false, `(Bash completed with no output)` when it printed nothing; status 2 and up still fails (2.1.312 bundle) | covered |
| `tool.Bash.r.10` | foreground timeout | a command still running at its timeout is not killed but moved to the background (unless its first command is `sleep`, or background tasks are off): the result is not an error; it carries the last 2,000 bytes printed so far, then the job ID, the output file, that the model is notified at the end, not to start it again, `TaskStop` to stop it, reading the file for interim output, and a note when nothing was printed yet; the end comes as the usual notification. A command that cannot be moved is killed with a timed-out error (2.1.312 bundle) | covered (a host without background jobs kills it) |
| `tool.Bash.r.11` | image output | a successful command whose whole output is one `data:image/<type>;base64,...` URI comes back as an image block (typed by its bytes, resized to the image limits); an undecodable one stays text (2.1.312 bundle) | covered |
| `tool.Bash.r.12` | background exit 1 of a search or comparison | a background command whose last command is one of `tool.Bash.r.9`'s and exits 1 ends `completed`, its summary giving the code and what it means there (no matches, files differ, a false condition, directories that could not be read); not when that last command follows `&&` or the line cannot be parsed (status 1 may then be an earlier command's), and never for a monitor (2.1.312 bundle) | covered — `background_exit_note` (`shell/output.py`) |
| `tool.Bash.r.13` | background command stuck at a prompt | a background command (not a monitor) whose output has not grown for 45 s (checked every 5 s) and whose last line looks like a question (`(y/n)`, `[y/n]`, `(yes/no)`, `Press Enter` / any key, `Continue?`, `Overwrite?`, a sentence opening "Do you" / "Would you" / "Shall I" / "Are you sure" / "Ready to" and ending in `?`) brings one notification without a status: the command appears to wait for interactive input, then, after the element, its last output and advice to stop it and re-run it with the answer piped in or a non-interactive flag (2.1.312 bundle) | covered — `Registry._stall`, read through the job's exec env |
| `tool.Bash.r.8` | attached context | a result may carry `<system-reminder>` blocks appended after the output (after compaction, the `CLAUDE.md` and git-status reminders rode on the first tool result, `m1/0059`) | see `compaction.md` |

## Variants

- `m2` (default permission mode) adds `tool.Bash.21`'s "avoid `cat`/`head`/..." line; schema is
  identical. `s1` (OAuth) is identical to `m1`.

## Sandbox variant

Not recorded (the baseline runs with `sandbox.enabled` unset); taken from the 2.1.312 bundle.
The sandbox is not described in `Bash`'s description: with `sandbox.enabled` and a sandbox that
can start, a `sandbox_instructions` harness note (a meta message, ahead of the other
announcements) carries the rules below. It is sent again whenever its text differs from the
last one in the conversation (so again after a compaction drops it), and once the sandbox is
off after one was sent, a note says commands now run unconfined and the earlier instructions
no longer apply (noeta: `Shell.reminders`, text `noeta/plugins/shell/descriptions/shell_sandbox*.md`,
`SANDBOX_OFF`).

| id | rule | noeta today |
|---|---|---|
| `tool.Bash.s.1` | The instructions state what the sandbox allows: where commands may write, which hosts they may reach, and that denied connections are reported in a `<sandbox_violations>` block. | covered |
| `tool.Bash.s.2` | Default to running inside the sandbox; set `dangerouslyDisableSandbox: true` only when the user explicitly asks, or a command just failed with evidence the sandbox caused it (commands fail for many other reasons). | covered |
| `tool.Bash.s.3` | Evidence: "Operation not permitted" on file or network operations, access denied outside allowed directories, connection failures to hosts not allowed, Unix socket errors. | covered |
| `tool.Bash.s.4` | On that evidence, retry at once with `dangerouslyDisableSandbox: true` without asking (it goes through the permission gate), and briefly explain the likely restriction and how the user can change the sandbox settings. | covered (points at the settings' `sandbox` section, see `parity/deviations.md`) |
| `tool.Bash.s.5` | A denial on a credential, file or host the task does not involve: tell the user rather than retrying outside. Each unsandboxed run is decided on its own. Never suggest adding sensitive paths to the allowlist. | covered |
| `tool.Bash.s.6` | Leaving the sandbox goes through the permission gate even when an allow rule matches (a prompt, or the classifier in auto mode). | covered (`noeta/plugins/permissions`) |
| `tool.Bash.s.7` | With `allowUnsandboxedCommands: false` the parameter is said to be disabled, and a sandbox failure is reported to the user instead. | covered |
| `tool.Bash.s.8` | Temporary files go under `$TMPDIR`. | covered |
| `tool.Bash.s.9` | `autoAllowBashIfSandboxed` (default true): a command that runs sandboxed needs no approval. | covered (not in plan mode) |
| `tool.Bash.s.10` | An `allowed_domains` parameter widens the network for one sandboxed command (auto mode). | n/a — sandbox-on only; the baseline runs unsandboxed (`parity/deviations.md`) |
| `tool.Bash.s.11` | Platform notes (macOS `open`/`osascript`, Windows localhost and TLS, git repositories made in the sandbox, `pytest` cache). | n/a — sandbox-on only (`parity/deviations.md`) |
