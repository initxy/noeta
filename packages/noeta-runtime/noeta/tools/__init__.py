"""L2 tools layer. Phase 0 ships only the FakeTool placeholder.

Also exports :func:`tool` — the decorator/factory that wraps a
plain ``fn(arguments, ctx) -> ToolResult`` as a runnable Tool carrying a
matching :class:`~noeta.agent.spec.ToolRef`.
"""

from __future__ import annotations

from noeta.tools.decorator import DecoratedTool, tool

__all__ = ["DecoratedTool", "tool"]
