"""Slash commands written as markdown (Claude Code's ``commands/*.md``): the
host reads the files and passes each command. Typing ``/<name> <args>``
sends the command's tags; the task then adds the body, as a skill typed by
name is added: the argument placeholders filled as Claude Code fills them
(:func:`noeta.toolkit.arguments.substitute_arguments`), its ``!`command```
lines run through the task's permission checks
(:mod:`noeta.toolkit.inline_commands`), and the tools its ``allowed-tools``
names pre-approved from then on."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from noeta.core.events import Recorded
from noeta.core.extension import Allow, Command, CommandContext, Extension, Gate, TaskView
from noeta.core.types import ToolUse, user
from noeta.toolkit.allowed_tools import allow_rule_texts
from noeta.toolkit.arguments import substitute_arguments
from noeta.toolkit.commands import command_message, parse_command_message
from noeta.toolkit.context import new_user_messages
from noeta.toolkit.inline_commands import (
    InlineCommandError,
    has_commands,
    neutralise,
    rules_cover,
    run_inline_commands,
)


class MarkdownCommands(Extension):
    """``commands``: one mapping per command — ``name``, ``body`` (the
    markdown after its frontmatter), and optionally ``description``,
    ``argument_hint`` and ``allowed_tools`` (the frontmatter's
    ``allowed-tools`` as written)."""

    name = "markdown_commands"

    def __init__(self, commands: Sequence[Mapping[str, Any]] = ()) -> None:
        self._commands: list[Command] = []
        self._bodies: dict[str, str] = {}
        self._allow: dict[str, tuple[str, ...]] = {}
        for c in commands:
            name, body = str(c["name"]), str(c["body"]).strip()
            self._bodies[name] = body
            allowed = c.get("allowed_tools")
            self._allow[name] = allow_rule_texts(str(allowed)) if allowed else ()
            self._commands.append(Command(
                name=name,
                description=str(c.get("description")
                                or (body.splitlines()[0][:80] if body else name)),
                template=command_message(name, "{args}"),
                argument_hint=str(c.get("argument_hint") or "")))

    async def commands(self, ctx: CommandContext) -> Sequence[Command]:
        return self._commands

    def render(self, name: str, args: str) -> str:
        """The body ``/<name> <args>`` adds, its commands not yet run."""
        body = self._bodies[name]
        escape = neutralise if has_commands(body) else None
        return substitute_arguments(body, args, (), escape).strip()

    def initial_state(self) -> Any:
        return {"allow_rules": []}

    def reduce(self, state: Any, kind: str, data: Any) -> Any:
        state = state or self.initial_state()
        if kind == "ran":
            have = list(state["allow_rules"])
            return {**state, "allow_rules": have + [r for r in data["allow_rules"]
                                                    if r not in have]}
        return state

    def _typed(self, text: str) -> tuple[str, str] | None:
        typed = parse_command_message(text)
        if typed is None:
            m = re.match(r"/(\S+)(?:\s+|$)(.*)", text.lstrip(), re.DOTALL)
            typed = (m.group(1), m.group(2)) if m else None
        return (typed[0], typed[1].strip()) if typed and typed[0] in self._bodies else None

    async def on_event(self, view: TaskView, recorded: Recorded) -> None:
        for message in new_user_messages(view.state, recorded.seq):
            typed = self._typed(message.text)
            if typed is None:
                continue
            name, args = typed
            rules = self._allow[name]
            try:
                text = await run_inline_commands(
                    view, self.render(name, args), skill=name, what="command",
                    call_id=f"command-{recorded.seq}", approve_rules=rules)
            except InlineCommandError as exc:
                view.notify(str(exc))
                view.record(self.name, "load_failed", {"name": name}, message=user(
                    f"The command could not run: {exc}", origin="inline"))
                continue
            view.record(self.name, "ran", {"name": name, "allow_rules": list(rules)},
                        message=user(text, origin="inline"))

    async def before_tool(self, view: TaskView, call: ToolUse) -> Gate | None:
        rules = (view.ext_state(self.name) or {}).get("allow_rules") or ()
        return Allow(approve=True) if rules and rules_cover(view, call, rules) else None
