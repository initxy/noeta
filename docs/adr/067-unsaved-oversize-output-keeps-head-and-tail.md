# ADR-067: An over-size tool result with nowhere to be saved keeps its head and its tail

- Status: accepted
- Date: 2026-10-26

## Context

Refines [ADR-028](028-unified-context-supply.md), which cut an over-size result to its start inside a `<truncated-output>` block when the host configures no `tool_output_spill_dir`. Hosts run without one by default. For `Bash` that dropped the end of the output — where a build or test run prints its error — and the capture layer already keeps only the last 256 KB, so a large log came back as its middle.

## Decision

- **Without a spill directory, the inline form keeps the start and the end** of the result, cut on line boundaries, with a `[N characters dropped]` line between them, inside the same `<truncated-output>` block and within the same size limit. It applies to every tool's result, not only `Bash`.
- With a spill directory nothing changes: the full output is saved and the model gets the `<persisted-output>` preview and path.

## Rationale

- The end of a command's output is the part a model most needs; keeping only the start defeated the purpose of showing the result at all.

## Consequences

- The rendering lives in `large_tool_output_message` (`packages/noeta-runtime/noeta/core/_decision_handlers.py`); the recorded `ToolResultRecorded` still holds the full bytes.
