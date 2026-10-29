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
    #: A one-line identity sent as its own cached system block ahead of the
    #: prompt (the prompt name ``identity/<agent>``); empty sends none.
    identity: str = ""
    #: What the agent is for: its line in another agent's sub-agent roster.
    description: str = ""
    extensions: Sequence[Extension] = ()
    tools: Sequence[Tool] = ()
    effort: Effort | None = None
    max_tokens: int | None = None
    #: End the task when the first turn ends (a one-shot job) instead of
    #: waiting for the next message. A sub-agent's task always ends so.
    one_shot: bool = False
    output_schema: dict[str, Any] | None = None
    #: Model calls allowed in one turn; the next one asks for the turn's
    #: final answer with tools switched off (the agent file's ``maxTurns``).
    step_budget: int = 1_000
    #: As a sub-agent, run on the caller's model rather than ``model`` (an
    #: agent file that names no model: ``model`` is then the host's default).
    inherit_model: bool = False
    #: As a sub-agent, start in this permission mode (the agent file's
    #: ``permissionMode``); ``None``: the caller's.
    permission_mode: str | None = None
    #: ``"worktree"``: as a sub-agent, run in a fresh git worktree.
    isolation: str | None = None
    #: The agent file's other fields, as written.
    extra: dict[str, Any] = field(default_factory=dict)
    #: Only these tool names are offered (Claude Code's agent ``tools``);
    #: ``None`` = every tool the extensions offer.
    allowed_tools: Sequence[str] | None = None
    #: Tool names never offered, whatever offers them (Claude Code's agent
    #: ``disallowedTools``).
    denied_tools: Sequence[str] = ()

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
