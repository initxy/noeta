---
status: active
created: 2026-10-30
branch: bench-fixes
---

# SWE-rebench midway findings: causes in noeta

## Goal

Find, from the trial logs of the running SWE-rebench comparison (noeta vs Claude Code 2.1.312,
same gateway models, 50-minute cap), why noeta makes fewer model calls in the same time, why it
emits more output per call on `auto_model/alwaysday1_max`, and why it lost two tasks; fix what
is a noeta defect, copying Claude Code where noeta differs without a written reason.

## Non-goals

- Changing the benchmark itself (`bench/`, the run on `gpu-cn`): the findings about its
  configuration are reported to its owner, not applied here.
- Reversing ADR-004 (project trust) or the documented `WebSearch` deviation.
- Running `Bash` calls concurrently by command (see Follow-ups).

## Context

Evidence: copies of `jobs/rb-ds41` (30 noeta + 30 Claude Code trials, `opensource/deepseek_v4_1_flash`)
and `jobs/rb-ad1max` (2 + 2 trials, `auto_model/alwaysday1_max`); Claude Code's session logs
carry a timestamp per block, noeta's `stream-json` output carries none, so noeta's timing comes
from the `result` line of the runs that finished (`duration_ms`, `duration_api_ms`). Request
bodies were captured offline: Claude Code 2.1.312 and noeta `-p` against a local fake Messages
endpoint, same flags as the benchmark (`--model auto_model/alwaysday1_max --effort high`,
bypass permissions, the benchmark's models manifest for noeta).

Findings:

1. **Request parameters are the same.** For a model Claude Code does not know, with `--effort
   high`, both send `thinking: {type: adaptive, display: updates}`, `context_management`
   `clear_thinking_20251015 keep all`, `output_config.effort: high`, `max_tokens` = the
   configured cap, `stream: true`, no `temperature`; both replay earlier thinking blocks (the
   gateway returns them with an empty signature) the same way. The suspected effort/thinking
   difference does not exist.
2. **No harness overhead between calls.** 60 scripted steps with ~1.4 MB of history against a
   fake endpoint: noeta 0.25–0.28 s per step, Claude Code 0.32–0.37 s. No retries in either
   arm's logs (Claude Code emits `api_retry` lines: none; noeta's `duration_api_ms` includes
   retries and matches its token rate).
3. **Where noeta's time goes (DeepSeek, finished runs: 8 noeta, 7 Claude Code).** Model time per
   call tracks output tokens per call; the gateway is not slower for noeta (output tokens per
   API second per finished run: noeta 15–24, Claude Code 14–26). Over all 30 trials noeta's calls average 1006 output tokens vs 930 (+8%), and
   outside model calls noeta spends 13.1 s per call vs 6.7 s: the commands the model chooses
   (Gradle/Maven test runs; `pulsar-25865`: 1362 s vs 329 s). Together ≈ the 68.2 vs 58.9 s per
   call measured from wall time. Claude Code's prompts are not shorter (mean input 50.6K vs
   42.8K tokens per call) and both arms read ~96–97% of input from cache.
4. **Serial `Bash`.** noeta runs `Bash` calls of one reply one after another
   (`Tool.read_only` is per tool; `Bash` is `False`); Claude Code runs read-only commands of
   one reply together. Bound on the cost: in Claude Code's 1443 calls, replies with several
   tool calls spent 985 s in tools in all (median 0.04 s), about 1% of model time.
5. **`<total_tokens>` sent to every model.** Claude Code sends the budget line (system prompt
   end) and the per-step `<total_tokens>` notes only for models whose catalog row has the
   `total_tokens_reminder` capability: every Claude model it ships, never an unknown id
   (2.1.312 bundle; confirmed by capture: zero occurrences for the gateway model, see
   `2026-10-30-bench-findings-capture.json`, which also lists which selectors Claude Code
   treats as its own models). noeta sent
   both to every model. A/B on `alwaysday1_max` through the live gateway (5 snapshots of the
   `nanobot-4129` trial, 4 samples each, with vs without the lines): mean output 1013 vs 1174
   tokens per call, within noise. So this is a parity defect, not the cause of finding 6.
6. **2.5x output on `alwaysday1_max` is not explained.** Per-call output is heavy-tailed: the
   top 5 calls are 46–47% of each noeta run's output (6–11K-token thinking episodes, 29–46K
   characters of thinking); medians are close (376 vs 532, 562 vs 249). With identical
   parameters and history handling and two samples per arm, trajectory variance is the only
   supported reading.
