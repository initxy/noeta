"""Building the model request from the folded state: one pure function, so a
recorded log always produces the same request bytes (and the prompt cache
keeps hitting)."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from typing import Any

from noeta.core.extension import ContextPart
from noeta.core.loop.state import TaskState
from noeta.core.tool import Tool
from noeta.core.types import ModelRequest, SystemPart, ToolSpec, dumps


def build_request(
    state: TaskState,
    *,
    model: str,
    system_prompt: str,
    parts: Sequence[ContextPart],
    tools: Sequence[Tool],
    identity: str = "",
    effort: str | None = None,
    max_tokens: int | None = None,
    output_schema: dict[str, Any] | None = None,
) -> ModelRequest:
    """The system prompt is two cached blocks, as Claude Code sends it: the
    identity line, then the agent's prompt with the extensions' parts."""
    system: list[SystemPart] = [SystemPart(text=identity, cache=True)] if identity else []
    text = system_prompt
    for part in sorted(parts, key=lambda p: p.order):
        text = f"{text}\n{part.text}" if text else part.text
    if text:
        system.append(SystemPart(text=text, cache=True))
    messages = list(state.messages)
    return ModelRequest(
        model=model,
        system=system,
        messages=messages,
        tools=[ToolSpec(name=t.name, description=state.prompt(f"tool/{t.name}", t.description),
                        input_schema=t.input_schema)
               for t in tools if getattr(t, "advertised", True)],
        max_tokens=max_tokens,
        effort=effort,  # type: ignore[arg-type]
        output_schema=output_schema,
        cache_after=cache_breakpoints(state),
        root_id=state.root_id or state.task_id or None,
    )


def cache_breakpoints(state: TaskState) -> list[int]:
    """One breakpoint, on the newest message, as Claude Code places it: the
    next step reads everything up to it from cache."""
    return [len(state.messages) - 1] if state.messages else []


def request_hash(request: ModelRequest) -> str:
    return hashlib.sha256(dumps(request)).hexdigest()[:32]
