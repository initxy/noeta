"""The plugin's other notes: the model and language notes, the deferred-tool
list, the token budget, the models' own prompt paragraphs and today's date."""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from noeta.core import models
from noeta.core.types import Message, ToolResult
from noeta.toolkit.commands import is_command_message
from noeta.toolkit.deltas import AMBIENT_NOTE, changed

#: The budget a turn starts with (Claude Code's ``<total_tokens>``).
TOKEN_BUDGET = 15_000_000


def valid_zone(name: Any) -> str | None:
    """``name`` when it is an IANA time zone this machine knows, else ``None``."""
    if not isinstance(name, str) or not name or name.startswith(("/", ".")):
        return None
    try:
        ZoneInfo(name)
    except (ValueError, KeyError, OSError):
        return None
    return name


def today_in(zone: str | None) -> str:
    """Today's date in ``zone``, or in UTC without one."""
    return datetime.now(ZoneInfo(zone) if zone else UTC).date().isoformat()


#: Told before the next model call when the task's model changed (Claude
#: Code's model note); ``{line}`` is the new model line.
MODEL_CHANGED = "The model changed during this conversation. {line}"
#: The ``language`` setting (Claude Code's language note); ``{language}`` is
#: its value.
LANGUAGE_NOTE = (
    "# Language\n"
    "Answer in {language}: explanations, code comments and every message to the user. Keep "
    "technical terms and code identifiers as they are. Write {language} with its full "
    "spelling, every accent and diacritic included; never put a plain ASCII letter in place "
    "of an accented one.")
LANGUAGE_CLEARED = "The language setting was removed: answer in the language the user writes in."


DEFERRED_TOOLS_HEAD = (
    "The tools below are deferred: only their names are known here and their schemas are "
    "not loaded, so calling one directly fails with an input validation error. Load the ones "
    "you need with ToolSearch, query \"select:<name>[,<name>...]\", before calling them:")


def deferred_list(names: Sequence[str]) -> str:
    """The deferred tools, one name per line under the lead-in."""
    return "\n".join([DEFERRED_TOOLS_HEAD, *names])


DEFERRED_BACK = (
    "{count} deferred tool{s} {verb} available again ({why}; the names were given earlier in "
    "this conversation): {names}. Load them with ToolSearch as before.")
DEFERRED_GONE_MCP = (
    "These deferred tools are no longer available (their MCP server disconnected). Do not "
    "search for them; ToolSearch will not find them:")
DEFERRED_GONE = (
    "These deferred tools are no longer available in this conversation. Do not search for "
    "them; ToolSearch will not find them:")


def deferred_delta(before: Sequence[str], ever: Sequence[str], now: Sequence[str]) -> str:
    """The note on a changed deferred-tool set (``""`` when it did not
    change): new names under the list's lead-in, names given earlier as
    available again, and the names gone."""
    joined, left = changed(before, now)
    new = [n for n in joined if n not in ever]
    back = [n for n in joined if n in ever]
    parts: list[str] = []
    if new:
        parts.append(deferred_list(new))
    for group, why in (([n for n in back if n.startswith("mcp__")], "their MCP server "
                        "reconnected"), ([n for n in back if not n.startswith("mcp__")],
                                         "offered again")):
        if group:
            parts.append(DEFERRED_BACK.format(
                count=len(group), s="" if len(group) == 1 else "s",
                verb="is" if len(group) == 1 else "are", why=why, names=", ".join(group)))
    mcp = [n for n in left if n.startswith("mcp__")]
    other = [n for n in left if not n.startswith("mcp__")]
    if mcp:
        parts.append("\n".join([DEFERRED_GONE_MCP, *mcp]))
    if other:
        parts.append("\n".join([DEFERRED_GONE, *other]))
    if left:
        parts.append(AMBIENT_NOTE)
    return "\n\n".join(parts)


def budget_note(left: int) -> str:
    return f"<total_tokens>{left} tokens left</total_tokens>"


#: A budget line of a system prompt (``main.md``'s last line), with the
#: blank lines before it; an appended prompt may follow it.
_PROMPT_BUDGET_LINE = re.compile(r"\n*^<total_tokens>\d+ tokens left</total_tokens>[ \t]*$",
                                 re.MULTILINE)

#: Claude Code 2.1.312's Claude models: every row of its catalog has the
#: ``total_tokens_reminder`` capability. It recognizes a model id that
#: contains one of these (case-insensitive: ``claude-opus-5-5[1m]``,
#: ``us.anthropic.claude-opus-5-5-v1:0``, a dated id), or the dated
#: first-party id of a 4.0 model.
_CLAUDE_CODE_MODELS = (
    "claude-3-5-haiku", "claude-3-5-sonnet", "claude-3-7-sonnet",
    "claude-haiku-4-5", "claude-haiku-5-5",
    "claude-sonnet-4-0", "claude-sonnet-4-5", "claude-sonnet-4-6", "claude-sonnet-5",
    "claude-sonnet-5-5",
    "claude-opus-4-0", "claude-opus-4-1", "claude-opus-4-5", "claude-opus-4-6",
    "claude-opus-4-7", "claude-opus-4-8", "claude-opus-5", "claude-opus-5-5",
    "claude-fable-5", "claude-fable-5-1", "claude-mythos-5", "claude-mythos-5-1",
    "claude-opus-4-2025", "claude-sonnet-4-2025",
)


def gets_budget(model: str) -> bool:
    """Whether ``model`` is told the ``<total_tokens>`` budget: Claude Code
    tells exactly the Claude models it knows (:data:`_CLAUDE_CODE_MODELS`)
    and nothing else (a gateway id like ``auto_model/x`` gets none). An
    alias resolves first."""
    real = models.resolve_alias(model).lower()
    return any(known in real for known in _CLAUDE_CODE_MODELS)


