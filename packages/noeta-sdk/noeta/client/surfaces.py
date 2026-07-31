"""Surface registry — the generality mechanism (spec D2 / D3).

A :class:`SurfaceSpec` describes one extension surface: which plane it lives on,
how a contribution to it is *validated*, how contributions *collide*, how they
*merge*, and how they *order*. The loader (:mod:`noeta.client.plugin_set`) is
**surface-agnostic** — it consults one :class:`SurfaceRegistry` and nothing
else, so adding a future surface is registering one ``SurfaceSpec``, not
changing the loader.

:func:`standard_registry` seeds the standard catalogue (D3 — all sixteen
surfaces). A host may :meth:`SurfaceRegistry.register` additional app-plane
surfaces on a copy **before** load; the same validation / collision / ordering
pipeline runs over them unchanged — and, since D11, so does the identity
projection: a registered identity surface names the ``PluginActivation``
channel it feeds (:data:`ActivationBinding`) and reaches ``compile_options``
with no loader edit.

This is the *mechanism-core* module of the manifest-plugin redesign. The 0.4.0
contribution-bundle mechanism it replaced is gone; ``noeta.client.plugins``
retains only the trust store and the shared error surface. This module imports
only value types (``AgentDefinition`` / ``ContentKindSpec``) and the shared
:class:`~noeta.client.plugins.PluginError`, so it introduces no new layering
edge.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal

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


#: A surface's plane, mirroring the ``Options`` / ``HostConfig`` split (D2/D3).
Plane = Literal["identity", "wiring", "host"]

#: How a surface's effect is scoped across agents in the process (D6).
ActivationScope = Literal["per-agent", "process", "host-wired"]

#: The namespace a contribution's key lives in — a human label for collision
#: messages and the switch that decides whether two contributions can clash.
#: ``single-valued`` means at most one across the whole loaded set; ``none``
#: means the surface never collides (guards / observers / skill dirs).
CollisionKey = Literal["name", "kind", "alias", "single-valued", "none"]

#: How merged contributions order. ``sorted`` = ``(plugin, name)``; ``priority``
#: = an integer ``priority`` param first, ties broken by ``(plugin, name)``
#: (the guard-observer-hooks precedent).
Ordering = Literal["sorted", "priority"]

# NOTE: a ``merge_rule`` field ("append" / "single") used to sit beside these.
# Nothing ever read it — the append-vs-single behaviour is fully determined by
# ``collision_key`` (``single-valued`` is the single-merge surface), so the
# field was pure decoration promising a mechanism the loader did not implement.
# Deleted with the D11 pass rather than left as documentation of a lie.

#: How an **identity-plane** contribution binds into the per-plugin
#: :class:`~noeta.client.options.PluginActivation` handed to
#: ``compile_options`` — the table-driven successor of the surface-name
#: chain ``PluginSet.identity_activations`` used to hardcode (D11). The
#: first five values name the ``PluginActivation`` channels (a closed set:
#: the compile contract owns them); ``"elsewhere"`` marks an identity-plane
#: surface that is deliberately NOT carried at compile because its own
#: per-agent projection resolves it (``control_tool`` rides
#: ``activation_control_tools``). An identity surface that declares NO
#: binding fails projection loudly — the ``content_kind``-went-missing
#: lesson: a value silently dropped between resolve and compile compiles
#: the wrong identity.
ActivationBinding = Literal[
    "tool", "agent", "content_kind", "prompt_fragment", "policy", "elsewhere"
]


@dataclass(frozen=True)
class SurfaceSpec:
    """One extension surface, fully described so the loader stays generic.

    ``validator`` is called on a **resolved** contribution value (after a
    manifest ``ref`` has been imported) and must raise — a :class:`PluginError`
    is preferred — when the value is not a legal member of the surface. It is
    the D2 "what a legal contribution value is" slot; listing and manifest-level
    collision never call it (they run without executing plugin code).
    """

    name: str
    plane: Plane
    activation_scope: ActivationScope
    validator: Callable[[Any], None]
    collision_key: CollisionKey
    ordering: Ordering = "sorted"
    #: How an identity-plane contribution binds into ``PluginActivation``
    #: (D11). Required for ``plane="identity"`` — the loader's projection
    #: dispatches on it instead of matching surface names, so a
    #: host-registered identity surface projects without a loader edit.
    #: ``None`` for every wiring / host surface (they project through the
    #: per-agent parameter channels instead).
    activation_binding: ActivationBinding | None = None

    def __post_init__(self) -> None:
        # An identity surface with no binding would be silently dropped
        # between resolve and compile — the ``content_kind``-went-missing
        # failure. Catch it at registration, not at projection time.
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


class SurfaceRegistry:
    """The surface catalogue the loader consults — one name → one ``SurfaceSpec``.

    Deliberately small: register, look up, list. A host extends the standard set
    by taking a :meth:`copy` of :func:`standard_registry` and registering its
    own app-plane surfaces before calling ``load_plugins``.
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
        """The spec for ``name``, or a :class:`PluginError` naming the unknown surface."""
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
        """Every registered surface name, sorted."""
        return tuple(sorted(self._by_name))

    def copy(self) -> "SurfaceRegistry":
        """A shallow, independent copy — the seam a host extends before load."""
        clone = SurfaceRegistry()
        clone._by_name = dict(self._by_name)
        return clone


# ---------------------------------------------------------------------------
# Validators — light structural checks (they tighten as later milestones wire
# each surface's runtime shape; a legal-value gate that never executes plugin
# code lives here, the runtime contract lives in the surface's own milestone).
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
    # A built-in tool name string, or a ``.ref``-bearing / callable tool object.
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
# The standard catalogue (D3) — sixteen surfaces.
# ---------------------------------------------------------------------------


#: The sixteen standard surfaces, in the D3 table order. ``★`` surfaces are new
#: in this redesign; their runtime wiring lands in M3/M4, but the mechanism
#: (registry entry + validation + collision + ordering) is complete here.
#: ``session_pack`` (microkernel phase 3) is the session-construction surface:
#: a factory ``(SessionBuildContext) -> PackContribution`` the kernel builder
#: runs in one priority-ordered loop — see ``noeta.execution.session_pack``.
#: ``control_tool`` (control-tool-surface S1) is the control-tool-construction
#: surface: a factory ``(ControlToolBuildContext) -> ControlToolMount | None``
#: the kernel builder runs in the post-tools dual-priority mount loop — see
#: ``noeta.execution.control_tool``.
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
    # Declared identity-plane (it enters durable identity in S3) but carried by
    # the per-agent ``activation_control_tools`` projection, not by
    # ``PluginActivation`` — hence the ``"elsewhere"`` binding.
    SurfaceSpec(
        "control_tool", "identity", "per-agent", _v_callable("control_tool"),
        "name", "priority", activation_binding="elsewhere",
    ),
)


def standard_registry() -> SurfaceRegistry:
    """A fresh registry seeded with the standard catalogue (D3).

    A new registry every call, so a host that extends it (``reg =
    standard_registry(); reg.register(app_surface)``) never mutates a shared
    global. All sixteen standard surfaces are present.
    """
    registry = SurfaceRegistry()
    for spec in STANDARD_SURFACES:
        registry.register(spec)
    return registry