7. **`lossless-claw-814`.** Claude Code fetched the upstream fix: `WebFetch` of
   `github.com/Martian-Engineering/lossless-claw/pull/814/files` and the raw patch. noeta had
   `WebFetch` (deferred) and did not use it; it has no `WebSearch` without a search key (a
   listed deviation). The gateway serves Anthropic's server-side web search, so Claude Code's
   arm has a search tool noeta's lacks.
8. **`codex-lb-744`.** noeta ran `-p` in an untrusted project, so `.agents/skills` was not loaded
   (ADR-004); Claude Code (`-p` skips its trust dialog) loaded that directory, `.claude/skills`
   and `CLAUDE.md`, and ran the `project-conventions` skill. noeta had working code and green
   unit tests by call 32 and spent the rest on the project's OpenSpec change folder.
9. Smaller `-p` differences seen in the capture: noeta offers `AskUserQuestion` (Claude Code
   does not in non-interactive mode), sends a title side request (Claude Code does not under
   `-p`) and the `per-turn-control` beta (Claude Code sends it only to models with per-turn
   effort). None changes the main loop's requests.

## Decisions

- Budget notes follow Claude Code: only a model Claude Code knows as a Claude model gets the
  `<total_tokens>` notes; for any other model the environment plugin also takes the budget
  line out of the system prompt wherever it sits as its own line (`before_model`; an appended
  prompt or a plugin's part may follow it), so `main.md` keeps the line for Claude models and
  the parity replay is unchanged. `token_budget=0` now also drops the prompt line.
- What "knows" means (`gets_budget`), taken from Claude Code 2.1.312 by capture: after noeta's
  alias resolution (a built-in alias, or a host alias that targets a Claude row), the id,
  lowercased, contains one of Claude Code's 22 catalog Claude ids (`claude-3-5-haiku` …
  `claude-mythos-5-1`), or the dated first-party id of a 4.0 model (`claude-opus-4-2025…`).
  So `claude-opus-5-5[1m]`, `us.anthropic.claude-opus-5-5-v1:0`, `anthropic/claude-opus-5-5`
  and dated ids are told; `claude-opus-9`, `claude-3-opus` and gateway ids are not. A host
  row whose id names no Claude model gets none even with family `anthropic`, as in Claude
  Code. The list is a copy of Claude Code's and goes stale with its catalog.
- Everything else found is either not a noeta defect (findings 1–3, 6), documented (7, 8), or
  small enough to leave for a follow-up (4, 9).

## Plan

- [x] Compare both arms' logs: calls, output, input, cache, timing, tools.
- [x] Capture Claude Code's and noeta's request bodies for an unknown model offline.
- [x] Measure per-step harness overhead offline.
- [x] Gate the budget line and notes on the model (`noeta/plugins/environment.py`), with a test.
- [x] A/B the budget lines on the live gateway.
- [x] `make check`, `make parity`.

## Acceptance criteria

- For a task whose model does not change, a model Claude Code does not know gets no
  `<total_tokens>` text in any request; a Claude model's requests are byte-identical to before
  (`make parity`: 0 mismatches).
- A model switch mid-task is not reconciled: notes already in the history stay after a switch
  to a non-Claude model, and after a switch to a Claude model the first count may start from
  an older turn's base. Claude Code has the same shape (its notes are history too); not
  handled.
- `make check` green; `make eval` shows no regression.

## Follow-ups

- Run read-only `Bash` commands of one reply together, as Claude Code does (needs a per-input
  read-only flag on tools; the command classifier lives in the permissions plugin). ~1% of time.
- `-p`: no `AskUserQuestion`, no title side request; `per-turn-control` beta only for models with
  per-turn effort.
- For the benchmark owner: Claude Code's arm can fetch upstream fixes (finding 7) and loads the
  project's skills and `CLAUDE.md` while noeta's does not (finding 8); either disable web tools
  and `noeta trust` the task directory, or report those tasks separately. More samples on
  `alwaysday1_max` before reading anything into the output ratio.

## Risks

- A Claude model reached under a gateway id that names no Claude model no longer sees the
  budget; Claude Code behaves the same way.

## Progress log

2026-10-30: findings from rb-ds41 / rb-ad1max; budget gating implemented; check green
(2579 passed, 88.75%); parity 0 mismatches; live A/B 20 vs 20 calls, no effect on output.
2026-10-30: `make eval` (DeepSeek V4.1, 3 trials): 0.948 vs baseline 0.953, no regression.
2026-10-30: review fixes: the prompt line is stripped wherever it sits; `gets_budget` matches Claude Code's id recognition (captured); capture summary committed.
