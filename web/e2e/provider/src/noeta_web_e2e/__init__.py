"""An offline model for the web app's end-to-end test. Its reply is a pure
function of the conversation: the keyword in the person's last prompt picks a
script, and how many replies came since that prompt picks the step.

* ``bash`` — a ``Bash`` call that writes a file (so it asks), then a summary.
* ``write`` — a ``Write`` of the prompt's ``*.txt`` word (``notes.txt``) in
  ``$NOETA_WEB_E2E_DIR``, then a summary.
* ``todo`` — the task tools loaded, two ``TaskCreate`` calls and a ``TaskUpdate``, then a summary.
* ``ask`` — an ``AskUserQuestion``, then the answer echoed.
* ``agent`` — an ``Agent`` call to ``Explore``, whose task answers ``explored``.
* ``plan`` — ``ExitPlanMode`` loaded and called with a plan.
* ``slow`` — a long reply streamed slowly (for interrupting).
* anything else — a markdown reply with a code block, after a thought.

Text streams in small fragments, so the page shows it as it is written.
"""

from __future__ import annotations

import asyncio
import itertools
import os
from collections.abc import Sequence
from typing import Any

from noeta.core.types import (
    Delta,
    Message,
    ModelRequest,
    ModelResponse,
    Text,
    Thinking,
    ToolResult,
    ToolUse,
    Usage,
)

_ids = itertools.count(1)

HELLO = ("**Hello!** I am the scripted model.\n\nHere is some code:\n\n"
         "```python\nprint('hello from noeta')\n```\n")
SLOW = " ".join(f"word{i}" for i in range(400))


def _prompt(messages: Sequence[Message]) -> tuple[str, list[Message]]:
    """The person's last prompt and the messages after it."""
    for i in range(len(messages) - 1, -1, -1):
        m = messages[i]
        if m.from_person and m.text.strip() and \
                not any(isinstance(b, ToolResult) for b in m.content):
            return m.text, list(messages[i + 1:])
    return "", []


def _results(after: Sequence[Message]) -> list[str]:
    return ["".join(c.text for c in b.content if isinstance(c, Text))
            for m in after for b in m.content if isinstance(b, ToolResult)]


def _step(prompt: str, after: list[Message]) -> tuple[str, list[tuple[str, dict[str, Any]]],
                                                      str]:
    """``(text, calls, thought)`` of the next reply."""
    replies = sum(1 for m in after if m.role == "assistant")
    low = prompt.lower()
    work = os.environ.get("NOETA_WEB_E2E_DIR", os.getcwd())
    if "explore-sub" in low:
        return ("explored", [], "") if replies else \
            ("", [("Glob", {"pattern": "*.txt"})], "")
    if "bash" in low:
        if replies == 0:
            return "Let me run it.", [("Bash", {
                "command": "echo hello-from-bash > bash-out.txt && cat bash-out.txt",
                "description": "Say hello"})], ""
        return f"The command answered: {(_results(after) or [''])[-1].strip()[:80]}", [], ""
    if "write" in low:
        name = next((w for w in prompt.split() if w.endswith(".txt")), "notes.txt")
        if replies == 0:
            return "", [("Write", {"file_path": os.path.join(work, name),
                                   "content": "first line\nsecond line\n"})], ""
        return f"Wrote {name}.", [], ""
    if "todo" in low:
        script: list[tuple[str, dict[str, Any]]] = [
            ("ToolSearch", {"query": "select:TaskCreate,TaskUpdate", "max_results": 5}),
            ("TaskCreate", {"subject": "Read the code", "description": "Look around",
                            "activeForm": "Reading the code"}),
            ("TaskCreate", {"subject": "Fix the bug", "description": "Change it",
                            "activeForm": "Fixing the bug"}),
            ("TaskUpdate", {"taskId": "1", "status": "in_progress"}),
        ]
        if replies < len(script):
            return "", [script[replies]], ""
        return "Planned the work.", [], ""
    if "ask" in low:
        if replies == 0:
            return "", [("AskUserQuestion", {"questions": [{
                "question": "Which color do you like?", "header": "Color",
                "multiSelect": False,
                "options": [{"label": "Red", "description": "warm"},
                            {"label": "Blue", "description": "cool"}]}]})], ""
        return f"You answered: {(_results(after) or [''])[-1][:120]}", [], ""
    if "agent" in low:
        if replies == 0:
            return "", [("Agent", {"description": "Look around", "subagent_type": "Explore",
                                   "prompt": "explore-sub: list the text files"})], ""
        return "The sub-agent is done.", [], ""
    if "plan" in low:
        if replies == 0:
            return "", [("ToolSearch", {"query": "select:ExitPlanMode", "max_results": 5})], ""
        if replies == 1:
            return "", [("ExitPlanMode", {"plan": "# Plan\n\n1. Read\n2. Change\n"})], ""
        return "Following the plan.", [], ""
    if "slow" in low:
        return SLOW, [], ""
    return HELLO, [], "The person greets me; answer with a code sample."


class WebE2EProvider:
    family = "scripted"

    async def complete(self, request: ModelRequest,
                       on_delta: Any | None = None) -> ModelResponse:
        prompt, after = _prompt(request.messages)
        if "title" in (request.output_schema or {}).get("properties", {}):
            words = prompt.split("<session>")[-1].split("</session>")[0].split()[:4]
            return ModelResponse(message=Message(role="assistant", content=[Text(
                text='{"title": "%s"}' % " ".join(words).replace('"', ""))]),
                stop_reason="end_turn", usage=Usage(uncached=20, output=5))
        if any("<block>" in part.text for part in request.system):  # auto mode's classifier
            return ModelResponse(message=Message(role="assistant", content=[Text(
                text="<block>no</block>")]), stop_reason="end_turn", usage=Usage(uncached=20))
        text, calls, thought = _step(prompt, after)
        pause = 0.08 if "slow" in prompt.lower() else 0.01
        if on_delta is not None:
            if thought:
                on_delta(Delta(kind="thinking", text=thought))
            words = text.split(" ")
            for i, word in enumerate(words):
                await asyncio.sleep(pause)
                on_delta(Delta(kind="text", text=word + (" " if i < len(words) - 1 else "")))
        content: list[Any] = [Thinking(text=thought)] if thought else []
        content += [Text(text=text)] if text else []
        content += [ToolUse(id=f"call_{next(_ids)}", name=n, input=i) for n, i in calls]
        return ModelResponse(message=Message(role="assistant", content=content),
                             stop_reason="tool_use" if calls else "end_turn",
                             usage=Usage(uncached=120, output=30), model=request.model)


def provider(**_kw: Any) -> WebE2EProvider:
    """The ``noeta.providers`` factory (base URL, key and headers ignored)."""
    return WebE2EProvider()
