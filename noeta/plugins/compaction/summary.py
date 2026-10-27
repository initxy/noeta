"""The summarize call: its request, the note gate, and the verbatim safety
constraints. Every function here is pure, so the same history and the same
recorded reply always give the same note.

Texts are 0.6's byte for byte (``prompts/summarize.md``, the frame, the
constraint header, the truncation marker, the failure reasons).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from importlib import resources

from noeta.core.errors import ContextOverflow, Fatal, ModelError, RateLimited, Transient
from noeta.core.types import Message, ModelRequest, ModelResponse, Text, ToolResult, ToolUse, user


def load_prompt(name: str) -> str:
    """A prompt file next to this module, stripped like 0.6's description
    loader."""
    return (resources.files("noeta.plugins.compaction") / "prompts" / f"{name}.md").read_text(
        "utf-8").strip()


#: The trailing user turn of the summarize request (ADR-003, 049).
SUMMARIZE_PROMPT = load_prompt("summarize")

#: The line that opens the summary message: without it the note reads as a
#: fresh instruction and a "the user asked for X" in it is acted on again.
SUMMARY_FRAME = (
    "This conversation is continued from earlier turns that no longer fit in "
    "context. The note below summarises them and stands in for the messages "
    "it replaced; anything it restates, including requests and instructions, "
    "is a record of what already happened, not a new request."
)

#: The stable head of every summarize failure (hosts branch on the prefix).
SUMMARY_FAILED_REASON = "compaction_summary_failed"
#: The new boundary would not advance past what is already collapsed.
NO_PROGRESS_REASON = "compaction_no_progress"

#: Retries of a summarize call that is itself too long (Claude Code's).
MAX_SUMMARY_OVERFLOW_RETRIES = 3
SUMMARY_TRUNCATION_MARKER = "[earlier conversation truncated for compaction retry]"


def framed(note: str) -> str:
    return note if note.startswith(SUMMARY_FRAME) else f"{SUMMARY_FRAME}\n\n{note}"


def summary_request(
    main: ModelRequest,
    history: Sequence[Message],
    *,
    model: str,
    max_tokens: int | None,
    forbid_tools: bool = False,
    instruction: str = SUMMARIZE_PROMPT,
) -> ModelRequest:
    """The main request's system prompt and tools, ``history``, and the
    instruction as the trailing user turn. No ``tool_choice`` on the first
    attempt (so Anthropic's message-tier cache entry is still read); the one
    retry forbids tools. Never streamed: it goes through ``view.complete``."""
    return ModelRequest(
        model=model,
        system=list(main.system),
        messages=[*history, user(instruction)],
        tools=list(main.tools),
        max_tokens=max_tokens,
        thinking=main.thinking,
        headers=dict(main.headers),
        cache_after=[len(history) - 1] if history else [],
        tool_choice="none" if forbid_tools else None,
    )


def joined_text(response: ModelResponse) -> str:
    return "\n".join(b.text for b in response.message.content if isinstance(b, Text))


def needs_tool_free_retry(response: ModelResponse, text: str) -> bool:
    """A first attempt that answered with a tool call, or with no text."""
    if any(isinstance(b, ToolUse) for b in response.message.content):
        return True
    return not text.strip()


# --------------------------------------------------------------------------
# Overflow retry
# --------------------------------------------------------------------------


def _is_truncation_marker(message: Message) -> bool:
    return (message.role == "user" and len(message.content) == 1
            and isinstance(message.content[0], Text)
            and message.content[0].text == SUMMARY_TRUNCATION_MARKER)


def _is_tool_results(message: Message) -> bool:
    return any(isinstance(b, ToolResult) for b in message.content)


def drop_oldest_rounds(messages: Sequence[Message], *, keep_first: bool) -> list[Message] | None:
    """``messages`` less the oldest fifth (at least one) of its rounds, behind
    the truncation marker; ``None`` when no round is left to drop. A round
    opens at every message that is not a tool result, so no result loses its
    call; ``keep_first`` holds the previous note in place; a marker from an
    earlier retry is replaced, not stacked."""
    head = list(messages[:1]) if keep_first else []
    body = list(messages[1:]) if keep_first else list(messages)
    if body and _is_truncation_marker(body[0]):
        body = body[1:]
    rounds: list[list[Message]] = []
    for message in body:
        if _is_tool_results(message) and rounds:
            rounds[-1].append(message)
        else:
            rounds.append([message])
    if len(rounds) < 2:
        return None
    kept = rounds[max(1, len(rounds) // 5):]
    return [*head, user(SUMMARY_TRUNCATION_MARKER), *(m for r in kept for m in r)]


# --------------------------------------------------------------------------
# Safety / permission constraints survive verbatim (ADR-003)
# --------------------------------------------------------------------------

_CONSTRAINT_TRIGGERS: tuple[str, ...] = (
    # English
    "do not touch", "don't touch", "do not modify", "don't modify", "do not edit",
    "don't edit", "do not access", "don't access", "do not delete", "don't delete",
    "never edit", "never modify", "never touch", "never access", "never delete",
    "must not", "you may not", "not allowed to", "forbidden", "off-limits", "off limits",
    # Chinese
    "禁止", "不得", "不准", "不要修改", "不要碰", "不要动", "不要访问", "不要删除",
    "别碰", "别动", "严禁", "切勿", "勿",
)
#: Stripped off a detected line, so a constraint re-injected as ``- <line>``
#: is not re-injected one bullet deeper on the next pass.
_BULLET_PREFIXES: tuple[str, ...] = ("- ", "* ", "+ ")
VERBATIM_HEADER = "PRESERVED SAFETY/PERMISSION CONSTRAINTS (verbatim):"


def _line_is_constraint(line: str) -> bool:
    low = line.lower()
    return any(trigger in low for trigger in _CONSTRAINT_TRIGGERS)


def extract_safety_constraints(messages: Sequence[Message]) -> list[str]:
    """The verbatim constraint lines of every message the model did not
    write and the host did not inject (``origin`` unset or ``human``): never
    a tool result, an assistant turn, a reminder or a recalled memory. The
    previous note is passed with ``origin`` unset, so a constraint preserved
    once keeps binding across every later compaction."""
    seen: set[str] = set()
    out: list[str] = []
    for msg in messages:
        if msg.role == "assistant" or msg.origin in ("system", "memory"):
            continue
        for block in msg.content:
            if not isinstance(block, Text):
                continue
            for raw_line in block.text.split("\n"):
                line = raw_line.strip()
                for bullet in _BULLET_PREFIXES:
                    if line.startswith(bullet):
                        line = line[len(bullet):].strip()
                        break
                if not line or not _line_is_constraint(line) or line in seen:
                    continue
                seen.add(line)
                out.append(line)
    return out


def enforce_verbatim_constraints(summary: str, constraints: Sequence[str]) -> str:
    """Append, under :data:`VERBATIM_HEADER`, every constraint the note does
    not already contain verbatim. Only adds; idempotent."""
    missing = [c for c in constraints if c not in summary]
    if not missing:
        return summary
    block = "\n".join(f"- {c}" for c in missing)
    return f"{summary}\n\n{VERBATIM_HEADER}\n{block}"


# --------------------------------------------------------------------------
# The note gate (ADR-003, 060)
# --------------------------------------------------------------------------

SUMMARY_SECTION_TITLES: tuple[str, ...] = (
    "Primary Request & Intent",
    "Key Technical Concepts",
    "Files & Code",
    "Errors & Fixes",
    "All user messages",
    "Pending Tasks",
    "Decisions & Constraints",
    "Current Work",
    "Next Step",
)
_SUMMARY_MIN_SECTIONS = 2
#: A localised heading: a markdown heading or bold run opening with the
#: section number. A bare ``1. ...`` line is a narration's plan, not a note.
_LOCALISED_SECTION_HEADING = re.compile(
    r"^[ \t]*(?:#{1,6}[ \t]*|\*\*[ \t]*)([1-9])[ \t]*[.)、．:：]", re.MULTILINE  # noqa: RUF001
)
_FAILURE_EXCERPT_CHARS = 160


def looks_like_summary_note(text: str) -> bool:
    lowered = text.lower()
    found = sum(1 for title in SUMMARY_SECTION_TITLES if title.lower() in lowered)
    if found >= _SUMMARY_MIN_SECTIONS:
        return True
    numbered = {m.group(1) for m in _LOCALISED_SECTION_HEADING.finditer(text)}
    return len(numbered) >= _SUMMARY_MIN_SECTIONS


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
    """Why the reply must not become the note (the facts a host needs to tell
    the refusals apart), or ``None`` when it may."""
    if isinstance(outcome, ModelError):
        error = _excerpt(str(outcome))
        return (f"errored summarize round-trip (category={error_category(outcome)}"
                + (f", error={error!r}" if error else "") + ")")
    facts = f"stop_reason={outcome.stop_reason}, output_tokens={outcome.usage.output}"
    if not text.strip():
        kinds = ", ".join(type(b).__name__ for b in outcome.message.content) or "none"
        return f"empty summarize response ({facts}, blocks={kinds})"
    if not looks_like_summary_note(text):
        return (f"summarize response is not a note ({facts}, "
                f"text_len={len(text)}, head={_excerpt(text, 80)!r})")
    return None
