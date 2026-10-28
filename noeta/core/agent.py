"""An agent: model, prompt, and the extensions that give it tools and
behavior. Usually built from an agent file (``noeta.agents.agent_files``); a
task names its agent, and every process that may run the task registers the
same agent under that name."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from noeta.core.extension import Extension, ToolSet
from noeta.core.tool import Tool
from noeta.core.types import Effort


@dataclass(frozen=True)
class Agent:
    name: str
    model: str
    system_prompt: str = ""
    #: What the agent is for: its line in another agent's sub-agent roster.
    description: str = ""
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
    #: Only these tool names are offered (Claude Code's agent ``tools``);
    #: ``None`` = every tool the extensions offer.
    allowed_tools: Sequence[str] | None = None

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
