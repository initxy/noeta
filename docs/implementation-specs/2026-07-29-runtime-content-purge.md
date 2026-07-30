# Microkernel phase 2c — runtime content purge

Status: Implemented (2026-07-30) — committed to main; pending review + archive

## Goal

Finish the microkernel story for **content**: after phase 1 + 2a + 2b moved
every capability *implementation* into the built-in plugins, a purity audit
(2026-07-29) found the runtime wheel still carries product **material** —
LLM-facing prose and product-default tables. The owner's bar: the runtime
changes only for bug fixes or new mechanisms; everything a product iterates
on (tool descriptions, resident prose, curated defaults) lives in plugins.

Not in scope: `noeta.policies.descriptions` (control-tool prose is
kernel-permanent per phase-2b P-D2/P-D4), the sqlite/postgres backends, the
`testing/` wheel-hygiene question, `tools/_limits.py` thresholds.

## Stages

### A — tool descriptions leave the runtime wheel

`noeta/tools/descriptions/*.md` (17 files) move into the builtin that ships
the tool: fs 9 (read/glob/grep/edit/write/apply_patch/shell_run/shell_poll/
shell_kill), web 2 (webfetch/web_search), browser 5, app 1. Load sites
(8 impl modules) repoint to `load_markdown(__package__, name)` — the
generic loader stays in `noeta.protocols.resources`. The
`noeta.tools.descriptions` package is deleted; `load_tool_description` goes
with it (its only consumers were the builtins + 2 test files). Packaging is
free: hatchling ships in-package `.md` (the `presets/prompts` precedent).

### B — control-band prose is consistently externalized

`policies/control_semantics.py` finishes what the 5 existing `.md` started:
the whole `structured_output` description and the two long `spawn_subagent`
property texts (`spawns`, `background`) move to
`policies/descriptions/*.md` (kernel-side — the control band stays kernel;
this stage is consistency, not relocation). One-line property descriptions
stay inline (schema-shape adjacent). Byte-identity: `load_markdown` strips
the trailing newline, so file bytes == old literal bytes.

The `core/_decision_handlers.py` background-started ack is **classified,
not moved**: spawn-subagent vocabulary sank kernel-side by decision P-D2,
core imports only `noeta.protocols` by contract, and a seam with no
substitution need is against the house rules. A comment records the
classification.

### C — the content-channel residents finish the reminders migration

The reminders pattern is the finished state: registry mechanism kernel-side,
renderer material in a builtin, injected at build. `context/memory.py`,
`context/environment.py`, `context/instructions.py` never got that
treatment — their renderers + prose still live in the runtime and the
builder imports them directly.

Constraint discovered in audit: **hash = sha256(rendered bytes)** — the
record-time hash (execution glue, incl. the mid-loop instructions discovery
hook) and the compose-time renderer must share one source. So the unit of
injection is a per-resident **kit** (the `SkillsKit` precedent): renderer +
hashes + `ContentKindSpec` factory travel together.

* Kernel keeps: kind vocabulary constants, snapshot types
  (`MemoryEntries`, `EnvironmentSnapshot`, `InstructionsSnapshot`), the
  loaders/recording seams in `execution/{memory,environment,instructions}.py`
  — but recording functions take the hash from the injected kit instead of
  importing a renderer.
* Builtins gain the material: memory index prose + recall
  matching/formatting (`match_memories*`, `RecallHit`, `format_recall_text`,
  `DEFAULT_RECALL_MAX_HITS`) → `builtins/memory/impl` (already its only
  consumer); environment + instructions renderers/prose → a new
  **`workspace` built-in** (identity-inert, declaration-free manifest —
  the browser/app precedent; catalogue 13 → 14). `DEFAULT_INSTRUCTIONS_FILENAMES`
  moves with the instructions material.
* Builder: `_build_content_registry` consumes injected kind factories
  (loud-fail when the matching feature is wired but the factory missing),
  the SDK resolves them through `noeta.client.parts` accessors.

Byte-identity bar: registration ORDER in `_build_content_registry` is
unchanged (skill, memory, instructions, environment, extras) and every
rendered string moves verbatim.

### D — the three product-default tables become injections

* `STUB_MODEL_ALLOWLIST` (execution/driver.py): the driver's
  `model_allowlist` param loses its product default (`None` ⇒ no
  deployment allowlist; principal gating unchanged). The `{opus, sonnet,
  haiku}` triple moves to the SDK client (its only production consumer,
  `client.py`, already passes it explicitly).
* `select_provider_edit_tool` (execution/builder.py): the kernel keeps a
  mechanical "drop these names for this family" filter driven by an
  injected mapping; the `{anthropic: apply_patch, openai: edit}` knowledge
  moves to the fs builtin (it ships both tools). No injection ⇒ no-op,
  which is today's None-family semantic.
* `_DEFAULT_RULES` + curated validators (runtime/shell_policy.py): the
  allowlist *engine* (`AllowRule`, `command_in_allowlist`,
  `_rule_from_spec`, `build_allowlist`) stays kernel; the curated rule
  table moves to the fs builtin. `build_allowlist` takes the base rules
  explicitly; both call sites (fs impl, host approval predicate) are
  SDK-side and pass the fs table.

