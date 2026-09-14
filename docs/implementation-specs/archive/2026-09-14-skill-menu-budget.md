# Skill menu budget, rank, and usage scoring

Status: SHIPPED 2026-09-14 (uncommitted at archive time; `make check` green 3943/86.71%). Distilled into `docs/adr/model-driven-skill-invocation.md` (amendment) and `CONTEXT.md`.
Owner: initxy

## Goal

The `skill` control tool's roster (every model-invocable skill's name plus
one-line summary, rendered into the tool schema and so into the stable prefix
of every turn) has a per-skill cap but no total budget. A workspace with a
hundred skills pays for a hundred summaries on every call. Borrow the part of
Claude Code's answer that fits Noeta's constraints (byte-stable composition,
tenancy-agnostic kernel, ledger as the source of truth) and leave the rest.

## Scope

In:

1. **A total menu budget in estimated tokens.** The host derives it from the
   model's context window (1 %, via the catalog); the pack falls back to a
   fixed default when the host passes nothing. An operator can override the
   absolute number through `plugin_config["skills"]["menu_budget_tokens"]`.
   The estimate is CJK-aware (a Han/Kana/Hangul character counts as one
   token, everything else as four characters per token) because the
   kernel's `chars/4` heuristic undercounts Chinese summaries by 3–4×.
2. **Name-only degrade.** Over budget, the lowest-ranked skills keep their
   name in the `enum` and roster but lose their summary. Names are never
   dropped: reachability is preserved, and activation renders the summary
   together with the body.
3. **Keep order** = host rank score (desc) > tier (workspace-local first) >
   frontmatter `priority` (asc) > name. The registry learns which tier each
   name came from at merge time.
4. **An operator warning** when the roster exceeds the budget, naming how
   many skills went name-only and which knob to turn.
5. **Per-task rank from the host**: `HostConfig.skill_menu_rank_resolver`
   (`task_id -> {skill: score} | None`), mirroring `memory_root_resolver`
   (cheap, total, deterministic per task id). It reaches the pack as
   `plugin_config["skills"]["menu_rank"]`, and the engine cache is
   partitioned by it so tenants never share a roster.
6. **A pure usage helper for hosts** (`noeta.sdk`): fold a tenant's event
   streams into per-skill `(count, last_used_at)` — counting only
   activations recorded after the task's first `ContextPlanComposed`, so
   host preloads do not count — and score them with Claude Code's decay
   (`count × max(0.5^(days/7), 0.1)`). Noeta records nothing new: the
   `TaskStatePatched(activate_skills=…)` events already carry the signal.

Out: model-driven or host-driven deactivation (owner decision 2026-09-14);
a kernel-side usage counter; any change to how activated bodies render.

## Key decisions

- The roster is computed once per session build and stays byte-stable for
  the task's life; rank is an input fixed at build time, never re-read
  mid-task.
- The budget algorithm is Claude Code's greedy fit: names-only baseline
  first, then summaries in keep order while they fit (a later, shorter
  summary may still fit after a longer one was skipped).
- Tier rank lives on `SkillRegistry` (`tier_of`), assigned by
  `merge_skill_registries` (overlay = base's highest tier + 1), so
  `load_workspace_skills` needs no change and synthetic registries default
  to tier 0.
- The host derives `menu_budget_tokens` because the skills built-in must
  not import the providers built-in (same layering reason as
  `web.digest_model`).

## Acceptance criteria

- Under budget: roster bytes unchanged from today.
- Over budget: lowest-ranked summaries drop first; every name stays in the
  `enum`; a warning is logged once per build; output is deterministic.
- Rank map beats tier; tier beats `priority`; `priority` beats name.
- A Chinese summary of N characters costs about N tokens in the estimate.
- `skill_menu_rank_resolver` reaches the schema and partitions the engine
  cache; a bare `HostConfig()` builds byte-identical schemas to today.
- Usage helper: preload activations excluded, model activations counted,
  decay floor honoured.
- `make check` green; docs (ADR amendment, plugin-surfaces en/zh,
  sdk-options en/zh, glossary en/zh, CONTEXT, CHANGELOG) updated.
