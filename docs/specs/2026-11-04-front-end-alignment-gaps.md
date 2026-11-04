---
status: active
created: 2026-11-04
branch: v1-align
---

# Front-end alignment gaps left by v1-finish

## Goal

Close the Claude Code alignment gaps that `archive/2026-11-04-v1-finish.md` left open in
`tui/` and `web/`, and find why the `compaction_resume_edits` eval scenario stalls. Same rule
as v1-finish: the smallest mechanism that is correct; a gap that needs a new subsystem is cut
with a reason, not built.

## Non-goals

- `TaskState.history` out of the fold (its own spec).
- The CSP's `connect-src` beyond `'self'` (a deliberate security cut).
- Anything Claude Code 2.1.318 does not have.

## Context

Open after v1-finish: TUI — `N% until auto-compact` (the compaction plugin publishes no
threshold), Tab-to-amend, Ctrl+R across sessions, `/mcp` `/hooks` `/init` `/review`, Tab
completion inside a command word, `noeta -c` with nothing to continue starts a new session.
Web — no session search, archive or delete; no auto-compact indicator. Eval —
`compaction_resume_edits` times out 3 of 6 trials at the eighth file with no compaction, on
`main` before v1-finish too.

## Decisions

- D1 The compaction plugin publishes its threshold as a record front ends may read (a typed
  reader in `noeta/toolkit/plugin_records.py`), so both front ends compute "N% until
  auto-compact" the same way.
- D2 `/init` and `/review` are prompt commands every front end gets through `AppAPI.commands`,
  not TUI code. `/mcp` and `/hooks` show what the task has (servers and their state; hooks
  configured), read-only.
- D3 Prompt history across sessions is kept by noeta in its own home (as Claude Code keeps
  its history file), per project.

## Plan

- [ ] A TUI + host: D1, D2, D3, Tab-to-amend, Tab completion inside a command word, `-c` exits
      when nothing to continue.
- [ ] B Web: session search, archive, delete (host routes as needed); auto-compact indicator
      once A's reader lands.
- [x] C Eval: why `compaction_resume_edits` stalls at the eighth file; fix the cause.
- [ ] Integrate, full gates, review.

## Acceptance criteria

- Each item has a test that fails without it; `make check`, `make frontends`, `make parity`,
  `make cluster` green; `compaction_resume_edits` no longer times out on a 6-trial run.

## Risks

- A and B both touch `noeta/host`; B starts on its own routes and takes A's reader after A
  merges.

## Progress log
- 2026-11-04 Created after the v1-finish goal check found these alignment gaps still open.
- 2026-11-04 C done: the hang was the first compaction's summarize call. The Anthropic adapter's
  60 s stream-silence timeout also bounded the wait for response headers, and the eval gateway
  sends headers only once the answer is complete, so a summary taking over 60 s timed out and
  was retried ~20 min. Headers now wait up to Claude Code's request timeout (10 min); silence
  after the stream began is still 60 s. 6 trials: 1.00 each, no timeouts (was 3 of 6).
