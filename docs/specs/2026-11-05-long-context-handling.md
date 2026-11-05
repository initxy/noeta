---
status: active
created: 2026-11-05
branch: v1-context
---

# Long-context handling: the rest of Claude Code's set

## Goal

Close the gaps between noeta and Claude Code 2.1.318 in how a long task stays inside its
context window, each in the module it belongs to, with the cache-cost trade-offs Claude Code
makes:

1. A provider error "input length and `max_tokens` exceed context limit" retries once with a
   smaller output cap instead of compacting the whole conversation.
2. A sub-agent's final result handed to its parent is bounded (100,000 chars; longer is saved
   to a file with a preview, as tool results are).
3. Precomputed compaction (default on): from ~80% of the effective window, a summary of the
   older rounds is written in the background; at the threshold it is swapped in, the rounds
   since kept verbatim.
4. Clearing old tool results once the prompt cache is cold (default off): after a long idle
   gap, older results of `clearable` tools are replaced by a placeholder, the newest kept.
5. A one-hour prompt cache for Anthropic (default off).
6. Idle compaction (default off; only with the one-hour cache): compact while idle before the
   cache expires.

## Non-goals

- Claude Code's experimental server-side clearing (`context_hint`, `clear_tool_uses` edits),
  sleep compaction, partial compaction from a chosen message, `/context`.
- Changing the existing auto-compaction threshold, summary prompt or re-attachments.

## Context

Evidence from the 2.1.318 binary (strings and byte offsets) is in the session notes; the
facts that drive the design:

- Max-tokens overflow: parse input tokens and the context limit, set the output cap to
  `limit - input - 1000` (floor 3,000), retry (`tengu_max_tokens_context_overflow_adjustment`).
  noeta today treats this error as `ContextOverflow` (`noeta/backends/providers/_common.py`)
  and compacts everything.
- Sub-agent result: Claude Code declares a 100,000-char cap on its agent tool's result.
  noeta puts the final answer into the parent's `<task-notification>` unbounded
  (`noeta/plugins/subtasks/extension.py`).
- Precomputed compaction (`precomputeCompactionEnabled`, default true): arms at
  `min(effective - round(effective * 0.2), threshold)`; runs the "summarize older rounds, keep
  newer rounds" compactor in the background, PreCompact hook included; at the threshold or a
  prompt-too-long the ready summary plus the messages since are swapped in; a summary that no
  longer fits the conversation is dropped; at most 3 failures.
- Clearing old results: placeholder `[Old tool result content cleared]`; clearable tools Read,
  Bash, Grep, Glob, WebSearch, WebFetch, Edit, Write (noeta: the `clearable` attribute, today
  read by nothing, `noeta/core/tool.py`); keep the newest 12; only when it saves at least
  20,000 tokens; only after an idle gap longer than the cache life (3,900 s for the 1 h cache),
  when clearing costs no cache.
- Idle compaction (`idleCompaction`): only with a 1 h cache TTL; fires at 90% of the cache
  life after the last request; needs at least max(100,000, 200,000) tokens of context;
  dropped if more than 60 s late or a newer request went out.
- noeta's Anthropic requests use the default 5-minute `ephemeral` cache
  (`noeta/backends/providers/_anthropic_wire.py`), though the `extended-cache-ttl` beta is sent.

## Decisions

- Placement: (1) in the kernel's model-error recovery / provider error classification, not
  compaction (it is a re-send, not a trim); (2) in the subtasks plugin, reusing
  `noeta/core/large_output.py`; (3), (4), (6) are options of the compaction plugin, which
  already owns the size measurement and the "when to slim the conversation" decision — not
  separate plugins that would re-measure and race; (5) an Anthropic provider option.
- Everything a step sends stays a pure function of the folded log: a precomputed summary, a
  clearing and an idle compaction are each recorded events, so a restarted runner sends the
  same bytes.
- Defaults: (3) on, (4) (5) (6) off. (6) does nothing unless the requests use the 1 h cache.
- Constants follow Claude Code (above); option names follow the plugin's existing
  `auto_compact_*` style.

## Plan

- [x] Max-tokens overflow retries with a smaller output cap (kernel / providers).
- [x] Sub-agent result bound (subtasks).
- [x] One-hour cache option (Anthropic provider).
- [ ] Precomputed compaction (compaction).
- [ ] Clearing old results after an idle gap (compaction).
- [ ] Idle compaction (compaction).
- [ ] Docs: CONTEXT.md / plugin docs / ADR if a costly-to-reverse decision lands.
- [ ] Review per docs/reviewing.md; gates; archive.

## Acceptance criteria

- Each item has tests through its public surface (plugin options, provider requests, runner
  behavior), including restart mid-way for (3), (4), (6).
- (1): a scripted overflow error with token counts yields one retry with the computed cap and
  no compaction; an unparsable one falls back to today's behavior.
- (3): at the threshold with a ready summary, no summarize call is made in that step; a stale
  summary is discarded; with the option off, behavior is unchanged.
- (4)/(6) off by default: requests are byte-identical to today's (`make parity` 0 mismatches).
- Gates: `make check`, `make frontends`, `make parity`, `make cluster`, `make e2e`, `make eval`
  without regression.

## Risks

- Precomputed compaction adds a model call per long task, wasted when the task ends before
  the threshold.
- A background request in a single-writer runner: the result must land as an ordinary event,
  never mutate state out of band.
- A cleared result the model still needed costs a repeat tool call.

## Progress log

- 2026-11-05: spec written; owner approved the recommended set (3 on, 4/5/6 off).
- 2026-11-05: (1) done in `core/retry.py` (`complete_with_retries`, so a plugin's model calls
  get it too): providers put the parsed numbers on `ContextOverflow` (`input_tokens`,
  `context_limit`; Anthropic's form plus OpenAI's and vLLM's two that state the input), and the
  call is made again once with the lowered cap, logged as an ordinary `retry` record. Nothing
  else is recorded: like Claude Code's `maxTokensOverride` the cap lives for that one call, and
  a restarted runner re-sends the request the log builds, meets the same error and retries the
  same way.
- 2026-11-05: (2) done: the completion notice's `<result>` and the hand-back report are bound
  at 100,000 chars (`RESULT_LIMIT_CHARS`), saved in the spill dir with a preview, start and end
  without one. Found and closed one more path: a parked tool's resumed result (a tool that waits
  on its children) skipped `bound_result`; `resume_calls` now applies it.
- 2026-11-05: (5) done: `prompt_cache_ttl = "5m" | "1h"` on the Anthropic provider and in
  `config.toml` (Claude Code's `promptCacheTtl` values; it sends `{"type": "ephemeral",
  "ttl": "1h"}`, checked in the 2.1.318 binary; the 2.1.312 recordings are all API-key 5-minute
  requests). Plugins read it from the log, not the provider: the reply says
  `ModelResponse.cache_ttl` (3600; `None` = the provider's default), folded into
  `TaskState.cache_ttl` (the newest reply's). Default off sends the same bytes (`make parity` 0).
