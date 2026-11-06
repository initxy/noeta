---
status: done
created: 2026-11-01
branch: empty-turn
---

# An empty reply asks the model to go on

## Goal

A model reply with no text and no tool call no longer loses the task. noeta handles it at least
as well as the best of Claude Code 2.1.312, pi and DeepSeek Harness (dsh), with the evidence
below.

Trigger: Terminal-Bench 4.0 on DeepSeek V4.1 Flash. Three trials ended mid-task after 23, 45
and 54 rounds because one reply came back empty
(`end_turn`, nothing in it): `call_model` failed the turn with `llm_empty_response`, and `-p`
exited 1. The same failure showed in an eval run (`hook_block_adapts` trial 8, on the first
request).

## Non-goals

- A reply that stopped with `tool_use` but carries no parsable call (Claude Code retries it once
  with a "the tool call failed, retry it" nudge), or an empty `refusal`: both still fail with
  `llm_empty_response`, as before.
- Sending the output-limit nudges (`OUTPUT_LIMIT_*_NUDGE`) unwrapped. Claude Code sends them as
  plain text too; noeta wraps them in `<system-reminder>`. Separate change.

## Context

### Claude Code 2.1.312

Established two ways: reading the bundle (`~/.local/share/claude/versions/2.1.312`, its strings
searched for the nudge text and the guard around it) and running that binary against a fake
Messages endpoint (a small local server that answers main-loop requests from a script and logs
every request; fresh `CLAUDE_CONFIG_DIR` and `HOME`, API-key style auth, no network).

Bundle: in the query loop, after a reply with no tool call, if the stop reason is `end_turn` or
`stop_sequence`, the reply is not an API error, the request is not a compaction or a few side
queries, and no assistant block of the reply has text that is more than whitespace, then:

- if the guard `thinkingOnlyNudged` is off: the reply is dropped, a meta user message with the
  fixed text `[Your previous response had no visible output. Please continue and produce a
  user-visible response.]` is appended, the guard turns on, and the loop calls the model again
  (`transition: thinking_only_retry`);
- if it is on: logged `nudge_exhausted`, and the turn ends normally. Only behind a server flag
  (`tengu_calm_waffle`, default off), when the reply had no assistant message at all and the
  last message is a real user prompt in the main thread, an API-error message "The model
  returned an empty response (retry also failed)." ends it instead.

The guard is reset (`...pi` initial guards) on the `next_turn` transition after tool results,
and starts off for each new prompt. So: one nudge per step.

Runs against the fake endpoint (scripts and logs in the session scratchpad, `cc/`):

| script (main-loop replies) | mode | requests | outcome |
|---|---|---|---|
| empty, empty | `-p --output-format json` | 2 | `success`, `result: ""`, exit 0 |
| empty-text, empty-text | `-p` | 2 | same |
| thinking-only ×2 | `-p` | 2 | same; the thinking block is not resent |
| tool, empty, empty | `-p` | 3 | same |
| tool, empty, text | `-p` | 3 | `success`, `result: "All done."` |
| tool, empty, tool, empty, empty | `-p` | 5 | nudged after each tool round (requests 3 and 5) |
| empty, empty, then a new prompt | interactive (tmux) | 3 | nothing shown for the empty turn; the next prompt's request still holds the nudge |
| tool, empty, empty + Stop hook | `-p` | 3 | the Stop hook runs once, at the end, with no `last_assistant_message` |

On the wire the nudge is plain text (no `<system-reminder>`), added to the user turn that
precedes it (after the prompt, or after the tool results); it persists in later requests. With
the model the parity baseline uses (`--model claude-opus-5-5`, which gets `role: "system"`
notes) the step's system note (`<total_tokens>`) still ends the request, after the user turn
that now holds `tool_result | nudge` (runs `p-opus-mid`, `p-opus-first`); with
`claude-sonnet-4-5` the note rides inside the tool result instead.

Two more conditions in the bundle skip the nudge: `nLr(H)` is true when the newest tool results
(since the last real prompt) include a successful call of a tool named in
`CLAUDE_CODE_TERMINAL_MCP_TOOLS` (MCP tools whose result is meant to end the turn), and `Me(H)`
is true when the model called `StructuredOutput` since the last real prompt (the answer was
given through the tool). noeta needs neither: a tool that ends the turn
(`ToolOutput.end_turn`) ends it without another model call, and structured output is the
provider's `output_schema`, not a tool (`rem.empty-reply.5`).

A dropped reply still counts in Claude Code's usage: tool, empty, empty reports `num_turns: 3`
(`p-opus-mid2`). Its `-p` result also carries `terminal_reason: "completed"`, which noeta's
headless result does not have for any turn (not specific to this change).