## Acceptance

1. `make check` green (pytest, mypy strict, import-linter, coverage bar).
2. Parity goldens + composed-request snapshots byte-identical (prose moves
   verbatim; `.md` round-trip minds the trailing-newline strip).
3. Runtime source contains no builtin-tool description text, no resident
   renderer prose, no curated shell rules, no model-alias or edit-tool-name
   tables (grep-verified).
4. Install smoke: runtime wheel imports alone; sdk wheel resolves the
   11-tool roster with descriptions from their new homes; nothing statically
   imports `noeta.builtins` (contract unchanged).
5. CONTEXT.md tools-band wording updated ("descriptions" no longer listed);
   stale ADR landing-point notes annotated.

## Progress log

* 2026-07-29 — spec written after the purity audit; stages A–D scoped.
* 2026-07-30 — resumed after the concurrent stream landed (@aa01002).
  **Stage A landed**: 17 `.md` moved beside their impls (git mv), 8 impl
  modules + 2 test files repointed to `load_markdown(__package__, …)`,
  `noeta.tools.descriptions` deleted, CONTEXT.md + 2 ADR notes updated.
  **Stage B landed**: `structured_output.md`, `spawn_subagent_spawns.md`,
  `spawn_subagent_background.md` created byte-identical;
  `control_semantics` loads them; the background-started ack in
  `core/_decision_handlers.py` carries the P-D2 classification comment.
  **Stage D landed**: driver `model_allowlist` is now `Optional`/`None` =
  no deployment bound (the `{opus,sonnet,haiku}` triple became
  `noeta.client.client.DEFAULT_MODEL_ALLOWLIST`); the edit-tool mutex is
  the fs built-in's `PROVIDER_EDIT_TOOL_MUTEX` injected via
  `build_session_inputs(edit_tool_mutex=…)` (parts accessor
  `edit_tool_mutex()`); the curated shell rules moved to
  `builtins/fs/impl/shell_rules.py` (`DEFAULT_SHELL_RULES`),
  `build_allowlist` takes `base_rules` explicitly, `AllowRule` +
  `SHELL_META_CHARS` public-named (the 87c5c2e precedent; both were
  already imported cross-wheel). Gates at this point: 3385 passed /
  129 skipped, mypy strict clean, naming + import-linter 10/10 clean;
  ruff shows 15 PRE-EXISTING errors (identical set on @aa01002 —
  verified in a clean worktree; not introduced here).
* 2026-07-29 — execution PAUSED before any code change (owner's call): a
  concurrent uncommitted work stream is in the tree (workflow_sandbox
  public-naming + `disabled_builtins` semantics, ~56 files) and overlaps
  the Stage B/C/D targets (`control_semantics.py`, `builder.py`). Resume
  once that stream lands. Stage A recon is done and mechanical: the 17
  `.md` ↔ load-site mapping is verified 1:1 (fs 9 / web 2 / browser 5 /
  app 1), packaging needs no config (hatchling ships in-package `.md`),
  and `noeta/tools/__init__.py` does not re-export the loader — see the
  checklist in this spec's stages.
* 2026-07-30 — **Stage C landed** (the largest piece). Per-resident kit
  injection, the SkillsKit pattern: `MemoryIndexKit` (execution/memory.py),
  `InstructionsKit` — content_kind_from + hash + the filename search
  order — (execution/instructions.py), `EnvironmentKit`
  (execution/environment.py). The record seams (`record_memory_index` /
  `record_instructions` / `record_environment`), the discovery hook and
  `load_instructions`/`discover_instructions` all consume the injected
  kit; `context/{memory,environment,instructions}.py` slimmed to kind
  vocabulary (constants + snapshot types). Material landed in
  `builtins/memory/impl/index.py` (index prose + hash + matching + recall
  formatting; recall.py repointed) and the NEW **`workspace` built-in**
  (contribution-free manifest, browser precedent) holding both
  environment and instructions renderer material +
  `DEFAULT_INSTRUCTIONS_FILENAMES`. Catalogue 13→14 (`workspace` in
  `_BUILTINS` + `_INERT_BUILTIN_ACTIVATIONS`). SDK: three parts accessors
  (`default_{memory_index,instructions,environment}_kit`); SdkHost
  memoizes the kits and hands the SAME objects to the builder (compose)
  and driver/resolver (record) — one source of truth by construction.
  Builder: three loud-fail kit params consumed by
  `_build_content_registry`; registration order unchanged.
  **Final acceptance run**: `make check` green — 3385 passed /
  129 skipped, coverage 87.70% (bar 85), mypy strict clean, naming clean,
  import-linter 10/10; install smoke (local subset) 2/2; runtime
  grep-clean of every moved symbol and of functional product tokens
  (remaining mentions are docstrings). ruff: 14 errors, ALL pre-existing
  on @aa01002 (one fixed in passing); none introduced. CONTEXT.md +
  plugins reference (en/zh) updated to the 14-built-in catalogue.
