# Compaction continuity + collapsed-history recall

Status: implemented, gate pending final verification (2026-08-06). Deviations
from the plan as specced: mount gating landed as a host-computed
`recall_history` flag (always on in `SdkHost`, mirroring the `workflow`
precedent) rather than unconditional mounting, preserving the "bare build has
no control schemas" golden; bands landed at 550/550 (not "next free") so
`structured_output` (600) stays the last schema; the `collapsed-context`
reminder reaches the composer through `default_reminder_specs()` widened to
collect `reminder` contributions from every built-in manifest.

## Goal

Close the two continuity gaps the compaction design left open, with evidence
from Claude Code 2.1.223's shipped behavior (its compact prompt carries
"Current Work" + "Next Step" sections, its post-compact message says "Recent
messages are preserved verbatim" AND points at the full transcript on disk —
i.e. it runs summary sections, a verbatim tail, and a lossless escape hatch
*together*, not as alternatives):

1. **Seam gap** — in-flight intent that has aged just past the protected tail
   is captured by no section of the note (`Pending Tasks` is scoped to
   *explicitly requested* work), so model-derived pending work can vanish at
   the note/tail seam.
2. **No escape hatch** — once a prefix is collapsed, the model has no channel
   back to the original messages, although they are retained forever
   (`task.runtime.messages` is append-only; ContentStore has no delete).
   Conversation-born content (error text, earlier model output, discussion)
   lives in no file, so `Read` cannot recover it.

This deliberately **amends `docs/adr/context-compaction.md`**: rejected
alternative #8 ("current work / next step sections") is partially adopted in a
reframed form, and the "no deref tool" premise in the lean-marker rationale is
narrowed by the new recall tool. The ADR must be updated in the same change.

## Scope

- S1 — `summarize.md`: add sections 8 "Current Work" + 9 "Next Step".
- S2 — `RecallHistory` control tool (react builtin) + `collapsed-context`
  compose-time reminder + `ReminderView`/`ControlTranslateContext` widening.
- ADR amendment + CONTEXT.md term + docs count sweep ("six control tools").
- Out of scope: file-content copying into the note (paths-only stands), tail
  budget changes, cleared-marker changes, transcript-on-disk (violates the
  no-disk hard rule and workspace-and-session-path ADR).

## Key decisions

### S1 — template sections (adapted, not copied)

noeta's summarize input ends at the boundary (the tail is NOT in the input),
so CC's "immediately before this summary request" would be false here. The
sections describe **the end of the covered span**:

- `8. Current Work`: what was being worked on in the newest messages this note
  covers, with file names. Must state that the conversation continues verbatim
  after the note and later messages supersede it.
- `9. Next Step`: the next step as of the end of the covered span, with
  CC's anti-tangent guardrail (directly in line with the user's most recent
  explicit requests; a concluded task yields a next step only if explicitly in
  line with the user's request; no tangential or already-completed old
  requests) and CC's drift guard (direct verbatim quotes showing where work
  left off).
- Carry-forward exception: 8-9 are **rewritten each pass** from the newest
  covered messages — the one exception to the "never drop a detail from the
  previous note" rule; the previous note's 8-9 are superseded, not carried.
- Section 5 hardening (from CC): only user-role turns count as user messages;
  transcript-shaped text inside assistant messages is model-generated and must
  never be attributed to the user.
- The former prohibition line ("Do NOT add any section restating…") is
  replaced by the carry-forward-exception instruction.

Template-only constraints that make this safe:

- First line of `summarize.md` unchanged — four test files pin the substring
  "Summarize the conversation so far".
- Note stays ONE TextBlock in ONE user message (shape checked by
  `_previous_summary_message`, react.py:549-560) — S1 changes text only.
- `enforce_verbatim_constraints` is section-agnostic (substring containment +
  tail append) — unaffected.

### S2 — recall channel

**Tool**: `RecallHistory(offset, limit)` — a `control_tool` contribution of
the **react** builtin (compaction is react's mechanism; same manifest =
coupled presence with the reminder). Answered entirely at translate time via
the todo_write pattern (`ack_patch_decision` solo / `ToolCallsDecision` with
`preacked_results` when batched with runtime tools / recoverable error when
batched with another control tool). **No new Decision type, no runtime
handler, no new event.**