### pi (pi-mono, `packages/coding-agent` 1.4.0, local checkout)

`packages/agent/src/agent-loop.ts`: a reply with no tool calls ends the inner loop; there is no
check for an empty reply, no nudge and no retry (`isRetryableAssistantError` only retries
`stopReason: "error"` with a transient message). The empty assistant message stays in the
session; `openai-completions.ts` skips an assistant message with no content and no tool calls
when it builds the next request. Print mode prints nothing and exits 0. So mid-task, the task is
lost silently.

### DeepSeek Harness (dsh 0.2.2-alpha.2, local checkout)

`packages/llm/llm-deepseek/src/translate.ts` and `packages/llm/llm-pi-ai/src/stream.ts`: a
`stop` with **zero content blocks** raises `EMPTY_RESPONSE`, which `llm/src/retry-policy.ts`
lists as retryable by default: the same request is sent again, up to 5 retries with backoff
(0.5 s doubling to 10 s, jitter). Exhausted, the turn fails. A reply with only reasoning or only
empty text is a normal `stop` and ends the turn.

### noeta before

`noeta/core/runner/model.py`: any empty reply other than a `max_tokens` cut failed the turn
(`llm_empty_response`); a one-shot task ended `failed`, `-p` exited 1.

## Decisions

- Copy Claude Code. On `end_turn` with no text and no tool call (no content, empty text, or
  reasoning only): do not record the reply; record a `continue` entry whose message is Claude
  Code's nudge text (ADR-020), origin `inline` so it goes out as plain text; the Anthropic
  adapter adds it to the user turn before the step's system notes, where Claude Code puts it.
  The entry has `reason: "empty_reply"`; folding it sets `TaskState.empty_nudged`, which a
  recorded reply or any new message (prompt, notice, sub-agent end, mid-turn message) clears.
  So "asked already" is folded from the log (another worker or a restart sees it, and a
  compaction that drops the nudge text changes nothing), not found by searching the messages.
- A deliberate `continue` (output-cap continuation or this nudge) starts `model_attempts`, the
  crash-loop guard, again: it was meant for calls without any reply, and two reasoning-only cuts
  plus an empty reply used to trip it before the nudge was answered. Claude Code keeps its
  output-cap and empty-reply counters apart from each other.
- A dropped reply's `model` and `usage` ride on its `continue` / `turn_done` entry (also for a
  reasoning-only cut), and `noeta/host/usage.py` counts them, so `/cost`, the headless
  `usage` / `total_cost_usd` / `num_turns` and the per-turn token count match Claude Code's.
- An empty reply right after the nudge ends the turn quietly, as Claude Code does: a
  `turn_done` entry with `reason: "empty_reply"` (old readers parse `turn_done` ignoring its
  data); the turn's answer is empty (the preamble before an earlier call is no answer); `-p`
  reports `success` and exits 0; the hooks plugin runs Stop / SubagentStop without
  `last_assistant_message`.
- Why not dsh's retry of the same request: it covers only zero-block replies (DeepSeek's
  reasoning-only empty turns are not caught), and resending an identical prompt to a model that
  just answered it with nothing tends to repeat; the nudge changes the prompt. Why not more than
  one nudge per step: no evidence that a second nudge rescues what the first did not, and Claude
  Code's limit bounds the cost of a model that keeps answering nothing.
- Why not keep failing after the nudge: Claude Code ends quietly (its error path is behind a flag
  that is off), and a failed turn and a quiet end lose the task alike; the difference left is the
  exit code, which follows Claude Code.
- Rules `rem.empty-reply.1`–`5` in `parity/rules/reminders.md`. One accepted deviation
  (`parity/deviations.md`, Reminders): a blocking Stop hook after the quiet end starts the
  count again (one more nudge), where Claude Code keeps the flag; the hook's reason reaches the
  task as a new message like any other, and the kernel's fold cannot tell it apart without the
  hooks plugin's state.
- Mixed versions: a worker without this change reads the new entries' known part only. It
  folds `turn_done` as before (the turn ends) but keeps the earlier answer of that turn and runs
  no Stop hook; it ignores `continue`'s reason, so it would fail an empty reply after the nudge
  (its old behavior) rather than end quietly; and it does not count a dropped reply's usage.

## Plan

- [x] Establish Claude Code's behavior (bundle + fake-endpoint runs), pi's and dsh's (source).
- [x] `after_empty_reply` in `noeta/core/runner/closing.py`; `call_model` routes `end_turn`
      empties to it; `TurnDone.reason`; fold clears the answer; Stop hooks on the quiet end.
