# Anchored content placement + instructions discovery

Implements `docs/adr/anchored-content-placement.md`. Two coupled slices: (A) the
generic placement rule (all content kinds), (B) the `read`-triggered discovery of
subdirectory `NOETA.md`/`AGENTS.md` (instructions kind only, flag-gated).

## Goal

1. A resident activated mid-task renders **at its activation point** in the
   dialogue instead of rewriting `semi_stable`.
2. After compaction, covered residents re-render right after the summary
   message.
3. With `instructions_discovery=True`, a successful `read` of a file inside the
   workspace activates every not-yet-active `NOETA.md`/`AGENTS.md` between the
   file's directory and the workspace root.

## Non-goals (v1)

- `grep`/`glob` triggers; discovery outside the workspace root; eager
  startup scans; a per-kind placement flag; noeta-agent enablement.

## Key decisions

- **D1 anchor**: fold records `ContextState.content_anchors["<kind>:<name>"] =
  len(task.runtime.messages)` when it first merges an activation
  (both `ContextContentRecorded` and legacy `SkillContentRecorded`).
  First-write-wins; dict default-empty (old snapshots rehydrate anchor-less →
  semi_stable placement, byte-safe "optional + last" convention).
- **D2 placement rule**: at compose, resident is *anchored* iff its anchor is
  strictly greater than the index of the first `role="assistant"` message in
  the raw rolling history; otherwise it renders in `semi_stable` as today.
  (Robust to seed ordering: goal-first vs skills-first seeding both keep
  pre-loop residents in `semi_stable`.)
- **D3 insertion coordinates**: with summary boundary `B` and summarized list
  `L = [summary] + raw[B:]` (or `raw` when no summary), effective index
  `eff = anchor - B + 1` clamped to `[1, len(L)]` when a summary applies, else
  `eff = anchor` clamped to `[0, len(L)]`. Anchors `< B` clamp to 1 → the
  re-hang bucket, ordered by (anchor, kind registration order, activation
  order). Then slide: while `L[eff].role == "tool"`, `eff += 1` (never insert
  before a tool-results message).
- **D4 per-name render**: anchored residents render via the existing registry,
  one name per call (`registry.render(kind, [name])`) — the skill renderer is
  already one-message-per-skill, so bytes match the batch shape. Rendered
  resources and skill names still feed `ContextPlan`
  (`selected_skills` = semi list + anchored skill names, deduped, in that
  order; `retrieved_resources` concatenated).
- **D5 discovery seam**: `Engine(content_discovery=...)` →
  `HandlerContext.content_discovery:
  Optional[Callable[[Task, ToolCall, ToolResult], list[ContextContentRecordedPayload]]]`.
  `handle_tool_calls` collects `(call, result)` pairs for invoked calls and,
  **after** the batched `MessagesAppended` is emitted+folded, runs the seam per
  pair, gates each payload first-only against `active_content`, then
  emits `ContextContentRecorded` + `apply_event`. (Approval-suspend early
  return skips discovery — v1 accepted.)
- **D6 discovery impl** (`execution/instructions.py`): given the read call's
  `path` argument, canonicalise via the session `WorkspaceRoot`
  (`canonicalise` — no fence; discovery itself checks `path_within(resolved,
  workspace.root)` and bails outside). Walk `resolved.parent` up to the root,
  **exclusive of the root directory** (the root file stays the pre-loop
  loader's job), shallowest directory first; per directory the first
  non-empty of `NOETA.md`, `AGENTS.md` wins; resident name = POSIX
  workspace-relative path (`src/pkg/AGENTS.md`). Reads go through the
  session's `ExecEnv` (sandbox reads inside the container). Loaded snapshots
  land in the build-owned mutable mapping BEFORE the payload is returned, so
  the renderer can render the name the moment fold activates it.
- **D7 renderer**: `context/instructions.py` gains a registry variant closing
  over a mutable `Mapping[str, InstructionsSnapshot]`; renders active names in
  activation order, `<workspace-instructions source="<name>">` tag unchanged
  (root file keeps its basename name → byte-identical). `hashes` resolves from
  the same mapping. Root-only hosts keep using the mapping with one entry.
- **D8 resume preload**: `Engine(content_preloader=Optional[Callable[[Task],
  None]])`, invoked at the top of each step before compose; the builder wires
  it (discovery mode only) to read every name in
  `active_content["instructions"]` missing from the mapping as
  `workspace_dir / name` through the ExecEnv. Missing/empty file ⇒ name stays
  unrendered (degrade, never raise). No-op when nothing is missing.
- **D9 config surface**: `SdkHost.instructions_discovery: bool = False` →
  `build_session_inputs(instructions_discovery=...)` → `_BuildSpec`. When on,
  the instructions kind is registered even with no root file (empty mapping
  renders nothing). Discovery does not require `instructions_enabled`.
- **D10 scope fence**: discovery only for reads resolving inside the workspace
  root (component-wise containment). Write grants (`extra_roots`) do NOT widen
  discovery.

## Tasks

- T1 `protocols/task.py`: `ContextState.content_anchors: dict[str, int]`.
- T2 `core/fold.py`: record anchors in `_on_context_content_recorded` +
  `_on_skill_content_recorded` (first-write-wins, alongside the merge).
- T3 `context/composer.py`: split active names by D2; anchored insertion per
  D3/D4 into the dynamic build (after `_apply_summary`, before
  `_reattach_thinking`/prune); plan fields per D4.
- T4 `context/instructions.py`: mapping-backed renderer + hashes
  (`instructions_content_kind_from`), keep the single-snapshot API delegating
  to it.
- T5 `execution/instructions.py`: `discover_instructions(...)` walk +
  `build_instructions_discovery(...)` (callable for D5) +
  `build_instructions_preloader(...)` (D8).
- T6 `core/_decision_handlers.py`: `HandlerContext.content_discovery` +
  `handle_tool_calls` integration (D5).
- T7 `core/engine.py`: `content_discovery` / `content_preloader` constructor
  params; preload call at step start.
- T8 `execution/builder.py`: `instructions_discovery` spec field; snapshot
  mapping owned by the assembly; kind registration condition; expose
  `content_discovery` / `content_preloader` on `SessionInputs`.
- T9 `sdk host.py`: `instructions_discovery` field; plumb through both
  `build_session_inputs` call sites; pass the two new seams to `Engine`.
- T10 tests: fold anchors (pre-loop 0 / mid-task > 0, first-write-wins);
  composer placement (no-mid-task byte-identity, anchored insert, tool-pair
  slide, post-summary re-hang, prune untouched); instructions mapping renderer
  bytes (root name unchanged); discovery walk (nesting, dedup, non-empty rule,
  outside-workspace bail, NOETA>AGENTS precedence, naming); handler emission
  (first-only, anchor after batch); preload (missing-name re-read, degrade);
  default-off byte-identity through `build_session_inputs`.

## Acceptance

- `make check` green.
- With the flag off: existing test suite passes without behavioural edits
  except placement-pinning tests updated for mid-task skill anchors.
- New tests above green.
