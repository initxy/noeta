"""``AgentSpec`` — the serializable Agent identity object.

An ``AgentSpec`` is *only identity*: declared, canonical-serializable fields,
no ``Callable``\\s. Runtime wiring (how a ``policy`` ref becomes a live
``Policy``, a ``ToolRef`` a live ``Tool``) is kept in a separate builder keyed
by the same ``(name, version)`` refs — ``noeta.agent`` for coding agents, the
future agent-sdk for official batteries. Keeping closures off the spec is what
keeps identity declarative: component lists are normalised to sorted tuples at
construction, so two specs that differ only in author ordering are ``==``.
Identity comparison is plain frozen-dataclass structural equality (an
earlier ``fingerprint`` digest was retired in favour of it).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping


__all__ = [
    "AgentSpec",
    "BudgetSpec",
    "Capabilities",
    "ComponentRef",
    "ToolRef",
]


@dataclass(frozen=True, slots=True, order=True)
class ComponentRef:
    """A versioned reference to a wired component (policy / composer / skill /
    guard / observer).

    ``version`` is the **behaviour** version, not a release tag:
    it MUST bump whenever the component's behaviour changes, because that is the
    only behaviour signal the spec's structural identity carries. ``order=True``
    makes refs sort deterministically by ``(name, version)`` for normalisation.
    """

    name: str
    version: str = "1"


@dataclass(frozen=True, slots=True, order=True)
class ToolRef:
    """A versioned reference to a tool, plus the metadata the runtime keys on
    (``risk_level`` gates approval).

    ``order=True`` sorts by ``(name, version, risk_level)``.
    """

    name: str
    version: str = "1"
    risk_level: str = "low"


@dataclass(frozen=True, slots=True)
class BudgetSpec:
    """Declared default budget caps. Mirrors ``noeta.guards.budget.Budget`` 1:1
    so a host can build the live guard straight from the spec. ``None`` ⇒ no
    cap for that dimension."""

    max_iterations: int | None = None
    max_tool_calls: int | None = None
    max_cost_usd: float | None = None
    max_spawned_subtasks: int | None = None
    max_subtask_depth: int | None = None


@dataclass(frozen=True, slots=True)
class Capabilities:
    """Behaviour-shaping capabilities that are part of an Agent's **identity**
    (not host config): which control surfaces the Agent may expose and whether
    it may delegate. A capability change is a real identity change, not a
    host-config tweak.

    ``spawnable`` lists the **names** of the subtask agents this Agent may
    delegate to (stable identity strings, normalised to a sorted tuple).
    """

    todo_write: bool = False
    ask_user_question: bool = False
    delegation: bool = False
    skill_invocation: bool = False
    #: memory v1: the host wires the memory tool pack
    #: (memory_write / memory_read), the index resident (content-channel
    #: kind "memory", policy "evolving") and user-message recall.
    memory: bool = False
    #: whether a delegated subtask may inherit the parent task's
    #: enabled MCP tool set. Per-spec opt-in: a child built off a spec with
    #: ``mcp=True`` inherits the parent's enabled aliases (it connects its own
    #: independent sessions, R-1 records its own specs); a child with
    #: ``mcp=False`` gets no MCP tools at all. presets default: main /
    #: general-purpose open it, explore / plan keep it closed.
    mcp: bool = False
    #: browser (sandbox-only): when the session has a provisioned sandbox
    #: container AND this is ``True``, the noeta-owned browser tool pack
    #: (``browser_navigate`` / ``click`` / ``type`` / ``extract`` /
    #: ``screenshot``) is merged into the tool set — flag-gated like ``memory``
    #: (never whitelist-filtered), backed per-session by the container's MCP
    #: browser server. ``False`` (default) OR no sandbox ⇒ no browser tools, so
    #: the tool set + stable prefix are byte-identical. presets default: main /
    #: the ``web`` subagent open it; explore / plan / general-purpose keep it
    #: closed (a heavy egress surface, opt-in per identity).
    browser: bool = False
    spawnable: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "spawnable", tuple(sorted(self.spawnable)))


@dataclass(frozen=True, slots=True)
class AgentSpec:
    """A named Agent's serializable identity.

    Resolve target for the server/worker; recorded durably via ``AgentBound``.
    Component lists are normalised to sorted tuples on construction so two specs
    that differ only in author ordering are ``==`` (structural equality is the
    identity comparison).
    """

    name: str
    instructions: str
    policy: ComponentRef
    composer: ComponentRef = ComponentRef("three_segment")
    tools: tuple[ToolRef, ...] = ()
    skills: tuple[ComponentRef, ...] = ()
    guards: tuple[ComponentRef, ...] = ()
    observers: tuple[ComponentRef, ...] = ()
    default_budget: BudgetSpec = field(default_factory=BudgetSpec)
    #: Behaviour-shaping capabilities (control surfaces + delegation) that are
    #: part of identity.
    capabilities: Capabilities = field(default_factory=Capabilities)
    #: Observational only (display name, owner, tags) — treated as cosmetic, not
    #: behaviour-affecting agent identity.
    metadata: Mapping[str, str] = field(default_factory=dict)
    #: Preferred LLM model id for this agent. A host-config /
    #: routing hint, **not** identity — swapping models must not change a
    #: recording's agent identity. ``None`` ⇒ host default.
    default_model: str | None = None

    def __post_init__(self) -> None:
        # Normalise to sorted tuples so identity is order-independent. Frozen
        # dataclass ⇒ assign through object.__setattr__.
        object.__setattr__(self, "tools", tuple(sorted(self.tools)))
        object.__setattr__(self, "skills", tuple(sorted(self.skills)))
        object.__setattr__(self, "guards", tuple(sorted(self.guards)))
        object.__setattr__(self, "observers", tuple(sorted(self.observers)))
        object.__setattr__(self, "metadata", dict(self.metadata))
