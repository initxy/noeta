# ADR-047: The menu has a budget and a keep order

- Status: accepted
- Date: 2026-09-14

## Context

Refines [ADR-013](013-model-driven-skill-invocation.md) (Model-driven skill invocation loads two-stage on demand: the menu in the tool schema, the body in the semi-stable segment). Where the two disagree, this record wins.

The per-skill cap (1024 characters per summary) bounded one entry; nothing
bounded the roster. A workspace that indexes a hundred skills — borrowed
ecosystems, plugin packs, a tenant's own — paid for a hundred summaries in the
stable prefix of every turn. Claude Code's listing has a total budget (1 % of
the context window), a per-skill cap, protected entries, and a usage-ranked
degrade to name-only; the question was which of that fits Noeta's constraints
(byte-stable composition, a tenancy-agnostic kernel, the ledger as the source
of truth).

## Decision

- **A total budget in estimated tokens, fitted at session build.** The host
  derives `plugin_config["skills"]["menu_budget_tokens"]` as 1 % of the bound
  model's catalog context window, capped at 4,096 tokens since 2026-09-25 (at
  a 1M window the bare fraction admitted ~10K tokens of roster on every
  request; an operator's explicit value is not capped — the skills built-in
  must not import the providers built-in); the pack defaults to 2000 when a
  host passes nothing;
  an operator override rides `HostConfig.plugin_config`. The estimate is
  CJK-aware — a Han / Kana / Hangul character counts as one token — because
  the kernel's `chars/4` heuristic undercounts a Chinese roster three- to
  four-fold, and Chinese summaries are the common case.
- **Degrade is name-only, never absent.** Over budget, summaries are kept
  greedily in keep order while their increment fits (a later, shorter summary
  may still fit after a longer one was skipped); the rest keep their name in
  the `enum` and roster. Reachability is preserved, activation renders the
  summary with the body, and the tool description says so to the model.
  Under budget the roster bytes are unchanged. Going over logs one warning
  per build naming the knob.
- **Keep order = host rank > merge tier > frontmatter `priority` > name.**
  The rank is the host's `menu_rank` (`skill → score`), per task via
  `HostConfig.skill_menu_rank_resolver` — the same tenancy seam and contract
  as `memory_root_resolver`: cheap, total, deterministic per task id. The
  tier is the merge **scope**, stamped by `load_workspace_skills` (every
  built-in, plugin-contributed and borrowed pack shares the lowest tier; the
  global `.agents` / `.noeta` dirs and the workspace `.agents` dir sit above
  it; the workspace `.noeta` pack on top), so a workspace-local skill keeps
  its summary ahead of a borrowed one and the host's own pack never loses
  to a borrowed ecosystem merely for having been folded first. The menu
  itself stays name-sorted; rank decides only which summaries survive, so an
  under-budget roster is rank-independent.
- **Rank is an input fixed per task.** The roster sits in the tool schema,
  in the stable prefix; re-reading a mutable rank mid-task would rotate the
  prefix. The Engine is built per turn ([ADR-046](046-engine-per-turn.md)), so the
  host enforces "fixed" in code: the resolver is asked once per task and its
  first non-empty answer is memoised for the task's life in that process (a
  declining resolver is asked again on the next build), because the score
  the SDK itself ships decays with the clock and a per-call `now` would
  otherwise rotate the prefix from one build to the next. Across processes
  the resolver's own determinism is the contract, as for
  `memory_root_resolver`. A static `plugin_config["skills"]["menu_rank"]`
  and a resolver are mutually exclusive (the static override is applied last
  and would silently replace every resolved rank).
- **A skill that appears mid-process is in the roster on every task's next
  turn, with a note.** The roster is read at every per-turn build, so
  install / edit / remove need no refresh verb; the skills built-in records
  a "new skills" `turn_intake` reminder naming what joined the roster since
  the task last saw it (the roster the pack composed rides the task's local
  slot — `noeta.runtime.task_local`, via `plugin_config["skills"]["task_slot"]`
  — so it is process-local: silent on the opening turn and after a restart —
  Claude Code's "New skills discovered" behaviour). A removed skill leaves
  the enum silently.
- **The budget is the honest 1 %.** Claude Code's 1 % is a character proxy
  (`1 % × window × 4` characters); Noeta's is 1 % in estimated tokens.
  Equal for an ASCII roster, four times tighter for a CJK roster — the
  point of the CJK-aware estimate. An operator who wants CC's looser fit
  overrides `menu_budget_tokens`.
- **Usage is folded from the ledger, not counted by the kernel.** Every
  activation is already a durable `TaskStatePatched(activate_skills=…)`; a
  multi-replica server shares the event store. `noeta.sdk` ships the pure
  fold (`skill_usage_from_events`) and Claude Code's decay score
  (`rank_skills_by_usage`: `count × max(0.5^(days/7), 0.1)`); the host picks
  which streams belong to a tenant. Only activations after a task's first
  `ContextPlanComposed` count, so host preloads (`Options.skills`) never rank
  themselves first.

## Alternatives considered

1. **A kernel-side usage counter (Claude Code's `skillUsage` file).**
   Rejected: the kernel is tenancy-agnostic and multi-replica; the ledger
   already carries the signal, so a second write path would only drift.
2. **Dropping over-budget skills from the menu entirely.** Rejected: the
   `enum` is the model's only view of what exists; a name costs a few tokens
   and keeps the skill reachable.
3. **Re-fitting the roster as usage changes within a task.** Rejected: the
   roster is stable-prefix bytes; a mid-task change re-primes the cache and
   makes a resumed task compose different bytes.
4. **Truncating activated bodies at compaction (Claude Code).** Not needed:
   activated content is state-derived and re-hangs after the summary
   (`[ADR-036](036-anchored-content-placement.md)).
5. **Model- or host-driven deactivation.** Declined by the owner
   (2026-09-14); the "no deactivate" rationale of [ADR-013](013-model-driven-skill-invocation.md) stands.

## Consequences

- `noeta.builtins.skills.impl.control_tool` owns the estimate
  (`estimate_menu_tokens`), the fit (`fit_menu_to_budget`), the keep order
  (`menu_keep_order`) and the warning; `wiring.py` reads the two config keys
  and fails loudly on a malformed value.
- `SdkHost` derives `menu_budget_tokens` (`skill_menu_budget_tokens`) and
  threads the per-task rank into the pack and the cache scope.
- `noeta.client.skill_usage` is the host-facing fold, exported from
  `noeta.sdk`.

## Refined by

- [ADR-048](048-skill-menu-short-summaries-and-usage-rank.md) — Short summaries first, a token cap per summary, a default usage rank
- [ADR-062](062-skill-listing-as-recorded-content.md) — The roster leaves the tool schema and becomes a recorded listing
