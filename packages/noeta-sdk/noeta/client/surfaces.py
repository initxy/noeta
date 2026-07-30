"""Surface registry — the generality mechanism (spec D2 / D3).

A :class:`SurfaceSpec` describes one extension surface: which plane it lives on,
how a contribution to it is *validated*, how contributions *collide*, how they
*merge*, and how they *order*. The loader (:mod:`noeta.client.plugin_set`) is
**surface-agnostic** — it consults one :class:`SurfaceRegistry` and nothing
else, so adding a future surface is registering one ``SurfaceSpec``, not
changing the loader.

:func:`standard_registry` seeds the standard catalogue (D3 — all fifteen
surfaces). A host may :meth:`SurfaceRegistry.register` additional app-plane
surfaces on a copy **before** load; the same validation / collision / ordering
pipeline runs over them unchanged.

This is the *mechanism-core* module of the manifest-plugin redesign; it is built
additively next to the still-live 0.4.0 ``noeta.client.plugins`` module (which
M2 retires). It imports only value types (``AgentDefinition`` / ``ContentKindSpec``)
and the shared :class:`~noeta.client.plugins.PluginError`, so it introduces no
new layering edge.
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
    "CollisionKey",
    "MergeRule",
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

#: How merged contributions combine into the target.
MergeRule = Literal["append", "single", "dict-merge"]

#: How merged contributions order. ``sorted`` = ``(plugin, name)``; ``priority``
#: = an integer ``priority`` param first, ties broken by ``(plugin, name)``
#: (the guard-observer-hooks precedent).
Ordering = Literal["sorted", "priority"]


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
    merge_rule: MergeRule
    ordering: Ordering = "sorted"


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
# The standard catalogue (D3) — fifteen surfaces.
# ---------------------------------------------------------------------------


#: The fifteen standard surfaces, in the D3 table order. ``★`` surfaces are new
#: in this redesign; their runtime wiring lands in M3/M4, but the mechanism
#: (registry entry + validation + collision + ordering) is complete here.
#: ``session_pack`` (microkernel phase 3) is the session-construction surface:
#: a factory ``(SessionBuildContext) -> PackContribution`` the kernel builder
#: runs in one priority-ordered loop — see ``noeta.execution.session_pack``.
STANDARD_SURFACES: tuple[SurfaceSpec, ...] = (
    SurfaceSpec("tool", "identity", "per-agent", _v_tool, "name", "append"),
    SurfaceSpec("agent", "identity", "per-agent", _v_agent, "name", "append"),
    SurfaceSpec(
        "content_kind", "identity", "per-agent", _v_content_kind, "kind", "append"
    ),
    SurfaceSpec(
        "prompt_fragment", "identity", "per-agent", _v_str("prompt_fragment"),
        "name", "append",
    ),
    SurfaceSpec(
        "policy", "identity", "per-agent", _v_present("policy"),
        "single-valued", "single",
    ),
    SurfaceSpec("guard", "wiring", "process", _v_present("guard"), "none", "append"),
    SurfaceSpec(
        "observer", "wiring", "process", _v_callable("observer"), "none", "append"
    ),
    SurfaceSpec(
        "provider", "wiring", "host-wired", _v_present("provider"),
        "single-valued", "single",
    ),
    SurfaceSpec(
        "reminder_provider", "wiring", "per-agent", _v_callable("reminder_provider"),
        "name", "append",
    ),
    SurfaceSpec(
        "reminder", "wiring", "per-agent", _v_callable("reminder"),
        "name", "append", "priority",
    ),
    SurfaceSpec(
        "tool_result_transform", "wiring", "per-agent",
        _v_callable("tool_result_transform"), "name", "append", "priority",
    ),
    SurfaceSpec(
        "mcp_server", "host", "host-wired", _v_present("mcp_server"),
        "alias", "append",
    ),
    SurfaceSpec("skills", "host", "host-wired", _v_path, "none", "append"),
    SurfaceSpec(
        "sandbox_provider", "host", "host-wired", _v_present("sandbox_provider"),
        "name", "append",
    ),
    SurfaceSpec(
        "session_pack", "wiring", "per-agent", _v_callable("session_pack"),
        "name", "append", "priority",
    ),
)


def standard_registry() -> SurfaceRegistry:
    """A fresh registry seeded with the standard catalogue (D3).

    A new registry every call, so a host that extends it (``reg =
    standard_registry(); reg.register(app_surface)``) never mutates a shared
    global. All fifteen standard surfaces are present.
    """
    registry = SurfaceRegistry()
    for spec in STANDARD_SURFACES:
        registry.register(spec)
    return registry
