---
status: done
created: 2026-11-03
branch: shell-deny-paths
---

# Shell paths held to deny rules

## Goal

A `Bash` / `Monitor` command that writes a path an `Edit` deny rule covers, or reads one a
`Read` deny rule covers, is refused in every mode, as in Claude Code 2.1.312. Found by the 9-trial eval
(`evals/results/2026-11-03-main-18ef3317.json`, `deny_rule_respected`, one trial 0): in
`bypassPermissions`, `cd <ws>/config && sed -i ... dev.toml staging.toml prod.toml` ran and
changed the file `Edit(./config/prod.toml)` denies.

## Decisions

- Claude Code (2.1.312 bundle, the shell path check): the operands of its path-checked writers
  (`mkdir`, `touch`, `rm`, `rmdir`, `mv`, `cp`, `sed` unless read-only, `tee`) and output
  redirection targets are matched against deny rules on `Edit` only (not `Write`); a match
  returns deny from the tool's own check, which comes before the mode step, so bypass and
  `acceptEdits` do not get past it. A leading `cd <dir> &&` inside the working directories sets
  the directory relative paths resolve in; after any other `cd` only absolute paths are checked.
  noeta does the same in `RuleCheck.decide`, right after the deny rules (`_shell_path_denied`,
  paths from `shell_paths.command_paths` via `scope.deny_targets`).
- Paths are read off the words as Claude Code reads them (flags skipped up to the first operand,
  then every word counts), with a `sed` script of any shape. A word bash expands (`$VAR`,
  `$(...)`) is not judged; in `bypassPermissions` such a path is not checked at all, as in Claude
  Code, but the commands inside a substitution are.
- noeta judges a relative path under every directory the command may be in (its existing `cd`
  tracking); stricter than Claude Code after a non-leading `cd`: a `deviations.md` row.
- The refusal names the path and the rule in noeta's words (`wording.SHELL_WRITE_DENIED`,
  `SHELL_READ_DENIED`).
- Reads, in the same step: Claude Code's path-checked readers (its list, `tool.Bash.25`), a `cp` /
  `mv` source, a `sed -i` file and a `<` input are matched against `Read` deny rules. Each
  program's paths are read off its words the way Claude Code reads them (`shell_paths.py`: a
  search's pattern, an `awk` / `jq` / `sed` program are skipped; `-f` files count; `ls`, `find`,
  `rg`, a recursive `grep` default to the current directory; only `git grep` / `git diff`).
  Programs it does not check (`less`, `xxd`, interpreters) are not checked here either.
- Leading assignments and Claude Code's pass-through programs (`time`, `nohup`, `timeout`,
  `nice`, `stdbuf`, `env`, `command`, `builtin`, `noglob`) are looked through on both sides.
- Review (independent, on `e909af0f`) found ways past the check; all closed, each in the one
  deviation row (refusing more than Claude Code, which asks about a subshell or command group and so
  lets it run in bypass): a lenient parse (`shell_syntax.parse(lenient=True)`) reads past comments, heredocs
  (an unquoted one's substitutions are commands), subshells, brace groups and substitutions; a
  command still unreadable is refused when its text names a covered path; write and read globs
  are expanded (lead globs too; a quoted bracket is no glob); `>&` to a file is a write; `nice`
  / `timeout` / `stdbuf` / `env` options are skipped, `env -C` moves the directory, `xargs`,
  `sudo`, `doas` are looked through; `ln` and `install` are writers; directory tracking goes on
  outside the working directories and through `pushd`; a recursive `rm` or an `mv` of a
  directory holding a place-tied `Edit` deny path is refused. `find -path` values stay paths, as
  in Claude Code's table (quoted, so not expanded).
- Cost: a kind with no deny rules is skipped (no parse at all without `Edit` / `Read` deny
  rules); paths are matched as named first; globs and links are resolved in one call to the
  task's env (`shell_check.resolve_paths`, `realpath -m -z`; one call per path only if that
  fails).
- The twelve programs Claude Code reads with a per-flag table are read as plain operands (a
  flag's value may count as a path; same row).

## Progress log

- 2026-11-03: CC behaviour read from the bundle; noeta probe: all 9 write forms ran in bypass and
  `acceptEdits` (redirections and `tee` asked in `acceptEdits`), none refused. Tests first, fix,
  parity row `tool.Bash.24`, deviation row, CHANGELOG.
- 2026-11-03: coordinator: close the read side on the same branch. Read the readers' table and
  argument readers from the bundle; probe: none of 29 read forms was refused in any mode. Tests
  first (29 forms × 4 modes refused, 10 neighbours run, 43 argument-reading cases), then
  `shell_paths.py`, the `Read` check in `_shell_path_denied`, parity row `tool.Bash.25`, the
  deviation row widened, CHANGELOG.
- 2026-11-03: independent review of `e909af0f`: opaque commands, write globs, lead globs, `>&`,
  wrapper options, `env -C`, `pushd`, `cd ..` out of the tree, links, `rm -rf` of the directory.
  Tests first (28 evasive writes, 7 evasive reads refused; 24 everyday commands run beside an
  `Edit` rule; all 52 run with no path deny rule), then the lenient parse, `deny_paths.py`, the
  batched resolve, docs. Reviewer probes (`probe.py`, `probe2.py`) rerun through the check: every
  listed bypass refused, the benign ones run.
