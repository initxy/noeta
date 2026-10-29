"""Deferred tool loading: a tool of any plugin that sets ``deferred = True``
is announced by name only and sent to the model once ``ToolSearch`` has
loaded it.

While a task offers at least one deferred tool, this plugin adds
``ToolSearch`` (``Extension.tool_changes``, computed from the other
plugins' tools). A tool is loaded once a tool result of the task named it
with a ``ToolReference``: the conversation's whole history counts (a
compaction does not unload a tool, a continued task keeps its source's),
and so do the results of the batch still open. The plugin withholds every
deferred tool not loaded yet from the request (a call to one is refused
with :data:`NOT_LOADED`), and the request always lists
``DeferredToolPlaceholder``, an entry that is never called and keeps the
vendor's deferred-loading mode on from the first request.

Order (the kernel's): every sent tool sorted by name, then the tools that
declare ``listed_last`` (server tools) sorted by name. The placeholder goes
right before the ``listed_last`` group; while that group is empty and no
deferred tool is loaded, it goes right before the last tool.
"""

from __future__ import annotations

from collections.abc import Sequence

from noeta.core.extension import Extension, TaskView, ToolChanges, ToolEntry
from noeta.core.loop.state import TaskState
from noeta.core.tool import Tool, tool_flag
from noeta.core.types import ToolReference, ToolResult, ToolSpec
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


def loaded_tools(state: TaskState) -> set[str]:
    """Every tool a ``ToolReference`` result of the task named: in its
    history (never compacted) and in the open batch."""
    results = [b for m in state.history if m.role == "user" for b in m.content
               if isinstance(b, ToolResult)]
    results += [c.result for c in state.calls if c.result is not None]
    return {b.tool_name for r in results for b in r.content if isinstance(b, ToolReference)}


class ToolSearch(Extension):
    """Adds ``ToolSearch`` while any deferred tool is offered and withholds
    the deferred tools not loaded yet from the request."""

    name = "tool_search"

    def tool_changes(self, view: TaskView, offered: Sequence[Tool]) -> ToolChanges | None:
        deferred = {t.name: t for t in offered if tool_flag(t, "deferred")}
        if not deferred:
            return None
        add: list[Tool] = [] if any(t.name == TOOL_SEARCH for t in offered) \
            else [ToolSearchTool(deferred)]
        loaded = loaded_tools(view.state)
        withhold = {n: NOT_LOADED.format(name=n) for n in deferred if n not in loaded}
        sent = [t for t in [*offered, *add] if t.name not in withhold]
        head = sorted(t.name for t in sent if not tool_flag(t, "listed_last"))
        last_group = any(tool_flag(t, "listed_last") for t in sent)
        any_loaded = len(withhold) < len(deferred)
        before = "" if last_group or any_loaded or not head else head[-1]
        return ToolChanges(add=add, withhold=withhold,
                           entries=[ToolEntry(spec=placeholder_spec(), before=before)])
