# Skills subsystem moves into the `skills` built-in (microkernel phase 2a)

> **Status: Shipped** — landed in the phase-2a commit (this session, after
> `0af7762`); the durable decisions live in the
> [plugin-contribution-bundles.md](../../adr/plugin-contribution-bundles.md)
> microkernel addendum (the skills built-in now carries impl like the other
> eleven).

## Goal

The skills *material* — the SKILL.md indexer, the three-layer merge, the
skill-script tool, the registry-consuming wiring — moves into
`noeta/builtins/skills/impl/` (the noeta-sdk wheel), following the proven M3
memory pattern: the kernel keeps typed seams and the recording path; the SDK
injects a loader-resolved factory; every composed byte stays identical.

Deferred from the phase-1 microkernel migration
(`archive/2026-07-29-microkernel-capability-migration.md`, D3) because skills
are a content tenant of the **locked composer**. The entanglement audit
(2026-07-29, this session) found the coupling is narrower than feared: the
composer has **no static import** of `noeta.context.skills` — it exposes the
`SkillRenderer` / `RenderedSkills` seam types and the indexer imports *them*
(downward edge, kernel-safe). The tenant mechanism (ContentKindSpec, the
`skill` content channel) is already an open registry surface.

## Decisions (2026-07-29, pattern-derived from phase 1)

- **S-D1 — movers.** `noeta.context.skills` (SkillIndexer / SkillRegistry /
  SkillDescription / `build_skill_renderer` + `_frontmatter`),
  `noeta.tools.skill_script` (`RunSkillScriptTool` + helpers), and
  `noeta.policies.skill_tools` (`resolve_skill_allowed_tools` — skill-policy
  glue, assigned to THIS mover, not the react one) → `noeta.builtins.skills.impl.*`.
  The disk/registry-touching halves of `noeta.execution.skills`
  (`load_workspace_skills`, `merge_skill_registries`, script/root resolution,
  `build_skill_script_wiring`, `build_skill_composer`'s renderer construction,
  `skill_content_kind` renderer + hashes) move with them.
- **S-D2 — kernel keeps.** The composer seam types (`SkillRenderer` /
  `RenderedSkills`, already composer-side); `noeta.execution.skills` becomes
  **seams-only** — the recording path (`activate_skills` over the engine's
  `emit_context_content_recorded`) and whatever pack-consuming glue is
  kernel-pure; the builder consumes a **skills kit** handed to it whole.
- **S-D3 — control side stays kernel** (phase-1 D3 reaffirmed): the `skill`
  control tool schema (`noeta.policies.control_tools`), `control_semantics`,
  and the `SkillEnforcementMode` vocabulary (already sunk in
  `noeta.runtime.governance`). Activation *selection* is kernel mechanism;
  the *material* (what a skill is, how it is found and rendered) is the mover.
- **S-D4 — manifest stays contribution-free** (browser/app precedent): the
  `skills` built-in dir keeps a declaration-free manifest; identity is carried
  by the `skill_invocation` capability flag exactly as today (parity pins it).
- **S-D5 — injection seam.** The builder takes `skills_factory` (loud-fail
  `None` — the skills pipeline runs unconditionally today, so the injection is
  unconditional like `guards_factory`); the factory returns the whole kit the
  builder currently assembles inline (registry, script wiring triple, content
  kind, allowed-tools raw extraction, hashes fn). SDK injects
  `parts.default_skills_factory()` / `parts.skills_impl()` (memoized dynamic
  resolution, M3 accessor pattern). `testing/profile` (if touched) resolves
  through the dynamic doorway at call time (M2 guards precedent).

## Sever list (grep-verified 2026-07-29)

`execution/builder.py` (imports `execution.skills` × 5 +
`policies.skill_tools`), `execution/skills.py` (imports `context.skills`,
`tools.skill_script`), `context/composer.py` (docstring mention only — no
code edge). Tests sweep: everything importing `noeta.context.skills`,
`noeta.tools.skill_script`, `noeta.policies.skill_tools`, or the moved
`execution.skills` names.

