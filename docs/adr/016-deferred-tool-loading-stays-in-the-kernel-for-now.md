# ADR-016: Deferred tool loading stays in the kernel for now

- Status: accepted
- Date: 2026-10-28
- Refines: ADR-002

## Context

ADR-002 says the kernel carries no capability. Deferred tool loading is in the kernel all the
same: `noeta/core/deferred.py` holds the `ToolSearch` tool, its search and its model-visible
description; the runner adds `ToolSearch` to a task's tools while any offered tool is deferred
(`TaskRun.tools`); the fold keeps the names `ToolSearch` loaded (`TaskState.loaded_tools`); and
the request builder sends each loaded tool with `defer_loading` and always sends the
`DeferredToolPlaceholder` entry.

## Decision

Deferred loading stays in the kernel for the 1.0 release, as the one capability there. The
kernel part that has a reason to stay is the vocabulary: the `deferred` tool flag,
`ToolSpec.defer_loading`, the `ToolReference` result block providers encode, and folding
`ToolReference` results into `loaded_tools`. The `ToolSearch` tool itself (search and
description) and the placeholder entry are capability and are to move into a plugin.

## Rationale

Deferral works across plugins, and the extension interface gives a plugin no clean way to do
that today:

- A tool that depends on the other plugins' tools cannot be offered from `Extension.tools`:
  `TaskView.deferred_tool_names` asks every extension for its tools, the asking plugin
  included, so `ToolSearch` offered from a plugin would recurse.
- Withholding unloaded tools from a request is possible only by rewriting the built request in
  `before_model`, where the result depends on the order of extensions and on every later
  `before_model`; the request must stay a function of the log.
- Moving the code now would touch the runner (`noeta/core/runner/task.py`) and the fold late in
  the release, while other work changes the runner.

Rejected for now: moving the tool and the request filtering into a plugin through
`before_model` (order-dependent requests).

## Consequences

- `noeta/core/deferred.py` is the one exception to "the kernel carries no capability" (the
  layering checks cannot tell capability from kernel code inside `noeta/core`); no other
  capability may cite it as precedent.
- Follow-up: give `Extension` a way to offer tools computed from the task's other tools
  (called after every extension's `tools`, never recursively), then move `ToolSearchTool`, its
  description and the placeholder into a `tool_search` plugin, keeping the request bytes
  identical (`make parity` 0) and a fold snapshot of an existing task still valid.
