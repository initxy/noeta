---
name: commit
description: Draft a clear git commit for the current changes and commit them
argument-hint: [extra intent, e.g. "only the parser fix" or a message hint]
---

# Commit changes

Stage the relevant changes and write a single, well-scoped git commit that explains
*why* the change was made, not just what changed.

Extra intent from the user (optional scope or message hint): $ARGUMENTS

## Steps

1. See the current state. Run `git_status` to list modified, staged, and untracked
   files, then `git_diff` to read the actual hunks. If the user named a scope above,
   limit the commit to the matching files instead of everything.
2. Understand the change before describing it. For any hunk that isn't self-evident,
   `read` the surrounding code so the message reflects the real intent rather than
   a literal restatement of the diff.
3. Decide what belongs in this commit. Keep it to one logical, self-contained change.
   If the working tree mixes unrelated changes, stage only the files for this commit
   with `shell_run` (`git add <paths>`) and leave the rest for a separate commit.
4. Draft the message:
   - Subject line: imperative mood, concise (aim for under ~72 chars), no trailing
     period — e.g. `fix parser crash on empty input`.
   - Body (when the change isn't trivial): wrap at ~72 cols and explain the motivation
     and any non-obvious trade-offs. Skip the body for one-line obvious changes.
   - Match this repo's existing commit conventions if it uses them (e.g. a
     `type(scope):` prefix) — check recent history with `shell_run`
     (`git log --oneline -10`) before choosing a style.
5. Create the commit with `shell_run` (`git commit -m "..."` or a heredoc for multi-line
   messages). Then run `git_status` to confirm a clean result and report the final
   subject line.

Do not push, open a PR, or amend existing commits unless the user explicitly asks.
Never invent changes you didn't make to pad the message.

(fork agent: main)