- [x] Tests through the runtime with a scripted provider (`tests/core/test_loop_behaviors.py`,
      `tests/plugins/test_hooks_events.py`); noeta `-p` against the same fake endpoint.
- [x] Parity rules, CHANGELOG.
- [x] Gates: `make check`, `make parity`, `make cluster`, eval vs `evals/baseline.json`.
- [x] Review fixes: per-step flag folded from the log, `continue` resets the crash guard,
      dropped replies counted, nudge placed in the user turn before the step's notes, tests for
      the review's repros, SubagentStop, entry decoding; gates and eval again.

## Acceptance criteria

- An empty `end_turn` reply is never recorded; the next request ends with the nudge, as plain
  text; a following reply with text or a call continues the task.
- One nudge per step; an empty reply after it ends the turn: interactive waits for input with an
  empty answer and no `turn_failed`; one-shot ends `done`; `-p` exits 0.
- `make check` green, `make parity` 0 mismatches, `make cluster` all pass, eval no regression
  beyond the known-noisy scenarios.

## Risks

- A model that answers nothing twice in every step still ends the turn early, now reported as
  done rather than failed; a host that relied on `llm_empty_response` for `end_turn` replies no
  longer sees it (it still does for other stop reasons).

## Progress log

2026-11-01: CC 2.1.312 bundle read and run against a fake endpoint (8 scripts, `-p` and
interactive); pi and dsh read from local checkouts.
2026-11-01: implemented; noeta `-p` against the same fake endpoint gives the same outcomes
(3 requests, `success`, empty result, exit 0 for tool/empty/empty; "All done." for
tool/empty/text). The request layout then differed (nudge as its own user message after the
step's system note); fixed after review, see below.
2026-11-01: gates. `make check` green (2606 passed, 60 skipped; coverage 89 %; mypy and lint
clean; check-docs green). `make parity` 0 mismatches (11 accepted deviations), own-words none.
`make cluster` 14 passed. Eval (DeepSeek V4.1 Flash, 9 trials, concurrency 8):
overall 0.945 vs baseline 0.953, "no regression"; no trial ended on `llm_empty_response`.
Moves beyond 0.06: `delegate_parallel_review` 0.00 vs 0.33 (4 timeouts; known-noisy),
`compaction_resume_edits` 1.00 vs 0.78 (known-noisy), `compaction_continuity` 0.89 vs 1.00
(one trial ended its turn with text after six of eight parts: a normal `end_turn` reply, not an
empty one), `hook_block_adapts` 0.47 vs 0.52 (known-noisy).
2026-11-01: review (no P0/P1). Fixed: P2 `model_attempts` counted cap continuations and the
nudge (two reasoning-only cuts + empty → `llm_error`); P2 the nudge search crossed into a turn
opened by a notice (no nudge there); usage of dropped replies; wire placement; mixed-version and
`nLr`/`Me` notes. Rebased onto main cdc2cb2e. noeta `-p` vs the fake endpoint now: tool, empty,
text → request 3 is `[assistant][user: tool_result | nudge][system: total_tokens]`, as Claude
Code's Opus run; tool, empty, empty → `num_turns: 3`, `result: ""`, exit 0, as Claude Code.
2026-11-01: gates after review fixes (on cdc2cb2e). `make check` green (2619 passed, 60
skipped; coverage 89 %; it prints `gc due` for this spec being active with every task ticked,
which merging resolves). `make parity` 0 mismatches (11 accepted deviations), own-words none.
`make cluster` 14 passed. Eval (9 trials): overall 0.947 vs baseline 0.953, "no regression"; no
`llm_empty_response`. Moves beyond 0.06, all known-noisy: `concise_direct_answer` 0.78 vs 1.00
(four answers added a second sentence), `hook_block_adapts` 0.39 vs 0.52 (two 420 s timeouts),
`compaction_resume_edits` 0.88 vs 0.78; `delegate_parallel_review` 0.33 as the baseline (6
timeouts).
2026-11-01: confirm-review follow-ups. The eval counts dropped replies in `requests` and records
per-trial `empty_nudges` (`evals/engine.py`, `trial_usage`; `tests/test_eval_usage.py`); the
one-byte `"\n"` gap when Claude Code joins user text blocks went to the open-source follow-ups
spec. `make check` green (2620 passed), `make parity` 0 mismatches.

2026-11-01: `concise_direct_answer` (0.78 in the 9-trial run) A/B against main at 460ae7c7, two
rounds of 18 and 27 trials: branch 0.83 then 0.94, main 0.94 then 0.76; no empty-reply nudge
in any branch trial. Noise in the 40-word line, not this change. Merged and archived.
