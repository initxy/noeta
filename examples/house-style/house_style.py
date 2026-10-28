"""house_style: an output style for every agent (Claude Code's output styles).
The style file is read once, when a task starts, and recorded: a task resumed
later or on another worker sends the same system prompt bytes even if the
file changed. A reminder restates the reply language on each of the person's
turns. Copy this file to ~/.noeta/plugins/."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from noeta import ContextPart, Extension, Reminder, TaskView


class HouseStyle(Extension):
    """``file``: a markdown style (frontmatter dropped); ``language``: the
    reply language, restated on each of the person's turns."""

    name = "house_style"
    add_to = ("*",)

    def __init__(self, file: str = "~/.noeta/output-styles/house.md",
                 language: str = "") -> None:
        self.file, self.language = file, language

    def reduce(self, state: Any, kind: str, data: Any) -> Any:
        return data["text"] if kind == "style" else state

    async def on_start(self, view: TaskView) -> None:
        if view.ext_state(self.name) is not None:  # captured at the first start
            return
        path = Path(self.file).expanduser()
        text = path.read_text("utf-8") if path.is_file() else ""
        if text.startswith("---\n"):
            text = text.split("\n---", 2)[-1]
        view.record(self.name, "style", {"text": text.strip()})

    def context(self, view: TaskView) -> Sequence[ContextPart]:
        style = view.ext_state(self.name)
        if not style:
            return ()
        return [ContextPart(text="\n# Output style\n" + view.prompt("house_style/style", style),
                            order=900)]

    async def reminders(self, view: TaskView) -> Sequence[Reminder]:
        last = view.state.messages[-1] if view.state.messages else None
        if not self.language or last is None or last.origin != "human":
            return ()
        text = view.prompt("reminder/house_style", f"Reply in {self.language}.")
        return [Reminder(text=text, kind="language")]
