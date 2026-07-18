"""Task conversation transcript extraction (source material for handoff
generation).

The input is the list of UIEvents replayed by the host layer (the narrow
vocabulary after translate); only the conversation backbone is taken: user
messages, assistant prose, the question text of follow-ups; tool-call
details / thinking / subtask internals are skipped.

With include_tools=True, tool-call summaries are appended (for handoff
generation to build the full handoff document).

Truncation policy: over budget, keep the **first user message** (the goal,
including the full template prompt) + **the last several turns**, replacing
the middle with an ellipsis marker — the head and the tail carry the most
handoff value.
"""
from __future__ import annotations

import json
from typing import Any

#: Transcript character budget (≈ a few thousand tokens, leaving the handoff
#: model plenty of window headroom)
MAX_CHARS = 24000

_ELLIPSIS = "\n\n... (the middle of the conversation was too long and has been omitted) ...\n\n"


def _question_text(data: dict) -> str:
    parts = []
    for q in data.get("questions") or []:
        if isinstance(q, dict) and q.get("question"):
            parts.append(str(q["question"]))
    return "; ".join(parts)


def _tool_call_summary(data: dict) -> str:
    """Tool call → one-line summary (e.g. read path=foo.md)."""
    tool_name = data.get("tool_name", "?")
    args = data.get("arguments") or {}
    if isinstance(args, dict):
        # Pick the most informative argument to show
        for key in ("path", "name", "query", "command", "url"):
            if key in args:
                val = str(args[key])[:80]
                return f"[Tool call] {tool_name} {key}={val}"
        args_str = json.dumps(args, ensure_ascii=False)[:100]
        return f"[Tool call] {tool_name} {args_str}"
    return f"[Tool call] {tool_name}"


def _tool_result_summary(data: dict) -> str:
    """Tool result → one-line summary (success/failure + short description)."""
    ok = "✓" if data.get("success") else "✗"
    summary = str(data.get("summary") or data.get("output") or "")[:150]
    return f"[Tool result {ok}] {summary}"


def build_transcript(events: list[Any], include_tools: bool = False) -> str:
    """UIEvent list → readable transcript text (an empty event stream
    returns an empty string).

    With include_tools=True, tool-call summaries are appended (for handoff
    generation to build the full handoff document).
    """
    blocks: list[str] = []
    for ev in events:
        etype = getattr(ev, "type", None)
        data = getattr(ev, "data", None) or {}
        if etype == "user_message":
            content = data.get("content")
            if isinstance(content, str) and content.strip():
                blocks.append(f"[User]\n{content.strip()}")
        elif etype == "assistant_text":
            text = data.get("text")
            if isinstance(text, str) and text.strip():
                blocks.append(f"[Assistant]\n{text.strip()}")
        elif etype == "question":
            text = _question_text(data)
            if text:
                blocks.append(f"[Assistant question]\n{text}")
        elif include_tools and etype == "tool_call":
            summary = _tool_call_summary(data)
            if summary:
                blocks.append(summary)
        elif include_tools and etype == "tool_result":
            summary = _tool_result_summary(data)
            if summary:
                blocks.append(summary)
    if not blocks:
        return ""

    total = sum(len(b) for b in blocks)
    if total <= MAX_CHARS:
        return "\n\n".join(blocks)

    # Truncate: keep the first block (the goal) + as much of the tail as fits
    head = blocks[0][:MAX_CHARS // 2]
    budget = MAX_CHARS - len(head) - len(_ELLIPSIS)
    tail: list[str] = []
    for b in reversed(blocks[1:]):
        if budget - len(b) < 0:
            break
        tail.append(b)
        budget -= len(b)
    tail.reverse()
    return head + _ELLIPSIS + "\n\n".join(tail)
