"""``AgentSpec`` — the serializable Agent identity object.

An ``AgentSpec`` carries *only identity*: declared, canonical-serializable
fields and no ``Callable``\\s. Runtime wiring — turning a ``policy`` ref into a
live ``Policy``, a ``ToolRef`` into a live ``Tool`` — belongs to a separate
builder keyed by the same ``(name, version)`` refs, because a spec holding
closures could neither be recorded durably nor compared. Identity comparison is
plain frozen-dataclass structural equality, which works only because component
lists are normalised to sorted tuples at construction.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


__all__ = [
    "AgentSpec",
    "BudgetSpec",
    "ComponentRef",
    "ToolRef",
    "agent_activates",
]


@dataclass(frozen=True, slots=True, order=True)
class ComponentRef:
    """A versioned reference to a wired component (policy / composer / skill /
    guard / observer).

    ``version`` is the **behaviour** version, not a release tag: it MUST bump
    whenever the component's behaviour changes, because it is the only behaviour
    signal the spec's structural identity carries. ``order=True`` is what makes
    the containing spec's normalisation deterministic.
    """

    name: str
    version: str = "1"


@dataclass(frozen=True, slots=True, order=True)
class ToolRef:
    """A versioned reference to a tool. ``risk_level`` gates approval, so it is
    part of the tool's identity rather than wiring metadata."""

    name: str
    version: str = "1"
    risk_level: str = "low"


@dataclass(frozen=True, slots=True)
class BudgetSpec:
    """Declared default budget caps. Mirrors ``noeta.runtime.governance.Budget``
    1:1 so a host can build the live guard straight from the spec, and the two
    must stay in step. ``None`` ⇒ no cap for that dimension."""

    max_iterations: int | None = None
    max_tool_calls: int | None = None
    max_cost_usd: float | None = None
    max_spawned_subtasks: int | None = None
    max_subtask_depth: int | None = None


@dataclass(frozen=True, slots=True)
class AgentSpec:
    """A named Agent's serializable identity, recorded durably via
    ``AgentBound``.

    Component lists are normalised to sorted tuples on construction, so two
    specs differing only in author ordering compare equal and a recording's
    identity does not depend on how its author happened to list things.
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
    #: **Activation tuple — the behaviour-shaping identity.** The full
    #: resolved activation of this agent, sorted: the built-in feature bundles it
    #: opens (``todo_write`` / ``ask_user_question`` / ``skill_invocation`` /
    #: ``memory`` / ``mcp`` / ``browser`` / ``delegation``) plus the identity-inert
    #: tool packs (``fs`` / ``web`` …) and any external plugin names. Feature
    #: gating reads this tuple through :func:`agent_activates` — membership *is*
    #: the capability. The structural ``delegation`` derivation (a root with
    #: children delegates) and any external-plugin forced flags are folded in at
    #: compile time, so the tuple alone carries the full activation. A change here
    #: is a real identity change.
    plugins: tuple[str, ...] = ()
    #: The **names** of the subtask agents this Agent may delegate to (stable
    #: identity strings, normalised to a sorted tuple). Structural — derived from
    #: the ``agents`` roster, never an activation.
    spawnable: tuple[str, ...] = ()
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
        object.__setattr__(self, "plugins", tuple(sorted(self.plugins)))
        object.__setattr__(self, "spawnable", tuple(sorted(self.spawnable)))
        object.__setattr__(self, "metadata", dict(self.metadata))

    def to_dict(self) -> dict[str, Any]:
        """Canonical dict form (every field, recursively).

        Emits ``plugins`` + ``spawnable`` as the identity-carrying activation
        representation. Inverse of :meth:`from_dict`.
        """
        from dataclasses import asdict

        return asdict(self)

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "AgentSpec":
        """Rebuild an ``AgentSpec`` from :meth:`to_dict` output.

        A body carrying a ``capabilities`` key, or one missing ``plugins``,
        is rejected loud (``ValueError`` naming the offending key), never
        silently defaulted — a plugin-less agent must say so explicitly
        (``plugins=[]``), so a malformed body cannot slip through as one.
        """
        if "capabilities" in raw:
            raise ValueError(
                "AgentSpec.from_dict: unknown 'capabilities' key — identity is "
                "the 'plugins' activation tuple; re-record with one. A recording "
                "carrying 'capabilities' does not decode."
            )
        if "plugins" not in raw:
            raise ValueError(
                "AgentSpec.from_dict: missing required 'plugins' key — identity is "
                "the activation tuple; a plugin-less agent must still say "
                "so explicitly (plugins=[]), never default silently."
            )
        return cls(
            name=raw["name"],
            instructions=raw["instructions"],
            policy=_component_ref(raw["policy"]),
            composer=(
                _component_ref(raw["composer"])
                if raw.get("composer") is not None
                else ComponentRef("three_segment")
            ),
            tools=tuple(_tool_ref(t) for t in raw.get("tools", ())),
            skills=tuple(_component_ref(s) for s in raw.get("skills", ())),
            guards=tuple(_component_ref(g) for g in raw.get("guards", ())),
            observers=tuple(_component_ref(o) for o in raw.get("observers", ())),
            default_budget=_budget_spec(raw.get("default_budget")),
            plugins=tuple(raw["plugins"]),
            spawnable=tuple(raw.get("spawnable", ())),
            metadata=dict(raw.get("metadata", {})),
            default_model=raw.get("default_model"),
        )


def _component_ref(raw: Any) -> ComponentRef:
    """Rebuild a :class:`ComponentRef` from its :meth:`AgentSpec.to_dict` shape."""
    if isinstance(raw, ComponentRef):
        return raw
    return ComponentRef(name=raw["name"], version=raw.get("version", "1"))


def _tool_ref(raw: Any) -> ToolRef:
    """Rebuild a :class:`ToolRef` from its :meth:`AgentSpec.to_dict` shape."""
    if isinstance(raw, ToolRef):
        return raw
    return ToolRef(
        name=raw["name"],
        version=raw.get("version", "1"),
        risk_level=raw.get("risk_level", "low"),
    )


def _budget_spec(raw: Any) -> BudgetSpec:
    """Rebuild a :class:`BudgetSpec` from its :meth:`AgentSpec.to_dict` shape."""
    if raw is None:
        return BudgetSpec()
    if isinstance(raw, BudgetSpec):
        return raw
    return BudgetSpec(**dict(raw))


def agent_activates(agent: Any, plugin: str) -> bool:
    """The single derivation rule: does ``agent`` activate ``plugin``?

    Identity is the ``plugins`` tuple alone, so a feature is active **iff** its
    name is a member of that tuple: built-in feature bundles (``todo_write`` /
    ``ask_user_question`` / ``skill_invocation`` / ``memory`` / ``mcp`` /
    ``browser`` / ``delegation``) land there by their own name, and the
    structural ``delegation`` derivation plus any external-plugin forced flags
    are folded into the tuple at compile time — so this membership test is the
    whole rule, in one place, shared by the host and the resolver.

    Tolerates a non-``AgentSpec`` agent object that carries no ``plugins`` (the
    reserved ``__workflow__`` orchestration interpreter): it activates nothing.
    """
    return plugin in getattr(agent, "plugins", ())
