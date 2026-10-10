# Skill listing: out of the tool schema, into a recorded resident

Status: IMPLEMENTED 2026-10-10 on `feat/skill-listing-resident`, uncommitted; `make check` green (4698 passed, 89.13%).
Owner: initxy
Packages: `noeta-sdk` only (the `skills` built-in and the client host). `noeta-runtime` is untouched (`enum_roster_prop` stays for `spawn_subagent`). Release: sdk-only patch.

## Goal

The `skill` tool's schema is byte-identical for every task and every turn, and
the roster of skills the model may load reaches it as recorded context that
never changes once written. Concretely:

1. The `skill` schema carries no roster: no `enum`, no "Available: …" text.
2. A task's roster is recorded once, pre-loop, as a content-channel resident and
   is never rewritten — not by an install, an edit, a removal, a re-rank, a
   restart, or a resume in another process.
3. A skill that joins the roster mid-task is announced by one appended resident
   naming only the newcomers, at the point in the conversation where it joined.
4. Compaction never loses the roster: the residents re-hang after the summary.

This is Claude Code's shape (verified against the 2.1.296 bundle, 2026-10-10):
the `Skill` tool takes a free `skill: string` with a fixed description; the
listing is a `skill_listing` attachment ("The following skills are available for
use with the Skill tool:") sent for the names not yet sent this session
(`sentSkillNames`), seeded from the transcript on resume (`resumeSeedNames`); a
removed skill is not announced. Noeta carries the listing as a resident instead
of a transcript message, so the compaction re-hang and the resume rebuild come
from the existing content channel rather than from repair code.

### Why

Today the roster is rendered into the tool schema, which sits at the head of
every request, and the Engine is rebuilt every turn
(`docs/adr/engine-per-turn.md`). So the head of the prompt — and with it every
cached token after it — changes whenever:

- a skill is installed, removed, or has its description edited mid-task;
- the roster is over budget and the per-task rank changes: after a restart or a
  memo eviction (`_MAX_SKILL_MENU_RANK_MEMO`), or when the default usage ranker
  answered empty on the first turn (not memoised) and non-empty later.

And the schema differs between tasks whenever their fitted rosters differ, so
tasks cannot share one cached system-plus-tools prefix. The ADR's premise for the
schema placement — "the skill set is … static within a session"
(`model-driven-skill-invocation.md`, Rationale; Alternative 2) — stopped being
true when the 2026-09-14 amendment made the roster re-read on every build.

## Scope

In:

1. **A fixed `skill` schema.** `skill_tool_schema()` takes no menu: the `skill`
   property is `{"type": "string", "description": "Name of the skill to
   activate, from the skill listing in your context."}`. The mount still
   self-gates on `skill_invocation` plus a non-empty model-invocable roster.
2. **A `skill_listing` content kind** in the `skills` built-in (its own
   `ContentKindSpec`, kind band 110 — after skill bodies at 100, before memory
   at 200). Each resident's body is JSON
   (`media_type="application/json"`): `{"names": [...], "lines": [...]}`, where
   `lines` are the fitted roster lines `- name: summary` (or `- name` when the
   budget left only the name), name-sorted. The renderer turns one resident into
   one user message:
   - resident `roster`: `The following skills are available via the \`skill\` tool:\n` + lines;
   - resident `added-<n>`: `New skills are available via the \`skill\` tool:\n` + lines.
   Renderer purity holds: bytes come only through `resolve()` at the recorded
   hash.
3. **The initial roster is a pre-loop, activate-once resident.** The skills
   session pack contributes an `init` hook (the memory-index precedent,
   `memory/impl/__init__.py` `_init`) that records `skill_listing:roster` with
   `refresh=False` when the agent activates `skill_invocation` and the roster is
   non-empty. It fits the roster with the existing machinery
   (`_menu_description`, `menu_keep_order`, `fit_menu_to_budget`, the host's
   `menu_budget_tokens` / `menu_rank`) — so rank and budget decide the bytes
   exactly once per task. Child tasks get it through the same
   `content_init_hooks` path the subtask drain already runs.
4. **Mid-task additions are appended residents.** `roster_note.py` is rewritten:
   the `turn_intake` provider computes `announced` = the union of `names` over
   every active `skill_listing` resident, resolved through the content store
   from `view.task_state.active_content` (the memory-index-delta precedent: the
   baseline comes from recorded state, so a fresh process computes the same
   answer). It returns one `ResidentActivation(kind="skill_listing",
   name=f"added-{k}", refresh=False)` for `current − announced`, where `k` is
   the count of `added-*` residents already active. It stays silent while no
   `roster` resident exists: the init hook also runs on every new goal
   (`driver.py` `refresh_content`, ahead of the intake prelude), so a roster
   empty at seed is recorded as `roster` by the hook on the first goal that
   finds skills. Removed skills and edited summaries are not announced. The current fitted
   roster still reaches the provider through the task-local slot
   (`ROSTER_SLOT`), now holding `(name, line)` pairs instead of a name set; the
   slot is refreshed by every build and is no longer the memory of what was
   announced. `SkillRoster.take_new` and its process-local `_announced` go away.
5. **Validation reads the live registry.** `make_skill_translate` validates
   against the current build's model-invocable names (unchanged source, no
   longer described as "the enum"). The unknown-name answer keeps its text
   (`unknown skill 'x'; available: …`). Fix the stale "the real gate is the
   menu enum" comment at `_SKILL_NAME_MAX_LEN`.
