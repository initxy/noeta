"""The summarize call and the summary message, as Claude Code shapes them.

The call is the main request with the instruction appended to the last user
message; the reply's ``<analysis>`` is dropped and its ``<summary>`` becomes
``Summary:``; the summary message frames it (what it stands for, the tools
used before it, where the full transcript is, how to carry on, and whether
messages were kept after it). Every function here is pure, so the same
history and the same recorded reply always give the same message.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from importlib import resources

import msgspec

from noeta.core.errors import ContextOverflow, Fatal, ModelError, RateLimited, Transient
from noeta.core.types import Message, ModelRequest, ModelResponse, Text, ToolResult, ToolUse, user


def load_prompt(name: str) -> str:
    return (resources.files("noeta.plugins.compaction") / "prompts" / f"{name}.md").read_text(
        "utf-8").strip()


#: The instruction appended to the conversation (``compaction/summarize``).
SUMMARIZE_PROMPT = load_prompt("summarize")
#: Ahead of the instructions a ``/compact`` argument adds.
EXTRA_INSTRUCTIONS_HEAD = "Further instructions for this summary:"
#: The instruction's last paragraph, after any extra instructions.
CLOSING_REMINDER = (
    "Once more: no tool calls. Reply in plain text only, an <analysis> block and then a "
    "<summary> block; a tool call is refused and fails the task."
)

#: The summary message's first line.
SUMMARY_PREAMBLE = (
    "This conversation picks up from an earlier one that outgrew the context window; the "
    "summary below stands in for that earlier part."
)
#: Where the full transcript is (``{path}``).
TRANSCRIPT_NOTE = (
    "The complete conversation from before this summary, with exact code, error messages "
    "and your own earlier output, is in {path}; read it when a detail matters."
)
#: When the oldest rounds had to be dropped for the summarize call to fit.
HEAD_DROPPED_NOTE = (
    "The oldest part of the conversation did not fit into the summarize call and is not "
    "covered here. If the work turns out to depend on it, say so instead of guessing."
)
HEAD_DROPPED_TRANSCRIPT = " The transcript above still holds it."
#: How to carry on.
CONTINUE_NOTE = (
    "Carry on with the work from where it stopped and ask the user nothing further. Go "
    "straight back to it: do not mention this summary, do not recap, and do not announce "
    "that you are resuming. Treat the last task as if it had never been interrupted."
)
#: When the newest messages were kept as they were after the summary.
KEPT_NOTE = (
    "The messages after this summary are the newest ones from before it, kept exactly as "
    "they were. The summary was written without them, so work it calls unfinished may "
    "already be done there."
)

#: The stable head of every summarize failure (hosts branch on the prefix).
SUMMARY_FAILED_REASON = "compaction_summary_failed"

#: Retries of a summarize call that is itself too long (Claude Code's).
MAX_SUMMARY_OVERFLOW_RETRIES = 3
#: The output ceiling of the second attempt after a failed first one.
RETRY_MAX_TOKENS = 64_000

_ANALYSIS = re.compile(r"<analysis>[\s\S]*?</analysis>")
_SUMMARY = re.compile(r"<summary>([\s\S]*?)</summary>")
_RESERVED_TAG = re.compile(r"<(\s*/?\s*system-reminder\s*)>", re.IGNORECASE)
_FAILURE_EXCERPT_CHARS = 160


def instruction(prompt: str, extra: str = "") -> str:
    """The instruction text: the prompt, the extra instructions a manual
    compaction was given, the closing reminder."""
    text = prompt
    if extra.strip():
        text += f"\n\n{EXTRA_INSTRUCTIONS_HEAD}\n{extra.strip()}"
    return f"{text}\n\n{CLOSING_REMINDER}"


def _host_injected(message: Message) -> bool:
    """A reminder or a system turn: the instruction goes to the user message
    before it, and it stays where it is (Claude Code leaves the trailing
    token-count system turn after the instruction)."""
    return message.role == "system" or (message.role == "user"
                                        and message.origin in ("system", "memory"))


def summary_request(main: ModelRequest, history: Sequence[Message], text: str, *,
                    model: str | None = None, max_tokens: int | None = None) -> ModelRequest:
    """The main request with ``history`` as its conversation and ``text``
    appended to the last user message that is neither a reminder nor
    followed by a reply (a new user message when there is none); the cache
    mark sits on the message before it, so the call reads the conversation
    from cache. Everything else is the main request's."""
    messages = list(history)
    at = None
    for i in range(len(messages) - 1, -1, -1):
        m = messages[i]
        if m.role == "assistant":
            break
        if not _host_injected(m):
            at = i
            break
    if at is None:
        messages.append(user(text))
        at = len(messages) - 1
    else:
        m = messages[at]
        messages[at] = Message(role="user", content=[*m.content, Text(text=text)],
                               origin=m.origin)
    return msgspec.structs.replace(
        main, model=model or main.model, messages=messages,
        max_tokens=max_tokens if max_tokens is not None else main.max_tokens,
        cache_after=[at - 1] if at > 0 else [], output_schema=None, tool_choice=None)


