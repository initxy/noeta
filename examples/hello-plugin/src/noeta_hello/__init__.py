"""An example noeta plugin, installed as its own package.

It shows the parts most plugins use:

* a tool (``Greet``), whose description is a named prompt (``tool/Greet``)
  a user can override like any built-in tool's;
* state folded from the task's log: each greeting is recorded, so a resumed
  task still knows who was greeted;
* a question: ``Greet`` with no name asks the person who to greet (the
  TUI and editors show it with no code for this plugin);
* a reminder: after three greetings the model is told, once, to stop, and
  the person gets a notice the model never sees;
* a slash command (``/greet <name>``) front ends offer;
* arguments, from an agent file (``- hello: {greeting: Hi}``) or
  ``config.toml`` (``[plugins.hello] greeting = "Hi"``).

``pyproject.toml`` registers it under the ``noeta.plugins`` entry point
group as ``hello``; that is all noeta needs to find it. (Copied into
``~/.noeta/plugins/hello/`` it works too, without packaging; it joins an
agent through ``add_plugins``, as it sets no ``add_to``.)
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
    ask,
    read_answer,
    text_output,
)

__all__ = ["Hello"]

LIMIT = 3
WHO = ask({"question": "Who should I greet?"})


class Greet:
    name = "Greet"
    description = "Greet someone by name; without a name, the user is asked who."
    input_schema = {"type": "object", "properties": {"name": {"type": "string"}}}
    read_only = False
    clearable = False

    def __init__(self, plugin: Hello) -> None:
        self.plugin = plugin

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        who = str(input.get("name") or "").strip()
        if not who:
            return ToolOutput(park=WHO)  # wait for the person's answer
        return self._greet(who, ctx)

    async def resume(self, input: dict[str, Any], ctx: ToolContext, outcome: Any) -> ToolOutput:
        answers = read_answer(outcome, WHO)
        who = str((answers or {}).get("0", {}).get("other") or "").strip()
        if not who:
            return text_output("nobody to greet", is_error=True)
        return self._greet(who, ctx)

    def _greet(self, who: str, ctx: ToolContext) -> ToolOutput:
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
        view.notify(f"hello: {len(state['greeted'])} greetings, asked the agent to stop")
        return [Reminder(text=text, kind="enough")]

    async def commands(self, ctx: CommandContext) -> Sequence[Command]:
        return [Command(name="greet", description="Ask the agent to greet someone",
                        template="Please greet {args}.", argument_hint="<name>")]
