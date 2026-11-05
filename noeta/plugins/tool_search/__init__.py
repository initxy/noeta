"""Deferred tool loading: a tool of any plugin that sets ``deferred = True``
is announced by name only and sent to the model once ``ToolSearch`` has
loaded it.

While a task offers at least one deferred tool, this plugin adds
``ToolSearch`` (``Extension.tool_changes``, computed from the other
plugins' tools). A tool is loaded once a tool result of the task named it
with a ``ToolReference``: the plugin records the names with that result
(``loaded``), so a compaction does not unload a tool and a continued task
keeps its source's. The plugin withholds every
deferred tool not loaded yet from the request (a call to one is refused
with :data:`NOT_LOADED`), and the request always lists
``DeferredToolPlaceholder``, an entry that is never called and keeps the
vendor's deferred-loading mode on from the first request.

Order (the kernel's): every sent tool sorted by name, then the tools that
declare ``listed_last`` (server tools) sorted by name. The placeholder is
always second to last: right before the last tool of that order, whatever
it is (Claude Code inserts it at ``length - 1``).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from noeta.core.extension import Extension, TaskView, ToolChanges, ToolEntry
from noeta.core.loop.state import TaskState
from noeta.core.mcp_names import legacy_index
from noeta.core.tool import Tool, ToolOutput, tool_flags
from noeta.core.types import ToolReference, ToolSpec, ToolUse
from noeta.plugins.tool_search.search import TOOL_SEARCH, ToolSearchTool

__all__ = ["NOT_LOADED", "PLACEHOLDER", "ToolSearch", "ToolSearchTool", "loaded_tools"]

PLACEHOLDER = "DeferredToolPlaceholder"
#: The error a call to a deferred tool that is not loaded gets.
NOT_LOADED = (
    "InputValidationError: {name} is a deferred tool whose schema is not loaded. Load it with "
    "ToolSearch (query \"select:{name}\"), then call it again.")

_PLACEHOLDER_DESCRIPTION = (
    "Reserved entry that only keeps deferred tool loading enabled. It does nothing; "
    "never call it.")


def placeholder_spec() -> ToolSpec:
    return ToolSpec(name=PLACEHOLDER, description=_PLACEHOLDER_DESCRIPTION,
                    input_schema={"type": "object", "properties": {}}, defer_loading=True)


#: The record of the tools a result loaded (``{"names": [...]}``).
LOADED = "loaded"


def loaded_tools(state: TaskState) -> set[str]:
    """Every tool a ``ToolReference`` result of the task named (recorded
    with the result, :data:`LOADED`)."""
    return set(state.ext.get(ToolSearch.name) or ())


class ToolSearch(Extension):
    """Adds ``ToolSearch`` while any deferred tool is offered and withholds
    the deferred tools not loaded yet from the request."""

    name = "tool_search"

    def reduce(self, state: Any, kind: str, data: Any) -> Any:
        if kind == LOADED and isinstance(data, dict):
            return frozenset(state or ()) | frozenset(data.get("names") or ())
        return state

    async def after_tool(self, view: TaskView, call: ToolUse, output: ToolOutput) -> ToolOutput:
        names = sorted({b.tool_name for b in output.content if isinstance(b, ToolReference)}
                       - loaded_tools(view.state))
        if names:
            view.record(self.name, LOADED, {"names": names})
        return output

    def tool_changes(self, view: TaskView, offered: Sequence[Tool]) -> ToolChanges | None:
        deferred = {t.name: t for t in offered if tool_flags(t).deferred}
        if not deferred:
            return None
        add: list[Tool] = [] if any(t.name == TOOL_SEARCH for t in offered) \
            else [ToolSearchTool(deferred)]
        loaded = loaded_tools(view.state)
        # A reference written under an MCP tool's old name loads it too (one
        # that two offered tools share loads neither).
        index = legacy_index(t.name for t in offered)
        loaded |= {index[o] for o in loaded if index.get(o) in deferred}
        withhold = {n: NOT_LOADED.format(name=n) for n in deferred if n not in loaded}
        sent = [t for t in [*offered, *add] if t.name not in withhold]
        order = [*sorted(t.name for t in sent if not tool_flags(t).listed_last),
                 *sorted(t.name for t in sent if tool_flags(t).listed_last)]
        before = order[-1] if order else ""
        return ToolChanges(add=add, withhold=withhold,
                           entries=[ToolEntry(spec=placeholder_spec(), before=before)])
