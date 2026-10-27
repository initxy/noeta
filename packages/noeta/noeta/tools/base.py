"""The tool interface and what a tool sees while it runs."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional, Protocol, Union

import msgspec

from noeta.execenv.base import ExecEnv
from noeta.types import Display, Image, Struct, Text


class ToolOutput(Struct):
    content: list[Union[Text, Image]]
    is_error: bool = False
    #: Recorded with the result for front ends and extensions; never sent to
    #: the model.
    meta: dict[str, Any] = msgspec.field(default_factory=dict)
    display: Optional[Display] = None


def text_output(text: str, *, is_error: bool = False, **meta: Any) -> ToolOutput:
    return ToolOutput(content=[Text(text=text)], is_error=is_error, meta=meta)


@dataclass
class ToolContext:
    task_id: str
    call_id: str
    env: ExecEnv
    #: Folded state of every extension, by extension name (read only).
    state: Mapping[str, Any]
    #: Record ``(extension, kind, data)``; the runner appends it as an
    #: ``ExtensionRecorded`` just before the call's ``ToolFinished``.
    record: Callable[[str, str, Any], None]
    #: Directory for full outputs too large to return inline, or ``None``.
    spill_dir: Optional[str] = None
    extras: dict[str, Any] = field(default_factory=dict)


class Tool(Protocol):
    name: str
    #: The model-facing description: the single source of the tool's
    #: semantics. Never restated in the system prompt.
    description: str
    input_schema: dict[str, Any]
    #: Only reads and holds no shared state: may run alongside other
    #: read-only calls of the same reply.
    read_only: bool
    #: Its result can be fetched again by calling it again, so an old result
    #: may be cleared from the context.
    clearable: bool

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput: ...