#: Claude Code's paragraph for Fable 5.1 (its ``fable_identity`` section,
#: after the cautions, before the session guidance).
FABLE_IDENTITY = (
    "You are Claude Fable 5.1, the newest model of Anthropic's Claude 5 family and part of "
    "the Mythos-class tier, which sits above Claude Opus in capability. Claude Fable 5.1 and "
    "Claude Mythos 5.1 are one underlying model: Fable 5.1, the most capable generally "
    "available Claude model, adds safeguards for dual-use capabilities, while Mythos 5.1 comes "
    "without them and only to approved organizations. When the person asks how the two "
    "differ, point them to https://www.anthropic.com/claude/fable.")
#: Claude Code's early-stopping guidance for Haiku 5.5 (the model's
#: ``haiku_5_5_early_stopping_guidance`` capability), at the prompt's end.
EARLY_STOPPING = (
    "The effort setting decides how much you think before you act, not how much of the "
    "request you finish. A turn lasts as long as you keep working, so a large task can be "
    "finished in the turn it was asked in; its size is no reason to check in first.\n"
    "Ending your turn halts all work until the user answers, and they may be away a while. "
    "Stopping before you change anything leaves them the code as it was plus a message to "
    "answer. End the turn when the request is done or nothing is left that you can do "
    "without them.\n"
    "Ask before you start only when you cannot name the most likely reading of the request; "
    "when you can, act on it and say in your final message which reading you took. The other "
    "reason to ask first is a fact, file or access that only the user has and the whole task "
    "depends on; their approval of a choice you could make yourself is not one. Actions that "
    "are hard to undo or reach outside still need their confirmation. When they say they "
    "want to approve something before you go on, such as a plan, stop there; words that only "
    "order the steps (\"plan, then build\") are not a stop, so do each step and go on. When "
    "they ask a question or are still choosing, they want your answer, not a change; when "
    "they also asked for work, answer and then do it.\n"
    "Edits to files in the working tree are neither hard to undo nor outward-facing, since "
    "the user can look at and revert them, unless they would overwrite work the user has in "
    "progress. That leaves the open choices to you: how to build the change, how to split "
    "it, how to handle a case the request left open. Pick what you would recommend, follow "
    "the user's words where they were specific, and list your choices in the final message "
    "so they can steer you. Start editing once you know the first change: a design worked "
    "out in files lasts, while a long stretch of thinking can be cut off and lost.\n"
    "When one part of the task is blocked, unclear or looks wrong, the rest usually is not. "
    "Try a step you expect to fail before you report it. Finish everything that does not "
    "depend on the stuck part and open your final message with what is stuck: finished parts "
    "help the user even when the whole task is not done. Setting the project up so you can "
    "build and test it, such as installing its declared dependencies, is part of the work; "
    "if it still cannot be built or run here, say so and make the changes you can check by "
    "reading. When you cannot find the cause of a problem, report what you ruled out and "
    "what would settle it. A question at the end of finished work costs the user one reply, "
    "the same as one asked before any work.")
#: Where the paragraphs go in the main prompt: before the first of these
#: headings (Fable's), and before the budget line or at the end (Haiku's).
_IDENTITY_ANCHORS = ("\n# Session-specific guidance\n", "\n# Environment\n")


def with_model_paragraphs(text: str, model: str) -> str:
    """The main prompt with Claude Code's paragraphs for ``model``: Fable
    5.1's identity, Haiku 5.5's early-stopping guidance. Only a prompt with
    the main prompt's ``# Environment`` section gets them (not a sub-agent's,
    not one the person replaced)."""
    if "\n# Environment\n" not in text:
        return text
    real = models.resolve_alias(model).lower()
    if "claude-fable-5-1" in real:
        at = min(i for a in _IDENTITY_ANCHORS if (i := text.find(a)) >= 0)
        text = f"{text[:at + 1]}{FABLE_IDENTITY}\n\n{text[at + 1:]}"
    if "claude-haiku-5-5" in real:
        budget = _PROMPT_BUDGET_LINE.search(text)
        if budget is None:
            text = f"{text.rstrip(chr(10))}\n\n{EARLY_STOPPING}"
        else:
            text = f"{text[:budget.start()]}\n\n{EARLY_STOPPING}{text[budget.start():]}"
    return text


def date_line(today: str, *, utc: bool = False) -> str:
    """Today's date for the environment turn; ``utc`` when the user's time
    zone is not known, so the date is UTC's and said to be."""
    if utc:
        return (f"The date today is {today} in UTC; the user's own date may differ. A date or "
                "time in a system reminder that is not marked UTC is the user's local one: "
                "take it as today's date.")
    return f"The date today is {today}."


DATE_CHANGED_REMINDER = (
    "The date moved on during this conversation: today is {date}. The user knows, so do not "
    "point it out."
)
#: The same, when the dates are UTC's (the user's time zone is not known).
DATE_CHANGED_UTC_REMINDER = (
    "The date in UTC moved on during this conversation: it is now {date}; the user's own "
    "date may differ. The earlier date line was in UTC too; a date or time in a system "
    "reminder that is not marked UTC is the user's local one: take it as today's date."
)


def _owes_budget_note(messages: Sequence[Message], *, prompts: bool = True) -> bool:
    """Whether a prompt (with ``prompts``) or a tool result came in since the
    last budget note; a typed slash command is not a prompt here (Claude
    Code sends none after one)."""
    for m in reversed(messages):
        if m.role == "assistant":
            return False
        if m.role == "system" and "<total_tokens>" in m.text:
            return False
        if m.role == "user" and any(isinstance(b, ToolResult) for b in m.content):
            return True
        if m.from_person:
            return prompts and not is_command_message(m.text)
    return False
