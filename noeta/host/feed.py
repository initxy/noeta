"""What every front end (the TUI, ACP, stream-json) shows of a task's log
the same way: a tool call's one-line title and a failed turn's reason. The
core's own records are read with ``noeta.core.records.decode``."""

from __future__ import annotations

from typing import Any

from noeta.core.types import ToolUse


def tool_title(call: ToolUse, *, limit: int = 120, gap: str = " ") -> str:
    """``Read src/app.py``: the tool's name and its main argument's first
    line, cut at ``limit`` characters, joined by ``gap``."""
    i = call.input
    target = (i.get("file_path") or i.get("path") or i.get("command") or i.get("pattern")
              or i.get("url") or i.get("query") or i.get("description") or "")
    target = str(target).splitlines()[0][:limit] if target else ""
    return f"{call.name}{gap}{target}".rstrip()


def failure_text(data: Any) -> str:
    """A ``turn_failed`` record's reason and detail, ``reason: detail``."""
    d = data if isinstance(data, dict) else {}
    return ": ".join(str(x) for x in (d.get("reason"), d.get("detail")) if x)
