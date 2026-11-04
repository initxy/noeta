"""Slash commands written as markdown (Claude Code's ``commands/*.md``): the
host reads the files and passes each command. Typing ``/<name> <args>``
sends the command's tags; the task then adds the body, as a skill typed by
name is added: the argument placeholders filled as Claude Code fills them
(:func:`noeta.toolkit.arguments.substitute_arguments`), its ``!`command```
lines run through the task's permission checks
(:mod:`noeta.toolkit.inline_commands`), and the tools its ``allowed-tools``
names pre-approved from then on. Two prompt commands come with it,
``/init`` and ``/review`` (:data:`BUILTIN`), unless a command of the same
name is passed; their bodies are prompts ``markdown_commands/<name>``."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from noeta.core import records
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

#: Typed commands whose added rules are kept for an erase, newest last.
ERASABLE_KEEP = 32

INIT_PROMPT = """\
Write the instruction file future agent sessions will read in this repository.

First learn the project: its README and build files, how it is built, linted and tested \
(running one test included), and how the main parts of the code fit together. Take in the \
instructions other tools keep here too, such as AGENTS.md, .cursorrules, .cursor/rules/ and \
.github/copilot-instructions.md.

Then write NOETA.md at the project's root; when NOETA.md or AGENTS.md is already there, \
improve that file instead of adding another. Keep it short and about this project only:
- the commands to build, lint and test, and how to run a single test;
- the design that only shows when reading several files together;
- the conventions someone new here would get wrong.

Leave out general advice, what a directory listing already shows, and anything you have \
not checked."""

REVIEW_PROMPT = """\
Review code changes for defects. When arguments follow, they name what to review: a pull \
request number (read it with `gh pr view` and `gh pr diff`), a branch, or paths. With none, \
review the uncommitted changes and the commits on the current branch that its base branch \
lacks (`git status`, `git diff`, `git log`).

Read enough of the code around each change to judge it. Report what should block a merge: \
wrong logic, errors or edge cases left unhandled, callers the change breaks, security holes, \
changed behavior without a test. Give each finding its file and line, what goes wrong, and \
how to fix it, most serious first. Skip matters of taste. When nothing needs fixing, say so. \
Change no files."""

#: The prompt commands every front end offers: name -> (description, argument
#: hint, body).
BUILTIN: dict[str, tuple[str, str, str]] = {
    "init": ("Write a NOETA.md with what agents need to know about this codebase", "",
             INIT_PROMPT),
    "review": ("Review the current changes, a branch or a pull request for bugs",
               "[PR number | branch | paths]", REVIEW_PROMPT),
}


class MarkdownCommands(Extension):
    """``commands``: one mapping per command — ``name``, ``body`` (the
    markdown after its frontmatter), and optionally ``description``,
    ``argument_hint`` and ``allowed_tools`` (the frontmatter's
    ``allowed-tools`` as written). ``builtin``: offer :data:`BUILTIN` too."""

    name = "markdown_commands"

    def __init__(self, commands: Sequence[Mapping[str, Any]] = (), *,
                 builtin: bool = True) -> None:
        self._commands: list[Command] = []
        self._bodies: dict[str, str] = {}
        self._allow: dict[str, tuple[str, ...]] = {}
        given = {str(c["name"]) for c in commands}
        extra = [{"name": name, "description": description, "argument_hint": hint,
                  "body": body}
                 for name, (description, hint, body) in BUILTIN.items()
                 if builtin and name not in given]
        self._builtin = {str(c["name"]) for c in extra}
        for c in [*commands, *extra]:
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

    def render(self, name: str, args: str, body: str | None = None) -> str:
        """The body ``/<name> <args>`` adds (``body`` in place of the
        command's), its commands not yet run."""
        body = self._bodies[name] if body is None else body
        escape = neutralise if has_commands(body) else None
        return substitute_arguments(body, args, (), escape).strip()

    def initial_state(self) -> Any:
        return {"allow_rules": []}

    def reduce(self, state: Any, kind: str, data: Any) -> Any:
        state = state or self.initial_state()
        if kind == "ran":
            have = list(state["allow_rules"])
            added = [r for r in data["allow_rules"] if r not in have]
            state = {**state, "allow_rules": have + added}
            if added and data.get("input") is not None:
                # Taken back if a hook blocks the command (only a message
                # the model has not seen yet is blocked: the newest few).
                by_input = {**(state.get("added") or {}), str(data["input"]): added}
                state["added"] = dict(list(by_input.items())[-ERASABLE_KEEP:])
            return state
        if kind == records.ERASED_KIND:
            by_input = dict(state.get("added") or {})
            gone = {r for seq in data["seqs"] for r in by_input.pop(str(seq), ())}
            return {**state, "added": by_input,
                    "allow_rules": [r for r in state["allow_rules"] if r not in gone]}
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
            body = view.prompt(f"{self.name}/{name}", self._bodies[name]) \
                if name in self._builtin else None
            try:
                text = await run_inline_commands(
                    view, self.render(name, args, body), skill=name, what="command",
                    call_id=f"command-{recorded.seq}", approve_rules=rules)
            except InlineCommandError as exc:
                view.notify(str(exc))
                view.record(self.name, "load_failed", {"name": name}, message=user(
                    f"The command could not run: {exc}", origin="inline"))
                continue
            view.record(self.name, "ran", {"name": name, "allow_rules": list(rules),
                                           "input": recorded.seq},
                        message=user(text, origin="inline"))

    async def before_tool(self, view: TaskView, call: ToolUse) -> Gate | None:
        rules = (view.ext_state(self.name) or {}).get("allow_rules") or ()
        return Allow(approve=True) if rules and rules_cover(view, call, rules) else None