## Milestones

- [x] **S1 — the move.** Impl into `noeta/builtins/skills/impl/`;
  `execution.skills` reduced to seams; builder takes `skills_factory`;
  parts accessors; tests swept. Gates green, parity goldens + composed-request
  snapshots byte-identical.
- [x] **S2 — docs.** CONTEXT.md Built-in plugin entry, reference/plugins
  note, spec ticks + archive. (No packaging change: `noeta.tools.skill_script`
  simply leaves the runtime wheel, shrinking it further.)

## Acceptance

1. No module outside `noeta.builtins` statically imports a moved skills
   module; import-linter contracts all KEPT (the universal rule covers it).
2. Parity goldens 5/5 byte-identical; every composed-request / skill snapshot
   byte-identical; `make check` green.
3. Runtime wheel no longer ships the skills impl (install smoke unchanged —
   the runtime-alone closure still imports; the skills pipeline is behind the
   builder injection).
4. Public surface unchanged (`noeta.sdk` exports untouched).

## Risks

- **Byte-identity of the skill menu / body rendering** — the renderer moves
  but its output must not: goldens + snapshots gate every step.
- **Private-name splits** (bit M2 once): after splitting
  `execution/skills.py`, grep the moved half for every `_underscore` name.
- The `skill` control tool's enforcement plumbing crosses the seam
  (builder → guards): the *facts* (frozensets) already travel as parameters,
  not imports — keep it that way.

## Progress log

- **2026-07-29 — S1+S2 landed.** Movers (git-mv, bodies unchanged):
  `context/skills/indexer.py` → `builtins/skills/impl/indexer.py` (+
  `_frontmatter.py`; the `noeta.context.skills` package is gone);
  `tools/skill_script.py` → `…/impl/script.py`; `policies/skill_tools.py` →
  `…/impl/allowed_tools.py`; the registry/disk halves of `execution/skills.py`
  (`merge_skill_registries` / `_skill_root` / `resolve_skill_scripts` /
  `resolve_skill_roots` / `build_skill_script_wiring` /
  `extract_skill_allowed_tools_raw` / `_snapshot_skill_tiers` /
  `load_workspace_skills` / `build_skill_composer` / `skill_content_kind` /
  `DEFAULT_SKILLS_SUBDIR`) → `…/impl/wiring.py`, which also adds
  `build_skills_kit` — the factory body.

  Kernel: `execution/skills.py` is seams-only — `SkillsKit` (frozen
  dataclass: opaque `registry`, `script_tool`, the two guard frozensets,
  `content_kind`, resolved `allowed_tools`), `activate_skills`,
  `skill_content_hash` + `build_skill_hashes` (duck-typed descriptors —
  the kernel holds no indexer type), `SKILL_KIND` / `SKILL_DRIFT_POLICY`.
  Builder: the old `_stage_skills_registry` + `_stage_skill_scripts`
  merged into one `_stage_skills` over the injected `skills_factory`
  (loud-fail None, unconditional — the two stages were pipeline-adjacent so
  the tool-append order is unchanged); `_build_content_registry` consumes
  `asm.skill_content_kind`; the guards call consumes
  `asm.skill_allowed_tools`; the composer is constructed directly
  (`build_skill_composer`'s only skill-specific behaviour was a fallback
  the builder never used — it always passes the multi-kind registry).
  SDK: `parts.default_skills_kit_factory()` + `parts.skills_impl()`
  (memoized doorway); injected at BOTH host build sites (session + the
  workflow orchestration engine — missing the second was caught by the
  workflow suite's loud-fail). Tests: sed sweep over ~25 files + the two
  `tests/_session_inputs.py` helpers; mixed kernel/impl import lines split
  by hand. `execution/__init__` re-exports only the seams now.

  Gates: 3377 passed / 129 skipped (unchanged — pure moves), parity
  goldens 5/5 + composer/recall snapshots byte-identical, coverage 87.58%,
  mypy strict clean, import-linter 10/10 KEPT.
