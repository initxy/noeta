# ADR-048: Short summaries first, a token cap per summary, a default usage rank

- Status: accepted
- Date: 2026-09-14

## Context

Refines [ADR-013](013-model-driven-skill-invocation.md) (Model-driven skill invocation loads two-stage on demand: the menu in the tool schema, the body in the semi-stable segment) and [ADR-047](047-skill-menu-budget-and-keep-order.md) (The menu has a budget and a keep order). Where they disagree, this record wins.

The budget of [ADR-047](047-skill-menu-budget-and-keep-order.md) degraded straight from a full summary to a bare
name. On a real roster that loses most of the menu: the 67 skills of one
`~/.claude/skills` directory cost ~9,600 estimated tokens against the 2000 of
a 200k window, so 50 were listed by name only — and since no host wired
`skill_menu_rank_resolver`, the survivors were chosen by tier and then by
name, which kept an alphabetically early pack and hid the skills in daily use.
The per-summary cap was also in characters, so a Chinese summary at the cap
cost four times an English one.

## Decision

- **The degrade has a short step, and breadth comes before depth.** Over
  budget, after every name is charged, every skill first gets its short
  summary — the summary's first sentence, at most 24 estimated tokens,
  clipped with `…` — in keep order while it fits. Only when no skill went
  name-only does the leftover restore full summaries, again in keep order. A
  short summary is what lets the model judge a skill and a bare name mostly is
  not, so every skill gets something before any gets everything, and a full
  summary never sits beside a bare name. Under budget the roster bytes are
  unchanged. This supersedes "Degrade is name-only, never absent" in
  [ADR-047](047-skill-menu-budget-and-keep-order.md); "never absent" stands.
- **The per-summary cap is 384 estimated tokens**, measured with the same
  CJK-aware estimate as the budget: Claude Code's 1,536 characters for an
  ASCII summary, 384 characters for a Chinese one.
- **A host that ranks nothing ranks by usage in its own store.** With no
  resolver, no static `menu_rank` and `HostConfig.skill_usage_ranking` on,
  `SdkHost` folds skill usage from the most recently updated task streams of
  the whole store (at most 200, refolded at most every 10 minutes) with the
  shipped fold and decay score, under the same once-per-task memo. The fold
  runs on a background thread and a build reads the last snapshot: on a
  store of many gigabytes one fold took 2–5 s on the host's main loop, and a
  rank minutes old orders a menu as well as a fresh one. The fold reads only
  the two event types it counts, and over sqlite on a read-only connection
  of its own, so it never holds the lock the adapter's writers share. The fold
  is store-wide, which is the single-tenant stance a host without tenancy
  seams already takes for memory and MCP; it stays off when
  `memory_root_resolver` or `mcp_scope_resolver` is bound, so "the host picks
  which streams belong to a tenant" still holds wherever tenants exist. It is
  best effort across processes: a task resumed in another process may compose
  a different roster once.

## Alternatives considered

1. **A live usage index fed by an event subscriber.** Exact and O(1) per
   event, but it needs a dedupe between the bootstrap scan and live appends,
   and still misses other replicas' writes. Rejected for the complexity.
2. **Ranking from the current task's own history.** Tenant-safe, but empty on
   the first turn, when the roster matters most. Rejected.
3. **A uniform clip that fits every summary at one equal length.** Uses the
   whole budget, but makes rank nearly irrelevant and moves every summary's
   bytes whenever one skill is added. Rejected.
4. **A 256-token per-summary cap.** Keeps ASCII truncation byte-identical but
   clips the trigger phrases many Chinese summaries end with. Rejected.

## Consequences

- `noeta.builtins.skills.impl.control_tool` owns `short_summary`,
  `MENU_SHORT_SUMMARY_MAX_TOKENS` and `MENU_DESCRIPTION_MAX_TOKENS`;
  `fit_menu_to_budget` returns the summary each name shows.
- `noeta.client.skill_usage.SkillUsageRanker` is the host's default rank
  source (internal, not exported from `noeta.sdk`); `SdkHost` builds it only
  when the rules above allow.
