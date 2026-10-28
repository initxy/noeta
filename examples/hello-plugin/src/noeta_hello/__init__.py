"""An example noeta plugin, installed as its own package.

It shows the parts most plugins use:

* a tool (``Greet``), whose description is a named prompt (``tool/Greet``)
  a user can override like any built-in tool's;
* state folded from the task's log: each greeting is recorded, so a resumed
  task still knows who was greeted;
* a reminder: after three greetings the model is told, once, to stop;
* a slash command (``/greet <name>``) front ends offer;
* arguments, from an agent file (``- hello: {greeting: Hi}``) or
  ``config.toml`` (``[plugins.hello] greeting = "Hi"``).

``pyproject.toml`` registers it under the ``noeta.plugins`` entry point
group as ``hello``; that is all noeta needs to find it.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from noeta import (
    Command,
    CommandContext,
    Extension,
    Reminder,
    TaskView,
    Tool,
    ToolContext,
    ToolOutput,
    text_output,
)

__all__ = ["Hello"]

LIMIT = 3


class Greet:
    name = "Greet"
    description = "Greet someone by name."
    input_schema = {"type": "object", "properties": {"name": {"type": "string"}},
                    "required": ["name"]}
    read_only = False
    clearable = False

    def __init__(self, plugin: Hello) -> None:
        self.plugin = plugin

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        who = str(input.get("name") or "").strip()
        if not who:
            return text_output("name must not be empty", is_error=True)
        ctx.record(self.plugin.name, "greeted", {"name": who})
        return text_output(f"{self.plugin.greeting}, {who}!")


class Hello(Extension):
    name = "hello"

    def __init__(self, greeting: str = "Hello") -> None:
        self.greeting = greeting
        self._greet = Greet(self)

    def initial_state(self) -> Any:
        return {"greeted": [], "warned": False}

    def reduce(self, state: Any, kind: str, data: Any) -> Any:
        if kind == "greeted":
            return {**state, "greeted": [*state["greeted"], data["name"]]}
        if kind == "enough":
            return {**state, "warned": True}
        return state

    def tools(self, view: TaskView) -> Sequence[Tool]:
        return [self._greet]

    async def reminders(self, view: TaskView) -> Sequence[Reminder]:
        state = view.ext_state(self.name) or self.initial_state()
        if len(state["greeted"]) < LIMIT or state["warned"]:
            return ()
        text = view.prompt("hello/enough", "You have greeted enough people; stop greeting.")
        return [Reminder(text=text, kind="enough")]

    async def commands(self, ctx: CommandContext) -> Sequence[Command]:
        return [Command(name="greet", description="Ask the agent to greet someone",
                        template="Please greet {args}.", argument_hint="<name>")]
