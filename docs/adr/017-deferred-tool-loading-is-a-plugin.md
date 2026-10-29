# ADR-017: Deferred tool loading is a plugin; extensions change the tool list through tool_changes

- Status: accepted
- Date: 2026-10-28
- Refines: ADR-002
- Supersedes: ADR-016

## Context

ADR-016 kept deferred tool loading in the kernel because the extension interface had no clean
way to offer a tool built from the other plugins' tools (asking every extension for its tools
from inside one would recurse) or to keep tools out of a request (a `before_model` rewrite
depends on the order of extensions and on every later `before_model`).

## Decision

- `Extension.tool_changes(view, offered) -> ToolChanges | None` (`noeta/core/extension.py`):
  called after every extension's `tools`, with the tools they offer once the agent's allow and
  deny lists are applied, never with another extension's changes. It returns tools to add,
  offered tools to withhold from the request (each with the error a call to it gets, answered
  before any gate) and `ToolEntry`s, request entries that are no tool, each placed before a
  named tool or after the tools that are not `listed_last`. Every extension gets the same
  `offered`, so the result does not depend on their order: added tools follow the offered ones
  (first of a name wins), withheld names are a union, entries go in by name.
- The `tool_search` plugin (`noeta/plugins/tool_search`) owns `ToolSearch`, its search and
  description, the not-loaded error and `DeferredToolPlaceholder`, all through
  `tool_changes`. The kernel keeps the vocabulary: the `deferred` tool flag
  (`TaskView.deferred_tool_names`), `ToolSpec.defer_loading` on every sent deferred tool, the
  `ToolReference` block and the text that follows it in a batch's results.
- Which tools are loaded is read from the fold, not recorded: every `ToolReference` in the
  task's tool results, over `TaskState.history` (never compacted, carried into a continued
  task) and the open batch. `TaskState.loaded_tools` is gone.
- Every agent built from agent files runs `tool_search` (`ALWAYS_PLUGINS` in
  `noeta/agents/agent_files.py`) unless the host turns it off; an `Agent` built in code adds
  `ToolSearch()` itself.

## Rationale

`tool_changes` gives a plugin the other plugins' tools as an argument, so nothing recurses, and
withholding is a set the kernel applies before it builds the request, so the request stays a
function of the log whatever the plugin order. The `ToolReference` results are already in the
log; a plugin record of the same names would state the fact twice and miss tasks written
before it, while the history gives the same answer for every existing log.

Rejected: a plugin record of the loaded names folded in `reduce` (a second copy of a logged
fact; old logs would lose their loaded tools); one extension owning the request's tool list (a
single-owner slot that conflicts as soon as a second plugin wants it); the placeholder as a
callable tool (a call to it would run instead of answering "no such tool").

## Consequences

- No capability is left in `noeta/core`; ADR-016's exception is gone.
- Request bytes are unchanged (`make parity` 0; `tests/agents/test_request_golden.py`). A
  snapshot taken before this change has another fold version, so its task folds from the log.
- An agent with deferred tools and without `tool_search` sends them as ordinary tools marked
  `defer_loading`, and nothing can load them; the agent files cannot get there by accident.
