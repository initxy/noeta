"""Tool-authoring machinery: :func:`tool` wraps a plain
``fn(arguments, ctx) -> ToolResult`` as a runnable Tool carrying a matching
:class:`~noeta.agent.spec.ToolRef`. No capability implementation lives here.
"""

from __future__ import annotations

from noeta.tools.decorator import DecoratedTool, tool

__all__ = ["DecoratedTool", "tool"]
