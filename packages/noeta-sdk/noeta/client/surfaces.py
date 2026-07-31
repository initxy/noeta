"""The surface registry — the mechanism that keeps the plugin loader generic.

A :class:`SurfaceSpec` describes one extension surface: which plane it lives on,
how a contribution to it is *validated*, how contributions *collide*, and how
they *order*. The loader (:mod:`noeta.client.plugin_set`) consults one
:class:`SurfaceRegistry` and nothing else, so adding a surface means registering
a ``SurfaceSpec``, never editing the loader — including the identity projection,
which dispatches on the :data:`ActivationBinding` a surface declares rather than
on its name.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal, get_args

from noeta.client.options import AgentDefinition
from noeta.client.plugins import PluginError
from noeta.context.content_channel import ContentKindSpec


__all__ = [
    "SurfaceSpec",
    "SurfaceRegistry",
    "standard_registry",
    "STANDARD_SURFACES",
    "Plane",
    "ActivationScope",
    "ActivationBinding",
    "CollisionKey",
    "Ordering",
]


#: A surface's plane, mirroring the ``Options`` / ``HostConfig`` split.
Plane = Literal["identity", "wiring", "host"]

#: How a surface's effect is scoped across the agents in the process.
ActivationScope = Literal["per-agent", "process", "host-wired"]

#: The namespace a contribution's key lives in — a human label for collision
#: messages and the switch that decides whether two contributions can clash.
#: ``single-valued`` means at most one across the whole loaded set; ``none``
#: means the surface never collides (guards / observers / skill dirs).
CollisionKey = Literal["name", "kind", "alias", "single-valued", "none"]

#: How merged contributions order. ``sorted`` = ``(plugin, name)``; ``priority``
#: = an integer ``priority`` param first, ties broken by ``(plugin, name)``.
Ordering = Literal["sorted", "priority"]

#: How an **identity-plane** contribution binds into the per-plugin
#: :class:`~noeta.client.options.PluginActivation` handed to ``compile_options``.
#: The first five values name the ``PluginActivation`` channels — a closed set
#: the compile contract owns. ``"elsewhere"`` marks an identity-plane surface
#: deliberately NOT carried at compile because its own per-agent projection
#: resolves it (``control_tool`` rides ``activation_control_tools``). An
#: identity surface declaring NO binding fails projection loudly: a value
#: dropped silently between resolve and compile compiles the wrong identity.
ActivationBinding = Literal[
    "tool", "agent", "content_kind", "prompt_fragment", "policy", "elsewhere"
]

#: The legal values of each ``Literal``-typed field, checked at construction
#: and read straight off the annotations above so there is no second list to
#: keep in agreement.
#:
#: A ``SurfaceSpec`` is written positionally in practice, so changing a field in
#: the middle of the signature shifts every later argument one slot left —
#: silently, because a bare ``Literal`` annotation carries no runtime check.
#: Registration is a one-time, host-side act; validating it here costs nothing
#: and turns that class of drift into an error at the line that caused it.
_FIELD_VOCABULARY: dict[str, tuple[Any, ...]] = {
    "plane": get_args(Plane),
    "activation_scope": get_args(ActivationScope),
    "collision_key": get_args(CollisionKey),
    "ordering": get_args(Ordering),
}


@dataclass(frozen=True)
class SurfaceSpec:
    """One extension surface, fully described so the loader stays generic.

    ``validator`` is called on a **resolved** contribution value (after a
    manifest ``ref`` has been imported) and must raise — a :class:`PluginError`
    is preferred — when the value is not a legal member of the surface. Listing
    and manifest-level collision never call it: they run without executing
    plugin code.
    """

    name: str
    plane: Plane
    activation_scope: ActivationScope
    validator: Callable[[Any], None]
    collision_key: CollisionKey
    ordering: Ordering = "sorted"
    #: How an identity-plane contribution binds into ``PluginActivation``.
    #: Required for ``plane="identity"`` — the loader's projection dispatches on
    #: it instead of matching surface names, so a host-registered identity
    #: surface projects without a loader edit. ``None`` for every wiring / host
    #: surface: they project through the per-agent parameter channels instead.
    activation_binding: ActivationBinding | None = None

    def __post_init__(self) -> None:
        for field_name, legal in _FIELD_VOCABULARY.items():
            value = getattr(self, field_name)
            if value not in legal:
                raise PluginError(
                    f"surface {self.name!r}: {field_name}={value!r} is not one "
                    f"of {', '.join(repr(v) for v in legal)} — check the "
                    f"positional order of the SurfaceSpec fields"
                )
        # Caught at registration rather than at projection, where an identity
        # surface with no binding would simply vanish between the two.
        if self.plane == "identity" and self.activation_binding is None:
            raise PluginError(
                f"surface {self.name!r} is identity-plane but declares no "
                f"activation_binding — an identity contribution with no "
                f"binding never reaches compile_options. Declare one of "
                f"tool / agent / content_kind / prompt_fragment / policy, "
                f"or 'elsewhere' if a per-agent projection carries it."
            )
        if self.plane != "identity" and self.activation_binding is not None:
            raise PluginError(
                f"surface {self.name!r} is {self.plane}-plane but declares "
                f"activation_binding={self.activation_binding!r} — only "
                f"identity-plane surfaces bind into PluginActivation"
            )
        bindings = get_args(ActivationBinding)
        if self.activation_binding is not None and (
            self.activation_binding not in bindings
        ):
            raise PluginError(
                f"surface {self.name!r}: activation_binding="
                f"{self.activation_binding!r} names no PluginActivation "
                f"channel — expected one of "
                f"{', '.join(repr(b) for b in bindings)}"
            )


class SurfaceRegistry:
    """The surface catalogue the loader consults — one name → one ``SurfaceSpec``.

    Deliberately small: register, look up, list. A host extends the standard set
    by taking a :meth:`copy` of :func:`standard_registry` and registering its own
    app-plane surfaces before calling ``load_plugins``.
    """

    def __init__(self) -> None:
        self._by_name: dict[str, SurfaceSpec] = {}

    def register(self, spec: SurfaceSpec) -> None:
        """Add ``spec``. A second surface under the same name raises loudly."""
        if not isinstance(spec, SurfaceSpec):
            raise PluginError(
                f"register() expects a SurfaceSpec; got {type(spec).__name__}"
            )
        if spec.name in self._by_name:
            raise PluginError(
                f"surface {spec.name!r} is already registered — surface names "
                f"must be unique (no override)"
            )
        self._by_name[spec.name] = spec

    def get(self, name: str) -> SurfaceSpec:
        spec = self._by_name.get(name)
        if spec is None:
            raise PluginError(
                f"unknown surface {name!r} — not registered (known: "
                f"{', '.join(sorted(self._by_name)) or '<none>'})"
            )
        return spec

    def __contains__(self, name: object) -> bool:
        return name in self._by_name

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._by_name))

    def copy(self) -> "SurfaceRegistry":
        """A shallow, independent copy — the seam a host extends before load."""
        clone = SurfaceRegistry()
        clone._by_name = dict(self._by_name)
        return clone


# ---------------------------------------------------------------------------
# Validators — structural legal-value gates; a surface's runtime contract is
# enforced where that surface is consumed, not here.
# ---------------------------------------------------------------------------


def _reject(surface: str, value: Any, why: str) -> None:
    raise PluginError(
        f"surface {surface!r}: illegal contribution value ({why}); "
        f"got {type(value).__name__}"
    )


def _v_present(surface: str) -> Callable[[Any], None]:
    def check(value: Any) -> None:
        if value is None:
            _reject(surface, value, "must not be None")

    return check


def _v_callable(surface: str) -> Callable[[Any], None]:
    def check(value: Any) -> None:
        if not callable(value):
            _reject(surface, value, "must be callable")

    return check


def _v_str(surface: str) -> Callable[[Any], None]:
    def check(value: Any) -> None:
        if not isinstance(value, str) or not value.strip():
            _reject(surface, value, "must be a non-empty string")

    return check


def _v_tool(value: Any) -> None:
    if isinstance(value, str) and value.strip():
        return
    if getattr(value, "ref", None) is not None or callable(value):
        return
    _reject("tool", value, "must be a built-in name or a .ref-bearing tool")


def _v_agent(value: Any) -> None:
    if not isinstance(value, AgentDefinition):
        _reject("agent", value, "must be an AgentDefinition")


def _v_content_kind(value: Any) -> None:
    if not isinstance(value, ContentKindSpec):
        _reject("content_kind", value, "must be a ContentKindSpec")


def _v_path(value: Any) -> None:
    if not isinstance(value, (str, Path)) or not str(value).strip():
        _reject("skills", value, "must be a non-empty path")


# ---------------------------------------------------------------------------
# The standard catalogue — sixteen surfaces.
# ---------------------------------------------------------------------------


#: The sixteen standard surfaces. Two carry a factory rather than a value:
#: ``session_pack`` is a ``(SessionBuildContext) -> PackContribution`` the kernel
#: builder runs in one priority-ordered loop (``noeta.execution.session_pack``),
#: and ``control_tool`` is a
#: ``(ControlToolBuildContext) -> ControlToolMount | None`` it runs in the
#: post-tools dual-priority mount loop (``noeta.execution.control_tool``).
STANDARD_SURFACES: tuple[SurfaceSpec, ...] = (
    SurfaceSpec(
        "tool", "identity", "per-agent", _v_tool, "name",
        activation_binding="tool",
    ),
    SurfaceSpec(
        "agent", "identity", "per-agent", _v_agent, "name",
        activation_binding="agent",
    ),
    SurfaceSpec(
        "content_kind", "identity", "per-agent", _v_content_kind, "kind",
        activation_binding="content_kind",
    ),
    SurfaceSpec(
        "prompt_fragment", "identity", "per-agent", _v_str("prompt_fragment"),
        "name", activation_binding="prompt_fragment",
    ),
    SurfaceSpec(
        "policy", "identity", "per-agent", _v_present("policy"),
        "single-valued", activation_binding="policy",
    ),
    SurfaceSpec("guard", "wiring", "process", _v_present("guard"), "none"),
    SurfaceSpec("observer", "wiring", "process", _v_callable("observer"), "none"),
    SurfaceSpec(
        "provider", "wiring", "host-wired", _v_present("provider"), "single-valued"
    ),
    SurfaceSpec(
        "reminder_provider", "wiring", "per-agent", _v_callable("reminder_provider"),
        "name",
    ),
    SurfaceSpec(
        "reminder", "wiring", "per-agent", _v_callable("reminder"), "name", "priority"
    ),
    SurfaceSpec(
        "tool_result_transform", "wiring", "per-agent",
        _v_callable("tool_result_transform"), "name", "priority",
    ),
    SurfaceSpec(
        "mcp_server", "host", "host-wired", _v_present("mcp_server"), "alias"
    ),
    SurfaceSpec("skills", "host", "host-wired", _v_path, "none"),
    SurfaceSpec(
        "sandbox_provider", "host", "host-wired", _v_present("sandbox_provider"),
        "name",
    ),
    SurfaceSpec(
        "session_pack", "wiring", "per-agent", _v_callable("session_pack"),
        "name", "priority",
    ),
    # Identity-plane (it enters durable identity) but carried by the per-agent
    # ``activation_control_tools`` projection rather than ``PluginActivation`` —
    # hence the ``"elsewhere"`` binding.
    SurfaceSpec(
        "control_tool", "identity", "per-agent", _v_callable("control_tool"),
        "name", "priority", activation_binding="elsewhere",
    ),
)


def standard_registry() -> SurfaceRegistry:
    """A fresh registry seeded with the standard catalogue.

    A new registry every call, so a host that extends it (``reg =
    standard_registry(); reg.register(app_surface)``) never mutates a shared
    global.
    """
    registry = SurfaceRegistry()
    for spec in STANDARD_SURFACES:
        registry.register(spec)
    return registry
