# Skill menu: token per-skill cap, three-step degrade, default usage rank

Status: IMPLEMENTED 2026-09-14, uncommitted — owner approved the three recommended decisions (tenancy rule, 384-token cap, 24-token short summary). Distilled into the ADR amendment and CONTEXT.md; archive after commit.
Owner: initxy
Packages: `noeta-sdk` only (the `skills` built-in and the client host). Release: sdk-only patch.

## Goal

The 2026-09-14 menu budget (`docs/adr/model-driven-skill-invocation.md`,
amendment) fits the `skill` roster to 1 % of the model's window, but on a real
roster it degrades badly. Measured on the 67 skills in `~/.claude/skills` at the
2000-token budget of a 200k window: the full roster costs ~9,621 estimated
tokens and the names alone 287; 17 skills keep a summary and 50 are listed by
name only. With no host rank the survivors are picked by tier and then by name,
so the alphabetically early `bytedance-*` pack keeps its summaries while
`cuihuo`, `haohao-shuohua`, `feynman-explainer` and every `lark-*` skill show a
bare name the model cannot judge.

Three causes, three fixes, landed in this order:

1. The per-skill cap is in characters, so one Chinese summary may cost four
   times an English one (1,024 characters is ~1,024 tokens against ~256).
2. The degrade has two steps only: full summary or name only.
3. The keep order is effectively alphabetical, because no host wires
   `skill_menu_rank_resolver`.

## Scope

In:

1. **Per-skill cap in estimated tokens.** `MENU_DESCRIPTION_MAX_CHARS = 1024`
   becomes `MENU_DESCRIPTION_MAX_TOKENS = 384`, measured with
   `estimate_menu_tokens`. Truncation keeps the longest prefix whose estimate,
   marker included, fits the cap; the marker stays `… (truncated)`.
2. **Three-step degrade: full, short, name only.** Under budget nothing
   changes. Over budget, after every name is charged, `fit_menu_to_budget`
   runs two greedy passes in keep order:
   - *Breadth*: each skill gets its short summary while the increment fits;
     a skill whose short summary does not fit is listed by name only.
   - *Depth*: only when no skill is name-only, each skill is upgraded from its
     short summary to its full summary while the increment fits.

   The short summary is the summary's first sentence (ending at `.`, `!` or
   `?` followed by whitespace, or at `。`, `！`, `？`, `；`), clipped to
   `MENU_SHORT_SUMMARY_MAX_TOKENS = 24` with a trailing `…` when longer. A
   summary within 24 tokens is its own short form. `fit_menu_to_budget`
   returns the roster summary per name (full, short or empty) instead of the
   set of dropped names, and the over-budget warning reports all three counts.
3. **Default usage rank in `SdkHost`.** When the host passes no
   `skill_menu_rank_resolver` and no static
   `plugin_config["skills"]["menu_rank"]`, the host ranks from the ledger
   itself: it folds the most recently updated task streams (from
   `list_task_streams()`, at most 200) with `skill_usage_from_events` and
   scores them with `rank_skills_by_usage`. The fold is a process-local
   snapshot, refreshed on demand at most once every 10 minutes; each task still
   freezes its first non-empty rank through the existing per-task memo. A new
   `HostConfig.skill_usage_ranking: bool = True` turns the default off.

Out:

- The frontmatter parser keeps the quotes of a quoted `description:` scalar
  (44 of the 67 measured summaries start with a literal `"`), and the quote
  leaks into short summaries too. That is a separate fix.
- The budget fraction (1 %), the keep-order keys, the `skill` tool description
  (it already says a shortened roster line still loads everything), and how
  activated bodies render.
- A kernel-side usage counter, or a live index fed by an event subscriber (see
  Alternatives).

## Key decisions

- **Breadth before depth.** A short summary is what lets the model judge a
  skill; a bare name mostly does not. Every skill gets something before any
  skill gets everything. Rank then decides who gets the full text and, when
  even short summaries overflow, who goes name-only. Measured on the 67-skill
  roster (no rank, 384-token cap): at 2000 tokens, 7 full / 60 short / 0
  name-only (today 17 / 0 / 50); at 3000 tokens, 17 / 50 / 0.
- **Depth only after full breadth.** A leftover too small for a skipped short
  summary could still pay for another skill's upgrade; allowing it would put a
  full summary beside a bare name. The rare leftover is left unused.
- **Short summary = first sentence, at most 24 tokens.** Authors put the "what"
  first. On the measured roster a 20-token cap fits with 2 tokens to spare and a
  32-token cap pushes one skill to name-only; 24 sits between. Deterministic,
  no model call.
