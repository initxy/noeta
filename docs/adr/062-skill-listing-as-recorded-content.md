# ADR-062: The roster leaves the tool schema and becomes a recorded listing

- Status: accepted
- Date: 2026-10-10

## Context

Refines [ADR-013](013-model-driven-skill-invocation.md) (Model-driven skill invocation loads two-stage on demand: the menu in the tool schema, the body in the semi-stable segment) and [ADR-047](047-skill-menu-budget-and-keep-order.md) (The menu has a budget and a keep order). Where they disagree, this record wins.

"The menu goes into the tool schema" rested on the skill set being static
within a session ([ADR-013](013-model-driven-skill-invocation.md), Rationale; its Alternative 2 rejected the message
stream on that premise). [ADR-047](047-skill-menu-budget-and-keep-order.md) made the roster re-read at every
per-turn build, so the premise no longer held: an install, a removal or an
edited summary rewrote the `skill` schema — the head of every request — and
with it every cached token after it. Over budget, the rank changed the bytes
too: after a restart, a memo eviction, or a default ranker that answered empty
on the first turn and non-empty later. And two tasks with different fitted
rosters could never share one cached system-plus-tools prefix.

## Decision

- **The `skill` schema is fixed.** One `skill: string` parameter with no
  `enum` and no roster; the description points at the skill listing. The
  bytes are the same for every task and turn. The translate still validates
  the name, against the current build's model-invocable names, and an unknown
  name keeps its recoverable answer listing the valid ones. This supersedes
  "The menu goes into the tool schema" and the `enum` clause of "Tool shape" in
  [ADR-013](013-model-driven-skill-invocation.md);
  Claude Code's `Skill` tool has the same shape.
- **The roster is a recorded, activate-once resident.** The skills pack's
  `init` hook records `skill_listing:roster` — the fitted lines plus their
  names — with `refresh=False`, for an agent that activates
  `skill_invocation` and has a non-empty roster. Pre-loop it renders in
  `semi_stable`; rank and budget shape a task's bytes exactly once.
- **Additions are appended residents; nothing else is announced.** A
  `turn_intake` provider records `skill_listing:added-<k>` for the skills no
  recorded listing names yet, anchored at that goal. Removed skills and edited
  summaries are not announced (Claude Code does neither); calling a removed
  skill gets the "unknown skill" answer.
- **What was announced is read from the ledger.** The provider resolves the
  `names` of every active `skill_listing` resident through the content store,
  so a restart or another replica announces exactly what this process would.
  The process-local "announced roster" of [ADR-047](047-skill-menu-budget-and-keep-order.md) is gone; the
  task-local slot now only carries the current build's fitted roster.

## Alternatives considered

1. **Keep the roster in the schema, frozen per task by the host.** Stops the
   in-task drift but not the cross-task divergence, and a restart still has
   to re-derive the same bytes from a mutable registry. Rejected.
2. **Recorded reminder turns (Claude Code's literal attachment).** A
   transcript turn is covered by compaction and the summarizer skips
   host-injected turns, so the roster would vanish at the first compaction and
   need a re-emit rule. A resident re-hangs after the summary for free
   (`[ADR-036](036-anchored-content-placement.md)). Rejected.
3. **One resident per skill.** Makes the announced set a key lookup, but costs
   one `ContextContentRecorded` per skill at every task start and renders a
   mid-task batch as one message per skill. Rejected.
4. **Refreshing one roster resident on change.** A refresh of a pre-loop
   resident rewrites `semi_stable` and re-primes everything after it — the
   cost this record removes. Rejected.

## Consequences

- `noeta.builtins.skills.impl.listing` owns the kind (`skill_listing`, kind
  band 110), the JSON body, the renderer, the init hook and the provider;
  `control_tool.listing_lines` fits the roster.
- Without the `enum`, a provider in strict-schema mode no longer constrains
  the value; a wrong name costs one round trip.
- The per-task rank memo in `SdkHost` stays, now only to keep the resolver to
  one call per task. The `roster` is fitted at seed, so a resolver that can
  answer only after `seed_start` leaves that task's roster unranked (it
  matters only over budget); a host binds its tenant before seeding.
- A task recorded before this record took effect changes its tool schema once and
  records `roster` at its next goal, anchored there; earlier messages keep
  their bytes.