6. **Tool description** (`impl/skill.md`): reword the three "roster" sentences to
   point at the skill listing in context. Net change at most one line; report
   the measured byte delta of the description and of the composed `main`
   prefix in the CHANGELOG entry (owner rule: prompt text stays lean).
7. **Docs.** ADR amendment to `docs/adr/model-driven-skill-invocation.md`
   (dated section: reverses "The menu goes into the tool schema" and
   Alternative 2, with the reason above); touch-ups in
   `docs/adr/engine-per-turn.md` (67, 91–93, 112, 186),
   `docs/reference/plugin-surfaces.md` 199–203, `docs/reference/glossary.md` 96,
   `docs/reference/tools.md` 149, `docs/reference/options.md` 203–207, and
   their `docs/zh/` mirrors; `CONTEXT.md` (Skill entry) at ship time.

Out:

- `spawn_subagent`'s agent directory — it also uses `enum_roster_prop`, but
  its roster is fixed by the agent spec, not re-read from disk. Separate call.
- The rank sources, the budget fraction, the three-step degrade, the per-skill
  cap, and how activated bodies render — all unchanged, just applied once.
- The per-task rank memo in `SdkHost._skill_menu_rank_override`. It is no longer
  load-bearing for cache stability (the bytes are recorded once), but removing
  it would turn one resolver call per task into one per build. Keep it; reword
  its docstring and the ADR line from "prefix stability" to "one resolver call
  per task".
- Re-announcing edited summaries or removals (Claude Code does neither).

## Key decisions

