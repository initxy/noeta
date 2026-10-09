# An absolute auto-compact window, and a note gate that reads localised headings — 2026-10-09

Status: ACTIVE (implementation in progress on branch
`auto-compact-window-and-localised-notes`; base `5022d01`, v0.6.32).

## Problem

Measured on a production host (Aisthon) over 14 days, with the worker tier
bound to a 1M-token relay model:

1. **Neither compaction layer ever fires.** Both the summarize trigger and the
   composer's two tool-output valves derive from the catalog window alone:
   `available = context_window − max_output_tokens − buffer` (≈ 966K on a 1M
   model), the microcompaction valve at half of it (≈ 483K). A worker's job
   ends at 100–300K tokens, so for the whole life of every job the request
   re-sends every tool output and every tool call it ever made. The host had
   wrapped the provider in its own fold to get an earlier trim — a workaround
   for a knob the SDK does not expose (`docs/adr/context-compaction.md`:
   "No compaction knob is exposed on `HostConfig` today").
2. **When compaction did fire, the note gate refused it.** Two production
   summaries from a relay-served model localised the nine section titles
   (`# 会话持久笔记 / ## 1. 主要请求与意图 / …`, 10 009 and 13 165 characters)
   and `looks_like_summary_note` — which matches the prompt's English titles —
   recorded `compaction_summary_failed: summarize response is not a note`.
   The failure is non-retryable, so the task died on a note that was, in
   fact, a note.

Claude Code solves the first with an absolute setting, `autoCompactWindow`
(a token count, capped at the model's window), not a fraction of the window.

## Goal

- A host can say "compact as if the window were N tokens": every derived
  compaction number (available window, protected tail, microcompaction
  threshold) follows `min(model window, N)`, while everything that is about
  the *model* (its output ceiling on the wire, the 1 % skill-roster and
  memory-index budgets) keeps using the real window — the same split Claude
  Code draws.
- A summarize response whose section headings are translated is recorded as
  a note; a narration still is not. The prompt also asks for the titles
  verbatim, so the English path stays the common one.

## Scope

### `noeta-sdk`

- `HostConfig.auto_compact_window: Optional[int] = None` — a positive int or
  `None` (the model's window, today's behaviour). Validated in
  `__post_init__` like the other positive-int knobs.
- `derive_compaction_config(model, *, auto_compact_window=None)` (catalog,
  and the loader-resolved wrapper in `client.parts`): with a cap,
  `context_window = min(spec window, cap)`; `max_output_tokens` is unchanged
  (it still rides `LLMRequest.max_tokens`). A cap at or above the window is a
  no-op, so live and resume derive identical knobs from the same inputs.
- `SdkHost.auto_compact_window` (wiring-only, like `compaction_model`),
  threaded from `HostConfig` by the Client and passed to the engine build's
  `compaction=`. The skill-roster and memory-index budgets (`skill_menu_budget_tokens`,
  `memory_index_budget_tokens`) keep calling without the cap.
- `looks_like_summary_note` also counts **localised section headings**: a
  markdown heading (`#` to `######`) or a bold run (`**`) that opens with a
  section number 1–9 and a separator (`.` `)` `、` `．` `:` `：`). Distinct
  numbers count, the same `_SUMMARY_MIN_SECTIONS` threshold applies, and the
  two counts are taken separately (an English-titled note and a localised one
  each pass on their own). A plain numbered line without an English title
  (`1. 主要请求`) still does not count — it is what a narration's plan looks
  like, and the gate fails closed.
- `summarize.md` gains one sentence: keep the nine section titles exactly as
  written, in English, whatever language the conversation is in; bodies may
  use the conversation's language.

### Non-goals

- No change to *what* compaction does (summary shape, tail fraction, valve
  fraction, marker) — only to the window it is measured against.
- No new `Options` field: the window is deployment wiring, not agent identity.
- No change to the skill-roster budget: it is already 1 % of the model
  window with a usage-ranked degrade (`2026-09-14-skill-menu-budget.md`); the
  host that reported the problem was overriding it.

## Key decisions

- **An absolute cap, not a fraction.** Claude Code's `autoCompactWindow` is a
  token count; a fraction of a 1M window would still be hundreds of thousands
  of tokens. The cap is applied to `context_window` before every derivation
  so the three derived numbers stay in the same proportion they have today.
- **The cap lives on `HostConfig`.** It is the same kind of thing as
  `tool_output_inline_limit`: a deployment choice about how much context to
  carry, not part of what the agent is. It changes the composed bytes, so a
  resumed task must be given the same value — the same rule the inline limit
  already states.
- **The 1 % budgets stay on the model window.** Claude Code sizes its skill
  listing from the model's context window, not from `autoCompactWindow`; a
  host that wants a smaller roster overrides `plugin_config["skills"]`.
- **The gate widens structurally, not by a translation table.** Matching a
  Chinese title list would fail the next language; a numbered heading is the
  shape the prompt asks for in any language. Requiring heading or bold
  decoration keeps a plain numbered plan — the narration failure the gate
  exists for — on the refused side.

## Acceptance criteria

- `derive_compaction_config("opus", auto_compact_window=100_000)` derives
  `context_window == 100_000`, `tail_token_budget == (100_000 − max_output −
  buffer) // 3`, and the same `max_output_tokens` as without the cap; a cap at
  or above the window equals the uncapped config; a non-positive or non-int
  cap raises `ValueError`.
- `HostConfig(auto_compact_window=0)` raises; `SdkHost(auto_compact_window=N)`
  builds an engine whose composer `_available_window` is derived from `N`,
  while `_plugin_config()["skills"]["menu_budget_tokens"]` is unchanged.
- `looks_like_summary_note` accepts the two production localised notes
  (markdown `## 1.` … headings), a bold-numbered note, and still refuses the
  narration fixture, a single title in passing, a plain numbered plan, and
  the empty string. `summarize.md` carries the verbatim-titles sentence.
- Docs: `docs/reference/options.md` (+ zh) list the knob;
  `docs/how-it-works/context.md` (+ zh) say the window can be capped;
  `docs/adr/context-compaction.md` amended; `CHANGELOG.md` Unreleased.
- `make check` green (ruff, pytest with coverage ≥ 85, `mypy --strict` on
  both packages, naming and import lints).

## Progress log

- 2026-10-09: spec written; branch created.
- 2026-10-09: implemented on the branch. `make check` green: ruff, 4629 passed /
  155 skipped, coverage 89 %, `mypy --strict` on both packages, naming and
  import lints. A cap under the model's output ceiling plus the buffer is
  refused at derivation (it would compact on every step) — found by the first
  test, which capped Opus 5 (128K output ceiling) at 100K. Release pending.
