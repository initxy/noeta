# ADR-051: A control call answered in place can ride another control tool's Decision

- Status: accepted
- Date: 2026-09-25

## Context

Refines [ADR-004](004-control-tools-neutral-mechanism.md) (Control tools are SDK material; the kernel carries only neutral Decisions — `StatePatchDecision` and `yield_for_human`). Where the two disagree, this record wins.

## Decision

A response holding one `TodoWrite` plus one or more `Task` calls used to be
refused whole. It now saves the checklist and spawns in the same turn, on the
same neutral mechanism, with no new Decision variant and no new event type:

- **Translate chain.** `ControlTranslateContext.translate_rest(call_ids)`
  re-runs the dispatcher over the same response with the named `tool_use`
  blocks taken out. A translate that answers some calls in place (TodoWrite)
  hands the rest on and folds its patch and its ack into the Decision that
  comes back. The kernel still reads no todo shape; the ack is an ordinary
  `ToolResultBlock`. TodoWrite accepts only a `Task` companion — beside
  `AskUserQuestion`, `skill`, `run_workflow` or a second TodoWrite it still
  refuses the whole response, and a refused `Task` refuses the TodoWrite with
  the same reason.
- **Decisions.** `SpawnSubtaskDecision` and `SpawnSubtasksDecision` gain
  `preacked_results`, the same field `ToolCallsDecision` already had. The
  patch rides the existing `state_patch`, applied before the spawn handler.
- **One tool-role message per turn.** A background launch puts the pre-answered
  results in front of its "started" result. A foreground spawn cannot answer
  yet, and two consecutive tool-role messages would be two consecutive user
  turns on the Anthropic wire, so the results are held on the spawn record —
  `SubtaskSpawnedPayload.preacked_ref`, an optional trailing field (omitted
  when `None`, so recordings without it are byte-identical) on the first
  member of a fan-out — and the Engine's child-result seams render them in
  front of the child results at resume, from the log, so a fresh process
  renders the same message. A held spawn keeps them on its approval anchor; a
  refused spawn (Guard deny, human deny) answers the ridden call with its own
  ack, since the patch was already applied.
