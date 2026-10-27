"""An agent definition: model, prompt, and the extensions that give it tools
and behavior. Agents are code; a task names its agent, and every process that
may run the task registers the same agent under that name."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from noeta.ext.base import Extension, ToolSet
from noeta.tools.base import Tool
from noeta.types import Effort


@dataclass(frozen=True)
class Agent:
    name: str
    model: str
    system_prompt: str = ""
    extensions: Sequence[Extension] = ()
    tools: Sequence[Tool] = ()
    effort: Effort | None = None
    max_tokens: int | None = None
    #: End the task when the first turn ends (subagents, one-shot jobs)
    #: instead of waiting for the next message.
    one_shot: bool = False
    output_schema: dict[str, Any] | None = None
    #: Model calls allowed in one turn before the turn fails.
    max_steps_per_turn: int = 1_000
    extra: dict[str, Any] = field(default_factory=dict)

    def all_extensions(self) -> list[Extension]:
        exts = list(self.extensions)
        if self.tools:
            exts.insert(0, ToolSet(self.tools))
        names = [e.name for e in exts]
        dupes = {n for n in names if names.count(n) > 1}
        if dupes or "" in names:
            raise ValueError(
                f"agent {self.name!r}: extension names must be unique and set: {dupes}"
            )
        return exts