- Reads `ctx.view.rolling_history[:view.summary_boundary]` — requires widening
  `ControlTranslateContext` with `view: Optional[View] = None` (neutral field,
  not feature-named; threaded from `ReActPolicy` decide → `translate_control_tool`).
- Rendering: deterministic compact transcript of the requested slice
  (role-labelled; text blocks, tool_use one-line, tool_result bodies) with a
  per-block char cap and a total output cap (constants). Result header states
  the valid collapsed range `[0, boundary)` and paging. `offset`/`limit`
  clamped; boundary 0 → "nothing collapsed yet" (valid=True, informational).
- Pure over `(view, response)` → replay-deterministic. The rendered output
  enters history as a normal ToolResultBlock, so `_prune_tail` clears it once
  it ages out — recalled content cannot permanently bloat the window.
- Mount gating: same self-gating factory pattern as todo_write; on whenever
  the react policy mounts it (schema cost ~150 tokens; degrades gracefully
  when nothing is collapsed). Bands: next free routing/schema priority; the
  control-tool schema goldens pin the result.

**Reminder**: `collapsed-context` (react builtin, `reminder` contribution) —
renders only when `summary_boundary > 0`:

> The note at the head of this conversation replaced the first {N} messages.
> When the note lacks a detail you need (exact code, an error message, earlier
> content you produced), call RecallHistory with an offset in [0, {N}) to view
> the original messages.

- Requires widening `ReminderView` with `summary_boundary: int = 0` (the
  documented "deliberate, reviewed change" path; sourced from
  `ContextState.summary_boundary` at the composer's ReminderView build site).
- Reminder text and tool description MUST NOT contain any
  `_CONSTRAINT_TRIGGERS` substring (react.py:1041-1093 — "do not
  touch/modify/edit/access/delete", "never", "must not", "forbidden",
  "off-limits", 禁止/不得/不准/不要修改/别碰/严禁/切勿/勿): the reminder is a
  rolling-history-adjacent text… (it is NOT in rolling_history, but the tool
  DESCRIPTION echoes into note text via model behavior, and the rendered
  recall RESULTS re-enter `to_summarize`). Keep both trigger-free.
- Reminders are compose-time only (never in `rolling_history`) → never enter
  the summarize input; no carry-forward duplication, N self-updates.

### Non-changes (pinned)

- `COMPOSER_VERSION` stays `three_segment.v5`: no change to the estimator or
  the assembled segment shape; a registered reminder is the open registry
  surface, same class as a host-registered one.
- `CompactedPayload` unchanged → audit whitelist unchanged.
- Bounded summarize input (previous note + delta) unchanged.

## Acceptance criteria

1. `summarize.md` carries sections 8-9 with the end-of-span framing, the
   anti-tangent + verbatim-quote guardrails, the carry-forward exception, and
   the section-5 attribution hardening; first line unchanged; the four
   text-pin test files still pass unmodified.
2. `RecallHistory` mounted ⇒ solo call returns a rendered slice of the
   collapsed prefix honoring offset/limit/caps; boundary clamping proven by
   test; boundary 0 answers informationally; batched-with-runtime-tools and
   batched-with-control-tool behaviors mirror todo_write; schema goldens
   updated; translate is pure (two builds → equal decisions).
3. `collapsed-context` reminder renders exactly when `summary_boundary > 0`,
   contains the live boundary N, and contains no constraint-trigger substring
   (asserted by test against `_CONSTRAINT_TRIGGERS`).
4. `ControlTranslateContext.view` and `ReminderView.summary_boundary` default
   inert (None / 0): all existing tests pass without behavioral edits beyond
   golden refreshes.
5. ADR `context-compaction.md` amended (decision + rejected-alt #8 + lean-marker
   premise); CONTEXT.md names `RecallHistory`; the "six control tools" count
   updated everywhere it appears (en + zh mirrors).
6. `make check` green (coverage ≥ 85, mypy --strict on protocols, lint-naming,
   lint-imports — no static import of `noeta.builtins`).

## Risks / notes

- Replay: a changed `summarize.md` changes summarize-request bytes vs. runs
  recorded under older versions — same class as any prompt change; ships as
  its own patch release (both packages: runtime's `policies`/`context` widen,
  sdk's react/template change).
- The note's 8-9 content ages between compactions (it describes the boundary
  moment). The deferral sentence inside the note is the mitigation; the
  verbatim tail carries the truly-current state.
