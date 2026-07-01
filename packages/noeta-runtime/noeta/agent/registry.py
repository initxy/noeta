"""``AgentRegistry`` — name → :class:`AgentSpec` resolve target.

The single entry point the server and worker use to resolve an Agent by name.
Resolution of an unknown name is a **hard error** (``UnknownAgentError``), never
a silent no-op now that ``agent_name`` is load-bearing.
"""

from __future__ import annotations

from noeta.agent.spec import AgentSpec


__all__ = [
    "AgentRegistry",
    "UnknownAgentError",
]


class UnknownAgentError(Exception):
    """A name was resolved that no registered Agent answers to.

    Generic over the resolution context: ``task_id`` is supplied when the
    lookup is driven by a leased Task (``noeta.agent.resolver``), and omitted for
    a bare registry lookup. This is the agent-layer home for the error;
    ``noeta.agent.resolver`` re-exports it (task #5).
    """

    def __init__(
        self,
        *,
        agent_name: str,
        available: list[str],
        task_id: str | None = None,
    ) -> None:
        self.agent_name = agent_name
        self.available = available
        self.task_id = task_id
        where = f"task {task_id!r} names" if task_id is not None else "no agent named"
        super().__init__(
            f"{where} unknown agent {agent_name!r}; available: {available}"
        )


class AgentRegistry:
    """In-process ``name → AgentSpec`` map. Duplicate names are rejected so a
    package cannot shadow another's Agent by accident."""

    def __init__(self) -> None:
        self._specs: dict[str, AgentSpec] = {}

    def add(self, spec: AgentSpec) -> None:
        """Register ``spec``. A name already present is an error."""
        if spec.name in self._specs:
            raise ValueError(
                f"agent {spec.name!r} already registered; names must be unique"
            )
        self._specs[spec.name] = spec

    def resolve(self, name: str) -> AgentSpec:
        """Return the ``AgentSpec`` named ``name``; unknown ⇒ ``UnknownAgentError``."""
        spec = self._specs.get(name)
        if spec is None:
            raise UnknownAgentError(agent_name=name, available=self.names())
        return spec

    def __contains__(self, name: object) -> bool:
        return name in self._specs

    def names(self) -> list[str]:
        """Registered Agent names, sorted."""
        return sorted(self._specs)
