---
status: active
created: 2026-10-26
branch: fix/audit-closure-2026-10-26
---

# Close the 2026-10-16 audit's correctness findings

## Goal

The P0s and the agreed P1s from the 2026-10-16 audit (mechanism, context, tools) are fixed in
one release, so a deny rule always denies, a cancelled task stays cancelled, compaction does not
blow the window it is meant to save, and a crash never re-runs a tool call that already finished.

## Non-goals

- Raising host dependency floors and merging Aisthon branches (owner: not this round).
- Goal auto-continue, a timer tool, a Stop hook, deadline / per-turn budgets (owner: next round).
- The `make eval` harness — tracked in `2026-10-26-prompt-regression-eval.md`, done alongside.
- Audit items not listed under Plan (P2s, MCP transport findings, memory/skill append-not-rewrite).

## Context

Audit reports are not in the repo; the findings are restated in Plan in enough detail to act on.
Line numbers are as of `adc2d1c`.

## Decisions

- **Deny wins over approval.** The guard chain evaluates every guard; any deny returns the deny,
  otherwise the first non-allow (needs-approval) is returned. A raising guard still denies.
- **Terminal is irreversible in fold.** Once status is `terminal`, lifecycle events
  (`TaskSuspended`, `TaskWoken`, …) no longer change status. Cancel is durable across processes:
  cancel moves the dispatcher row to terminal and the running step checks a durable cancel signal
  at each step / tool-call boundary, not only the in-process `CancellationRegistry`.
- **Crash recovery keeps finished tool calls (refines ADR-032).** An interrupted attempt whose
  decision is recorded keeps that decision and every `ToolCallFinished`; a call that started but did
  not finish gets a synthesized error result ("interrupted by a crash; its outcome is unknown —
  check before retrying"), never a re-execution; a call that never started is executed normally.
  The step then continues from "all results present". Attempts with no recorded decision re-drive
  as today. Approval-window parks are unchanged. Written up as a new ADR refining 032.
- **Caller idempotency key.** `send_goal` / `inject_goal` take `idempotency_key: Optional[str]`.
  A repeat with the same key on the same task returns the first call's outcome and appends
  nothing, durably across processes. Unkeyed calls behave as today.
- **Summarize sees the main loop's view.** The summarize input replaces every tool result the
  current view has cleared with the same marker. On a context-overflow error the summarize call
  retries up to 3 times, each time dropping the oldest ~20% of whole turns behind a leading
  `[earlier conversation truncated for compaction retry]` line (Claude Code's behavior).
- **Clearing old tool results is batched.** Once the clearing valve opens it clears down to the
  newest 5 clearable results in one go, then holds the view byte-identical until at least 20,000
  newly clearable tokens accumulate. Only tools declaring themselves clearable count and get
  cleared (Read, Bash, Grep, Glob, WebFetch, WebSearch, Edit, Write, BashOutput) — not Task
  reports, AskUserQuestion answers, MCP results.
- **Compaction margin like Claude Code.** Effective window = window − min(max_output, 20,000);
  auto-compaction triggers at effective − 13,000. With `auto_compact_window` set, the same
  formula applies to that cap, and no full max-output is subtracted.
- **Continued output joins.** After output-cap continuations, the final answer (and its
  `output_schema` parse) is the concatenation of the continued assistant texts.
- **Disabling Bash disables the shell.** `disallowed_tools` containing `Bash` also removes
  Monitor, BashOutput and KillShell; `PreToolUseRule(match_tool="Bash")` also applies to Monitor's
  `command`.
- **Background notices go to the spawner.** A background shell / Monitor started by a subtask
  notifies that subtask while it is live, falling back to the root once it has ended.
- **Task mixed with other tools runs.** A response with `Task` plus ordinary tool calls executes
  all of them instead of refusing the round.
- **Heartbeat survives transient errors.** Non-lease errors back off and retry; `InvalidLease`
  cancels the in-flight step.
- **No fixed ~3 h turn ceiling.** The heartbeat cap counts per attempt and is configurable; hitting
  it goes through normal recovery instead of failing or stranding the task.
- **Provider quirks.** Anthropic "input length and `max_tokens` exceed context limit" and the
  wider overflow wording set (incl. HTTP 413) count as context overflow; OpenAI-compat `"null"` /
  `""` arguments decode as `{}`, other non-objects raise `MalformedToolArgumentsError`;
  `finish_reason="stop"` with `tool_calls` is a tool-use stop; `ThinkingBlock` records its provider
  family and adapters drop foreign-family thinking on the way out.
- **Bash output without a spill dir keeps head and tail**, so the trailing error survives.

## Plan

- [ ] A1 deny-wins guard chain (`noeta/core/hooks.py`)
- [ ] A2 terminal-irreversible fold + durable cross-process cancel
- [ ] A3 heartbeat retries transient errors; InvalidLease cancels the step
- [ ] A4 per-attempt, configurable heartbeat cap; cap → recovery
- [ ] A5 `idempotency_key` on `send_goal` / `inject_goal`
- [ ] B1 tool-level crash recovery + ADR refining 032 + limitations doc
- [ ] C1 summarize input mirrors cleared view; overflow retry ×3
- [ ] C2 batched clearing with clearable-tool set
- [ ] C3 compaction margin formula
- [ ] C4 continuation answer join
- [ ] D1 Bash disable covers Monitor/BashOutput/KillShell + hook rules
- [ ] D2 background notices to the spawning subtask
- [ ] D3 Task mixed with ordinary tools executes
- [ ] D4 provider quirks (overflow markers, null args, stop+tool_calls, thinking provenance)
- [ ] D5 Bash head+tail without spill dir
- [ ] Integrate, `make check`, relevant `make e2e`, CHANGELOG, CONTEXT/ADR updates
- [ ] Benchmark rerun on the integrated build

## Acceptance criteria

- Each Plan item has a regression test that fails before the fix and passes after.
- `make check` green; the live tests touching compaction, providers and background shells pass.
- A kill -9 mid-batch in bypassPermissions re-drive does not re-run a finished Bash call.

## Risks

- B1 changes recovery semantics on a hardened path; the recursion / cap behavior of ADR-032 must
  keep holding.
- C2 changes cache shape for every long task; verify with recorded `cache_read` in an e2e run.

## Progress log
2026-10-26 spec written from the 2026-10-16 audit; baseline make check green (4824 passed, 89%).
