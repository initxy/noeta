"""Test helpers for hosts and for noeta's own tests: a scripted model."""

from __future__ import annotations

import itertools
from collections.abc import Callable, Sequence
from typing import Any, Union

from noeta.core.provider import OnDelta
from noeta.core.types import Delta, Message, ModelRequest, ModelResponse, Text, ToolUse, Usage

Step = Union[ModelResponse, Exception, Callable[[ModelRequest], ModelResponse]]

_ids = itertools.count(1)


def reply(text: str = "", *calls: tuple[str, dict[str, Any]], stop: str | None = None,
          usage: Usage | None = None) -> ModelResponse:
    """A canned reply: ``reply("done")`` or ``reply("", ("Read", {"file_path": "x"}))``."""
    content: list[Any] = [Text(text=text)] if text else []
    content += [ToolUse(id=f"call_{next(_ids)}", name=n, input=i) for n, i in calls]
    return ModelResponse(
        message=Message(role="assistant", content=content),
        stop_reason=stop or ("tool_use" if calls else "end_turn"),  # type: ignore[arg-type]
        usage=usage or Usage(uncached=10, output=5),
    )


class ScriptedProvider:
    """Answers each call with the next scripted step and keeps every request."""

    family = "scripted"

    def __init__(self, steps: Sequence[Step] = ()) -> None:
        self.steps = list(steps)
        self.requests: list[ModelRequest] = []

    def add(self, *steps: Step) -> None:
        self.steps.extend(steps)

    async def complete(self, request: ModelRequest,
                       on_delta: OnDelta | None = None) -> ModelResponse:
        self.requests.append(request)
        if not self.steps:
            raise AssertionError("ScriptedProvider ran out of steps")
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        response = step(request) if callable(step) else step
        if on_delta is not None and response.message.text:
            on_delta(Delta(kind="text", text=response.message.text))
        return response