def joined_text(response: ModelResponse) -> str:
    return "\n".join(b.text for b in response.message.content if isinstance(b, Text))


def format_summary(text: str) -> str:
    """The reply without its ``<analysis>``, ``<summary>`` unwrapped under
    ``Summary:``, runs of blank lines collapsed."""
    out = _ANALYSIS.sub("", text)
    out = _SUMMARY.sub(lambda m: f"Summary:\n{m.group(1).strip()}", out)
    out = re.sub(r"\n\n+", "\n", out)
    return _RESERVED_TAG.sub(r"&lt;\1>", out.strip())


_TOOLS_USED = re.compile(r"<tools-used-before-summary>(.*?)</tools-used-before-summary>")


def tools_used(messages: Sequence[Message]) -> list[str]:
    """The tools called in ``messages``, and those an earlier summary among
    them names, sorted."""
    names = {b.name for m in messages if m.role == "assistant"
             for b in m.content if isinstance(b, ToolUse)}
    for m in messages:
        if m.role == "user" and m.origin is None and m.text.startswith(SUMMARY_PREAMBLE):
            found = _TOOLS_USED.search(m.text)
            if found:
                names.update(n.strip() for n in found.group(1).split(",") if n.strip())
    return sorted(names)


def summary_message(note: str, *, tools: Sequence[str], transcript: str | None,
                    head_dropped: bool, kept: bool) -> str:
    """The text of the user message that stands for the summarized part."""
    used = ""
    if tools:
        used = ("<system-reminder>\n<tools-used-before-summary>"
                f"{', '.join(tools)}</tools-used-before-summary>\n</system-reminder>\n")
    text = f"{SUMMARY_PREAMBLE}\n\n{used}{format_summary(note)}"
    if transcript:
        text += "\n\n" + TRANSCRIPT_NOTE.format(path=transcript)
    if head_dropped:
        text += "\n" + HEAD_DROPPED_NOTE + (HEAD_DROPPED_TRANSCRIPT if transcript else "")
    text += "\n" + CONTINUE_NOTE
    if kept:
        text += "\n\n" + KEPT_NOTE
    return text


# --------------------------------------------------------------------------
# Overflow retry
# --------------------------------------------------------------------------


def _is_tool_results(message: Message) -> bool:
    return any(isinstance(b, ToolResult) for b in message.content)


def drop_oldest_rounds(messages: Sequence[Message]) -> list[Message] | None:
    """``messages`` less the oldest fifth (at least one) of its rounds;
    ``None`` when no round is left to drop. A round opens at every message
    that is not a tool result, so no result loses its call."""
    rounds: list[list[Message]] = []
    for message in messages:
        if _is_tool_results(message) and rounds:
            rounds[-1].append(message)
        else:
            rounds.append([message])
    if len(rounds) < 2:
        return None
    kept = rounds[max(1, len(rounds) // 5):]
    out = [m for r in kept for m in r]
    if out and out[0].role == "assistant":
        out.insert(0, user("(earlier conversation left out)"))
    return out


# --------------------------------------------------------------------------
# Failures
# --------------------------------------------------------------------------


def _excerpt(text: str, limit: int = _FAILURE_EXCERPT_CHARS) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 3] + "..."


def error_category(error: ModelError) -> str:
    if isinstance(error, ContextOverflow):
        return "overflow"
    if isinstance(error, (Transient, RateLimited)):
        return "transient"
    if isinstance(error, Fatal):
        return "fatal"
    return "unknown"


def summary_failure(outcome: ModelResponse | ModelError, text: str) -> str | None:
    """Why the reply cannot become the summary, or ``None`` when it can."""
    if isinstance(outcome, ModelError):
        error = _excerpt(str(outcome))
        return (f"errored summarize round-trip (category={error_category(outcome)}"
                + (f", error={error!r}" if error else "") + ")")
    if not format_summary(text):
        facts = f"stop_reason={outcome.stop_reason}, output_tokens={outcome.usage.output}"
        kinds = ", ".join(type(b).__name__ for b in outcome.message.content) or "none"
        return f"empty summarize response ({facts}, blocks={kinds})"
    return None
