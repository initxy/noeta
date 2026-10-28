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
| `tool.Bash.p.dangerouslyDisableSandbox` | `dangerouslyDisableSandbox` | boolean | no | true asks to run outside the sandbox | covered in the schema; no effect until the local sandbox (spec Phase 3) |
| `tool.Bash.p.schema` | (schema shape) | - | - | `$schema` 2020-12, `additionalProperties: false`, a `description` on each property | covered |

## Rules

| id | rule | noeta today |
|---|---|---|
| `tool.Bash.1` | Purpose line: runs a bash command and returns its output. | covered |
| `tool.Bash.2` | A `cd` persists into the next call only in the main conversation (not in sub-agents), only when the command succeeds, and only when the final directory is inside the working directories. | covered |
| `tool.Bash.3` | When a command ends outside the working directories, the next call starts back in the starting folder; otherwise the directory carries over. | covered (resets to workspace root, adds a reset note) |
| `tool.Bash.4` | Absolute paths are preferred; a `cd` inside a compound command may trigger a permission prompt. | covered |
| `tool.Bash.5` | Environment variables and shell functions do not persist between calls. | covered |
| `tool.Bash.6` | The shell is initialized from the user's login profile. | contradicts: noeta runs with a scrubbed, secret-free environment |
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
| `tool.Bash.21` | Default-mode variant only (`m2`): avoid `cat`, `head`, `tail`, `sed`, `awk`, `echo` unless told to or after confirming no dedicated tool fits; use the dedicated tool instead. In `auto` mode (baseline) this line is absent and the environment message instead says Bash is fine for reading, searching and small mechanical edits. | covered (no dedicated-tool steering; the default-mode variant is not built) |
| `tool.Bash.22` | The description states no output-size cap. (Observed: a 12005-character output came back whole.) | covered (no cap stated; output past 30000 characters is still spilled to a file) |

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
| `tool.Bash.r.8` | attached context | a result may carry `<system-reminder>` blocks appended after the output (after compaction, the `CLAUDE.md` and git-status reminders rode on the first tool result, `m1/0059`) | see `compaction.md` |

## Variants

- `m2` (default permission mode) adds `tool.Bash.21`'s "avoid `cat`/`head`/..." line; schema is
  identical. `s1` (OAuth) is identical to `m1`.
