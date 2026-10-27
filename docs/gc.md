# GC

Entropy control. Feature work stays focused, so structure and docs drift by
design; gc pulls them back. Core rule: **separate discovery from execution** —
do safe things directly (gate-protected), propose anything structural.

## Scope

- scoped: after a large feature merges, only the changed area.
- global: periodic (weekly is a good default), the whole repo.

State which scope is running.

## Actions

1. Reconcile (execute directly; gates green after each step, or roll it back):
   - Start from `scripts/check-docs.sh`: every red line and every `gc due` line is
     on this pass's list.
   - Move terminal-but-unarchived specs into `archive/`; an `active` spec with every
     task ticked goes to review or gets its leftovers recorded; flag long-stale `active` /
     `proposed` ones (including stale claims — an `owner` gone quiet) to the user,
     don't delete.
   - Reconcile `CONTEXT.md` and `AGENTS.md` files against code entry by entry; code
     wins, fix the doc. Over budget → first cut what code already says down to a
     pointer and drop entries an agent can recover quickly; still over → move
     module-only entries into `<module>/AGENTS.md`. Never raise a budget to get
     green — that's the user's call.
   - Mark ADRs overturned by newer decisions `superseded`, don't delete.
   - Dead code, references to deleted files, stale TODOs — removal counts only
     with all gates green.
2. Prune the harness: for each `AGENTS.md` rule, name the failure it prevents.
   One that prevents nothing any more — the model already does it unasked — is
   proposed for deletion, not kept out of habit.
3. Scan (record only): duplicate patterns at rule-of-three, shallow modules,
   tests dragging the feedback loop. Mine spec Progress logs for friction first —
   where agents worked hardest is where refactoring pays most.
4. Propose (don't act): write structural changes as `proposed` specs — motivation
   citing evidence, expected benefit, acceptance criteria — queued for a human to pick.

## Red lines

- Never mix in behavior changes.
- When staleness is unclear, ask — don't delete.

## Report

Close with: what was cleaned, what was found, what was proposed, what's left for
the user to decide.