- **A 384-token per-skill cap, not 256.** 256 would keep ASCII truncation
  byte-identical to today but clip 9 of the 67 measured summaries, among them
  `cuihuo`, `feynman-explainer` and `tangshan-style`, whose trigger phrases sit
  at the end. 384 clips 3 (`lark-apps`, `vdd`, `video-reader`) and equals
  Claude Code's 1,536-character cap for an ASCII summary. Once the degrade has a
  short step, the cap no longer decides who starves over budget; it bounds one
  entry when the roster fits.
- **The default rank follows the single-tenant precedent.** A host that binds no
  tenancy seam already shares one memory root and one MCP scope across all
  tasks; folding usage across the whole store is the same stance. The default
  stays off when the host binds `memory_root_resolver` or `mcp_scope_resolver`:
  that host is multi-tenant and must say which streams belong together through
  `skill_menu_rank_resolver`, and the host logs so once. The ADR rule "the host
  picks which streams belong to a tenant" stays intact.
- **A bounded snapshot, not a live index.** Reading at most 200 streams at most
  once every 10 minutes per process bounds the cost with no subscriber, no
  scan/live dedupe and no per-task state. Against a 7-day half-life, a
  10-minute lag does not matter.
- **Best effort across processes.** Two processes (a restart, another replica)
  may fold different snapshots, so a resumed task may compose a different
  roster once: one prompt-cache miss, no effect on correctness. A host that
  needs byte stability across processes passes its own deterministic resolver,
  as today.

## Alternatives considered

1. **A live usage index fed by an event subscriber** (bootstrap scan plus
   replay). Exact and O(1) per event, but it needs a dedupe between the scan
   and live events and still misses other replicas' appends. Rejected for the
   complexity.
2. **Ranking from the current task's own history only.** Tenant-safe, but empty
   on the first turn, exactly when the roster matters, and the per-task memo
   freezes it after the first activation. Rejected: close to no value.
3. **A uniform water-filling clip** (the largest equal cap that fits every
   skill). Uses the budget fully, but makes rank nearly irrelevant and moves
   every summary's bytes whenever one skill is added. Rejected.
4. **A 256-token per-skill cap.** See Key decisions.

## Implementation notes

- `noeta/builtins/skills/impl/control_tool.py`: the token cap in
  `_menu_description`; a `short_summary` helper; `fit_menu_to_budget` with two
  passes and the new return type; `_skill_menu` renders per-name summaries and
  the three-count warning.
- `noeta/client/skill_usage.py`: an internal `SkillUsageRanker` (event log,
  clock, stream cap, TTL, a lock-guarded snapshot), not exported from
  `noeta.sdk`.
- `noeta/client/host.py`: `_skill_menu_rank_override` falls back to the ranker
  under the rules above; `HostConfig` and `client.py` forward
  `skill_usage_ranking` to `SdkHost`.
- Each numbered item lands with its own tests before the next starts.

## Acceptance criteria

1. Per-skill cap: a 1,024-character Chinese summary renders within 384
   estimated tokens and ends in `… (truncated)`; an ASCII summary of at most
   1,536 characters renders unchanged, and a longer one is clipped.
2. Under budget, the roster bytes are identical to today's except where
   criterion 1 applies.
3. Over budget: every name stays in the `enum`; a skill holds its full summary
   only when no skill is name-only; both passes follow the keep order; the same
   registry and rank give the same bytes; the warning reports the full, short
   and name-only counts.
4. Short summary: the first sentence when it fits 24 tokens, otherwise clipped
   and ending in `…`; ASCII and CJK sentence ends both split; a summary within
   24 tokens is unchanged.
5. A synthetic 67-skill fixture with the measured cost profile (median ~120
   tokens per summary) at a 2000-token budget lists every skill with at least
   a short summary.
6. Default rank: with a bare `HostConfig()` on a SQLite store where earlier
   tasks activated `zeta` through the `skill` tool, a new task's over-budget
   roster upgrades `zeta` to its full summary ahead of alphabetically earlier
   skills; a host preload (`Options.skills`) counts for nothing; the rank is
   memoised per task.
7. The default rank is off when `skill_usage_ranking=False`, when a static
   `menu_rank` is set, when `skill_menu_rank_resolver` is set (the resolver
   wins), and when `memory_root_resolver` or `mcp_scope_resolver` is bound
   (logged once).
8. Cost bound: within one TTL the ranker reads at most 200 streams, once,
   however many tasks build (asserted with a counting event log). The first
   snapshot is timed on a real local store and the number recorded in the
   handoff.
9. `make check` green. Docs: a new dated ADR amendment superseding "Degrade is
   name-only, never absent"; the skill paragraph in `CONTEXT.md`;
   `docs/reference/plugin-surfaces.md` and its zh mirror;
   `docs/reference/sdk-options.md` and its zh mirror; `CHANGELOG.md`
   `[Unreleased]`, noting that the `skill` schema bytes change for over-budget
   rosters and oversized summaries, so the first turn after upgrading misses
   the prompt cache once.
