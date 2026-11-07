"""An example plugin with a web part, as a plugin directory.

* a status item every front end shows (``ui``): how long the conversation is;
* a call its web part makes (``ui_call``): what is in the task's directory;
* a web part (``web/dist/index.js``, named by ``.noeta-plugin/plugin.json``)
  that adds a panel to the web and desktop apps' sidebar and makes that call.

``noeta plugin add examples/web-panel`` copies the directory into
``~/.noeta/plugins/``; it joins ``main`` by itself (``add_to``). Only the
built web part is installed and served: ``web/`` holds its source and the
build (``npm ci && npm run build`` there).
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from typing import Any

from noeta import CommandContext, Extension, StatusItem, TaskView

__all__ = ["WebPanel"]

#: The most directory entries the panel is sent.
MAX_ENTRIES = 50


class WebPanel(Extension):
    """A sidebar panel showing the task's directory, and the conversation's length."""

    name = "web-panel"
    add_to = ("main",)

    async def ui(self, view: TaskView) -> Sequence[StatusItem]:
        n = len(view.state.messages)
        return [StatusItem(key="messages", text=f"{n} message{'' if n == 1 else 's'}")]

    async def ui_call(self, ctx: CommandContext, name: str, args: Any) -> Any:
        if name != "workspace":
            return await super().ui_call(ctx, name, args)
        try:
            entries = sorted(os.listdir(ctx.cwd)) if ctx.cwd else []
        except OSError as exc:
            return {"cwd": ctx.cwd, "error": str(exc), "entries": [], "count": 0}
        shown = [e for e in entries if not e.startswith(".")]
        return {"cwd": ctx.cwd, "entries": shown[:MAX_ENTRIES], "count": len(shown)}