- **A resident, not a recorded reminder message.** A `Reminder` is a transcript
  turn: compaction covers it and the summarizer skips host-injected turns, so
  the roster would vanish at the first compaction and need a re-emit rule (the
  memory index delta's workaround). A resident is state-derived: pre-loop it
  renders in `semi_stable`, mid-task at its anchor, and after compaction it
  re-hangs right after the summary — free, because compaction has already
  invalidated the cache (`docs/adr/anchored-content-placement.md`).
- **Activate-once, and the announced set comes from the ledger.** `refresh=False`
  makes the first recorded bytes the task's bytes for life; deriving
  `announced` from active residents rather than a process-local slot is what
  makes restart and cross-replica resume byte-stable. This replaces the per-task
  rank memo as the stability mechanism.
- **JSON body with explicit `names`.** The delta needs the announced names; parsing
  them back out of rendered prose would couple the delta to the line format.
- **One resident per batch, not per skill.** Per-skill residents would make the
  announced set a plain key lookup but cost one `ContextContentRecorded` per
  skill at every task start and render a mid-task batch as one message per
  skill.
- **Drop the `enum`, keep name validation.** The `enum` was the only reason the
  roster had to live in the schema; the translate already rejects unknown names
  with a recoverable answer that lists the valid ones. Claude Code has no
  `enum` either. Cost: a provider in strict-schema mode no longer
  hard-constrains the value, so a wrong name costs one extra round trip.

## Alternatives considered

1. **Keep the roster in the schema, freeze it per task in the host.** Stops the
   in-task drift but not the cross-task divergence, and a restart still has to
   re-derive the same bytes from a mutable registry. Rejected.
2. **Recorded `Reminder` turns (Claude Code's literal shape).** Rejected — see
   the first key decision.
3. **Per-skill residents.** Rejected — see key decisions.
4. **Re-render one roster resident with `refresh=True` on change.** A refresh of
   a pre-loop resident rewrites `semi_stable` and re-primes everything after it
   — the cost this work removes.

## Implementation notes

- `skills/impl/control_tool.py`: `skill_tool_schema()` drops the `menu`
  parameter; `_skill_menu` becomes the fitting helper that returns
  `(name, line)` pairs for the listing (lines rendered as `- name: summary`,
  not the `name — summary; …` schema form); `make_skills_control_tool` stops
  taking `menu_budget_tokens` / `menu_rank`.
- `skills/impl/listing.py` (new): kind constant, body encode/decode,
  `ContentKindSpec`, the init hook, and the shared
  `pending_batch(current_lines, active_listing, resolve)` used by both the hook
  and the provider.
- `skills/impl/wiring.py`: contribute the kind (band 110) and the init hook,
  gated on the activation; put the fitted `(name, line)` pairs into the slot.
- `client/host.py` (~2604): bind the new provider with the host's content store
  and `peek`.
- Tests to rewrite: `test_control_tool_schema_goldens.py` (`_SCHEMA_SKILL`),
  `tests/snapshots/composer_view_{main,explore}.txt`, `test_skill_tool_menu.py`
  (schema / enum / "Available:" assertions move to listing-body assertions; the
  budget/degrade unit tests stay), `test_skill_menu_rank_host.py` (parses
  "Available: "), `test_plugin_host_surfaces.py` (`enum`),
  `test_engine_per_turn.py` 104–150 (new-skills note). The `spawn_subagent`
  tests must not change.

## Acceptance criteria

1. The `skill` schema bytes are identical across two tasks with different
   rosters, and across turns of one task in which a skill is installed,
   removed, and edited. Golden pins the new schema.
2. First turn: one `ContextContentRecorded(kind="skill_listing", name="roster")`
   recorded pre-loop, rendering in `semi_stable` with the fitted lines;
   over budget, rank decides the degrade exactly as today (existing degrade
   tests pass against the listing body).
3. Install a skill mid-task → the next turn records `added-0` naming only it,
   rendered at its anchor; the `semi_stable` bytes and every earlier message's
   bytes are unchanged. Remove or edit a skill → nothing recorded, the listing
   bytes are unchanged; calling a removed skill gets `unknown skill …`.
4. Restart: a fresh `SdkHost` over the same store resumes the task with
   byte-identical composed messages, a changed usage rank notwithstanding, and
   announces only skills absent from every recorded listing.
5. Compaction covering the anchor of `added-0` → it re-hangs right after the
   summary; a pre-loop `roster` stays in `semi_stable` (never part of the
   summarised history). A `roster` recorded mid-conversation re-hangs too,
   ahead of `added-0`.
6. A task recorded before this change gets its next goal: the tool schema
   changes once (one cache miss) and the init hook records `roster` ahead of
   that goal — anchored there, since the first assistant message is already
   behind it, so earlier messages keep their bytes.
7. A child task with `skill_invocation` gets its own `roster` resident; one
   without it gets neither the tool nor the listing.
8. `make check` green (coverage ≥ 85, mypy --strict, naming lint, import
   lint); CHANGELOG reports the byte deltas from scope item 6.

## Outcome

- Shipped as specified, plus: the `skill_listing` kind is registered only for
  agents that activate `skill_invocation`, so other agents' kind order and
  bytes are unchanged.
- Known limit: the `roster` is fitted at seed, so a rank resolver that can
  answer only after `seed_start` leaves that task's roster unranked (matters
  only over budget). Documented in the ADR amendment and
  `_skill_menu_rank_override`.
- Measured (80 skills, `~/.claude/skills`): `skill` parameter 8,911 → 107
  bytes, listing message 7,375 bytes, net −1.4 KB per request at the
  2,000-token budget.
