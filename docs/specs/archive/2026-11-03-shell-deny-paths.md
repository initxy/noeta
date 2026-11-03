---
status: done
created: 2026-11-03
branch: shell-deny-paths
---

# Shell writes held to Edit deny rules

## Goal

A `Bash` / `Monitor` command that writes a path an `Edit` deny rule covers is refused in every
mode, as in Claude Code 2.1.312. Found by the 9-trial eval
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
  noeta does the same in `RuleCheck.decide`, right after the deny rules (`_shell_write_denied`,
  targets from `scope.deny_targets`).
- Targets are read off loosely (any flag skipped, a `sed` script of any shape), since they can
  only refuse. Words bash expands are not judged (Claude Code asks about those; noeta's other
  checks still do).
- noeta judges a relative path under every directory the command may be in (its existing `cd`
  tracking); stricter than Claude Code after a non-leading `cd`: a `deviations.md` row.
- The refusal names the path and the rule in noeta's words (`wording.SHELL_WRITE_DENIED`).
- Not done here: Claude Code also holds shell reads (`cat`, `head`, `grep`, a `cp` source, input
  redirection) to `Read` deny rules; noeta does not yet.

## Progress log

- 2026-11-03: CC behaviour read from the bundle; noeta probe: all 9 write forms ran in bypass and
  `acceptEdits` (redirections and `tee` asked in `acceptEdits`), none refused. Tests first, fix,
  parity row `tool.Bash.24`, deviation row, CHANGELOG.
