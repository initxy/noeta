"""Building the model request from the folded state: one pure function, so a
recorded log always produces the same request bytes (and the prompt cache
keeps hitting)."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from typing import Any

from noeta.ext.base import ContextPart
from noeta.loop.state import TaskState
from noeta.tools.base import Tool
from noeta.types import ModelRequest, SystemPart, ToolSpec, dumps


def build_request(
    state: TaskState,
    *,
    model: str,
    system_prompt: str,
    parts: Sequence[ContextPart],
    tools: Sequence[Tool],
    effort: str | None = None,
    max_tokens: int | None = None,
    output_schema: dict[str, Any] | None = None,
) -> ModelRequest:
    system: list[SystemPart] = []
    if system_prompt:
        system.append(SystemPart(text=system_prompt))
    for part in sorted(parts, key=lambda p: p.order):
        system.append(SystemPart(text=part.text, cache=part.cache))
    if system and not any(p.cache for p in system):
        system[-1] = SystemPart(text=system[-1].text, cache=True)
    messages = list(state.messages)
    return ModelRequest(
        model=model,
        system=system,
        messages=messages,
        tools=[ToolSpec(name=t.name, description=t.description, input_schema=t.input_schema)
               for t in tools],
        max_tokens=max_tokens,
        effort=effort,  # type: ignore[arg-type]
        output_schema=output_schema,
        cache_after=_breakpoints(state),
    )


def _breakpoints(state: TaskState) -> list[int]:
    """The newest message, plus the newest user message before it, so the
    next step reads everything up to its own previous request from cache."""
    n = len(state.messages)
    if n == 0:
        return []
    points = [n - 1]
    for i in range(n - 2, -1, -1):
        if state.messages[i].role == "user":
            points.insert(0, i)
            break
    return points


def request_hash(request: ModelRequest) -> str:
    return hashlib.sha256(dumps(request)).hexdigest()[:32]
