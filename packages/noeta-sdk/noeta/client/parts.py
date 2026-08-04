"""The SDK's parts table: built-in identity constants and loader doorways.

Single source for the built-in tool-name → class mapping, the policy/composer
ComponentRefs, and every accessor that reaches a built-in plugin. Each of those
accessors resolves its target by dynamic import — SDK core holds no static edge
into the builtins band, so a host pays only for the capabilities it uses and a
built-in's internals never become part of the compile surface.
"""

from __future__ import annotations

import importlib
from dataclasses import MISSING, fields
from typing import Any, Callable, Optional, Protocol, cast

from noeta.agent.spec import ComponentRef, ToolRef
from noeta.context.reminders import ReminderSpec
from noeta.execution.builder import CompactionConfig
from noeta.execution.control_tool import ControlToolEntry
from noeta.execution.session_pack import SessionPackEntry
from noeta.protocols.messages import Usage
from noeta.protocols.policy import Policy


__all__ = [
    "COMPOSER_REF",
    "POLICY_REF",
    "ReactImpl",
    "builtin_tool_classes",
    "builtin_tool_ref",
    "browser_tool_names",
    "catalog_price",
    "default_guards_factory",
    "default_policy_factory",
    "default_control_tools",
    "default_reminder_specs",
    "default_session_packs",
    "default_shell_rules",
    "workspace_impl",
    "derive_compaction_config",
    "mcp_impl",
    "memory_impl",
    "provider_family",
    "react_impl",
    "resolve_model_alias",
]


def _resolve_ref(ref: str) -> Any:
    """Resolve an explicit ``pkg.mod:attr`` manifest ref.

    The same doorway the plugin loader uses: a dynamic import at the sanctioned
    execution boundary, keeping SDK core free of static edges into builtins.
    """
    module_name, _, attr = ref.partition(":")
    obj: Any = importlib.import_module(module_name)
    for part in attr.split("."):
        obj = getattr(obj, part)
    return obj


_TOOL_CLASS_CACHE: Optional[dict[str, type]] = None


def builtin_tool_classes() -> dict[str, type]:
    """name → class for the default built-in tools, **loader-resolved**.

    Read from the ``fs`` / ``web`` manifests (inert data) and resolved at the
    sanctioned execution boundary, so there is no static tool table to drift
    from what the plugins actually declare. Memoized: import-stable per process.

    ``web_search`` appears here like every other name, but the runtime pack
    constructs it only when ``NOETA_WEB_SEARCH_API_KEY`` is set — the
    whitelist's intersection with the built pack drops it otherwise, so the
    model never sees a search tool it cannot use.
    """
    global _TOOL_CLASS_CACHE
    if _TOOL_CLASS_CACHE is None:
        builtins_mod = importlib.import_module("noeta.builtins")
        out: dict[str, type] = {}
        for manifest in builtins_mod.builtin_manifests():
            if manifest.name not in ("fs", "web"):
                continue
            for c in manifest.contributions:
                if c.surface != "tool" or c.ref is None:
                    continue
                out[c.name] = _resolve_ref(c.ref)
        _TOOL_CLASS_CACHE = out
    return _TOOL_CLASS_CACHE




# The model catalog lives in the ``providers`` built-in; the resolution is
# memoized because the module is import-stable for the process.

_CATALOG_MOD: Optional[Any] = None


def _catalog() -> Any:
    global _CATALOG_MOD
    if _CATALOG_MOD is None:
        _CATALOG_MOD = importlib.import_module(
            "noeta.builtins.providers.impl.catalog"
        )
    return _CATALOG_MOD


def derive_compaction_config(model: str) -> CompactionConfig:
    """Catalog-derived compaction knobs for ``model``, loader-resolved.

    The kernel builder takes this pre-resolved via
    ``build_session_inputs(compaction=…)``; it reads no catalog itself.
    """
    config: CompactionConfig = _catalog().derive_compaction_config(model)
    return config


def provider_family(model: str) -> Optional[str]:
    """The bound model's vendor family; ``None`` for an uncatalogued selector.

    Injected into ``build_session_inputs(provider_family=…)`` so assembly
    stages can branch on the bound vendor family when they must.
    """
    family: Optional[str] = _catalog().provider_family(model)
    return family


_WORKSPACE_MOD: Optional[Any] = None


def workspace_impl() -> Any:
    """The ``workspace`` built-in's impl module, loader-resolved (memoized).

    The doorway for the host's own record path (``load_environment`` /
    ``load_instructions``); the compose path goes through the plugin's
    ``session_pack`` contributions instead.
    """
    global _WORKSPACE_MOD
    if _WORKSPACE_MOD is None:
        _WORKSPACE_MOD = importlib.import_module(
            "noeta.builtins.workspace.impl"
        )
    return _WORKSPACE_MOD


def default_shell_rules() -> tuple[Any, ...]:
    """The ``fs`` built-in's curated shell allowlist.

    The host's approval predicate composes its effective allowlist from this
    base plus host config plus the project's remembered rules — the same table
    the ``shell_run`` tool enforces, so the two can never drift.
    """
    rules: tuple[Any, ...] = _resolve_ref(
        "noeta.builtins.fs.impl.shell_rules:DEFAULT_SHELL_RULES"
    )
    return rules




def catalog_price(model: str, usage: Usage) -> float:
    """USD for one round-trip's ``Usage``; injected into ``RuntimeLLMClient``."""
    cost: float = _catalog().price(model, usage)
    return cost


def resolve_model_alias(selector: str) -> str:
    """Friendly alias → real model id; identity for any non-alias selector."""
    resolved: str = _catalog().resolve_alias(selector)
    return resolved


def browser_tool_names() -> tuple[str, ...]:
    """The browser built-in's tool names, for the host's approval gating.

    The list lives beside the pack that builds those tools, so gating and
    construction cannot disagree about what the browser owns.
    """
    names: tuple[str, ...] = _resolve_ref(
        "noeta.builtins.browser.impl:BROWSER_TOOL_NAMES"
    )
    return names


_MCP_MOD: Optional[Any] = None


def mcp_impl() -> Any:
    """The ``mcp`` built-in's impl module, loader-resolved (memoized).

    Carries the connector implementation (``build_mcp_tools`` /
    ``mcp_provenance_from_specs``); the vocabulary — specs, errors,
    ``MCP_PREFIX`` — is kernel-side in :mod:`noeta.runtime.mcp`.
    """
    global _MCP_MOD
    if _MCP_MOD is None:
        _MCP_MOD = importlib.import_module("noeta.builtins.mcp.impl")
    return _MCP_MOD


_MEMORY_MOD: Optional[Any] = None


def memory_impl() -> Any:
    """The ``memory`` built-in's impl module, loader-resolved (memoized).

    Carries the store and recall implementation: ``build_memory_pack``,
    ``load_memory_store``, ``memory_reminder_provider``, and the late-read
    ``DEFAULT_GLOBAL_MEMORY_DIR``.
    """
    global _MEMORY_MOD
    if _MEMORY_MOD is None:
        _MEMORY_MOD = importlib.import_module("noeta.builtins.memory.impl")
    return _MEMORY_MOD




def default_policy_factory() -> Callable[..., Any]:
    """The default policy factory builder the kernel builder requires.

    Resolved from the ``react`` built-in so the kernel imports no policy
    implementation of its own. ``Options.policy`` and the plugin ``policy``
    surface override it at the builder.
    """
    return _resolve_ref("noeta.builtins.react.impl:build_react_policy_factory")


class ReactImpl(Protocol):
    """The typed shape of the ``react`` built-in's impl module.

    A doorway returning ``Any`` would silently un-type every call site behind
    it: a changed constructor kwarg in the built-in would keep mypy green and
    fail at runtime. Declaring the shape structurally restores the check
    without a static import. Only what SDK core reaches through the doorway
    belongs here — default policy construction goes through
    :func:`default_policy_factory` instead.
    """

    WORKFLOW_SYSTEM_PROMPT: str

    def OrchestrationPolicy(  # noqa: N802 — mirrors the built-in's class name
        self, *, script: str, args: dict[str, Any]
    ) -> Policy: ...

    def StructuredOutputPolicy(  # noqa: N802 — mirrors the built-in's class name
        self, *, inner: Policy, schema: dict[str, Any]
    ) -> Policy: ...


_REACT_MOD: Optional[ReactImpl] = None


def react_impl() -> ReactImpl:
    """The ``react`` built-in's impl module, loader-resolved (memoized).

    The cast is where the dynamic import meets the static contract;
    ``tests/test_react_doorway.py`` pins the real module against
    :class:`ReactImpl` so the two cannot drift apart silently.
    """
    global _REACT_MOD
    if _REACT_MOD is None:
        _REACT_MOD = cast(
            ReactImpl, importlib.import_module("noeta.builtins.react.impl")
        )
    return _REACT_MOD


def default_guards_factory() -> Callable[..., Any]:
    """The default guard-stack factory the kernel builder requires.

    Resolved from the ``governance`` built-in so the kernel imports no guard
    implementation of its own.
    """
    return _resolve_ref("noeta.builtins.governance.impl:build_default_guards")


_SESSION_PACK_CACHE: dict[frozenset[str], tuple[SessionPackEntry, ...]] = {}


def default_session_packs(
    *, disabled: frozenset[str] = frozenset()
) -> tuple[SessionPackEntry, ...]:
    """The built-in ``session_pack`` entries, **loader-resolved** and ordered.

    The one injection the kernel builder's generic pack loop takes: each
    manifest declares a factory ref plus a priority, and the tuple comes back
    sorted ``(priority, plugin, name)`` so construction order is a manifest
    fact rather than a call-site one. ``disabled`` drops a built-in's pack
    entirely — the honest expression of a turned-off capability. Memoized per
    disabled-set; the factories are import-stable module-level functions.
    """
    cached = _SESSION_PACK_CACHE.get(disabled)
    if cached is None:
        builtins_mod = importlib.import_module("noeta.builtins")
        keyed: list[tuple[int, str, str, SessionPackEntry]] = []
        for manifest in builtins_mod.builtin_manifests():
            if manifest.name in disabled:
                continue
            for c in manifest.contributions:
                if c.surface != "session_pack" or c.ref is None:
                    continue
                priority = c.params.get("priority")
                if not isinstance(priority, int):
                    raise RuntimeError(
                        f"built-in session pack {c.name!r} declares no "
                        f"integer priority — the manifest must carry the "
                        f"construction order"
                    )
                keyed.append(
                    (
                        priority,
                        manifest.name,
                        c.name,
                        SessionPackEntry(
                            name=c.name,
                            priority=priority,
                            factory=_resolve_ref(c.ref),
                        ),
                    )
                )
        keyed.sort(key=lambda t: (t[0], t[1], t[2]))
        cached = tuple(entry for _p, _pl, _n, entry in keyed)
        _SESSION_PACK_CACHE[disabled] = cached
    return cached


_CONTROL_TOOL_CACHE: dict[frozenset[str], tuple[ControlToolEntry, ...]] = {}


def default_control_tools(
    *, disabled: frozenset[str] = frozenset()
) -> tuple[ControlToolEntry, ...]:
    """The built-in ``control_tool`` entries, **loader-resolved** and ordered.

    The mirror of :func:`default_session_packs` for the ``control_tool``
    surface: each manifest declares a factory ref plus a priority, and the tuple
    comes back sorted ``(priority, plugin, name)`` so the mount loop's iteration
    order is a manifest fact rather than a call-site one. ``disabled`` drops a
    built-in's entry entirely. Memoized per disabled-set; the factories are
    import-stable module-level functions.
    """
    cached = _CONTROL_TOOL_CACHE.get(disabled)
    if cached is None:
        builtins_mod = importlib.import_module("noeta.builtins")
        keyed: list[tuple[int, str, str, ControlToolEntry]] = []
        for manifest in builtins_mod.builtin_manifests():
            if manifest.name in disabled:
                continue
            for c in manifest.contributions:
                if c.surface != "control_tool" or c.ref is None:
                    continue
                priority = c.params.get("priority")
                if not isinstance(priority, int):
                    raise RuntimeError(
                        f"built-in control tool {c.name!r} declares no "
                        f"integer priority — the manifest must carry the "
                        f"mount-loop iteration order"
                    )
                keyed.append(
                    (
                        priority,
                        manifest.name,
                        c.name,
                        ControlToolEntry(
                            name=c.name,
                            priority=priority,
                            factory=_resolve_ref(c.ref),
                        ),
                    )
                )
        keyed.sort(key=lambda t: (t[0], t[1], t[2]))
        cached = tuple(entry for _p, _pl, _n, entry in keyed)
        _CONTROL_TOOL_CACHE[disabled] = cached
    return cached


_REMINDER_SPEC_CACHE: Optional[tuple[ReminderSpec, ...]] = None


def default_reminder_specs() -> tuple[ReminderSpec, ...]:
    """The built-in compose-time reminders, **loader-resolved**.

    The ``reminders`` manifest declares each render (ref + priority) as inert
    data; resolving it here at the sanctioned execution boundary gives the
    kernel builder its ``base_reminders`` injection without a static edge into
    builtins. Memoized; the renders are import-stable module-level functions.
    """
    global _REMINDER_SPEC_CACHE
    if _REMINDER_SPEC_CACHE is None:
        builtins_mod = importlib.import_module("noeta.builtins")
        specs: list[ReminderSpec] = []
        for manifest in builtins_mod.builtin_manifests():
            if manifest.name != "reminders":
                continue
            for c in manifest.contributions:
                if c.surface != "reminder" or c.ref is None:
                    continue
                priority = c.params.get("priority")
                if not isinstance(priority, int):
                    raise RuntimeError(
                        f"built-in reminder {c.name!r} declares no integer "
                        f"priority — the manifest must carry the composed order"
                    )
                specs.append(
                    ReminderSpec(
                        name=c.name,
                        priority=priority,
                        render=_resolve_ref(c.ref),
                    )
                )
        _REMINDER_SPEC_CACHE = tuple(specs)
    return _REMINDER_SPEC_CACHE


#: ReAct decision-mapping behaviour version.
POLICY_REF = ComponentRef("react", "1")
#: Three-segment context composer version.
COMPOSER_REF = ComponentRef("three_segment", "v3")


def _field_default(cls: type, field_name: str) -> Any:
    """Return the static dataclass-field default of ``cls.field_name``.

    Raises ``TypeError`` when the field has no static default: the alternative
    is a ``MISSING`` sentinel travelling silently into the AgentSpec identity.
    """
    for f in fields(cls):
        if f.name == field_name:
            if f.default is MISSING:
                raise TypeError(
                    f"{cls.__name__}.{field_name} has no static default; "
                    f"cannot read tool identity metadata without instantiation"
                )
            return f.default
    raise AttributeError(f"{cls.__name__} has no field {field_name!r}")


def builtin_tool_ref(name: str) -> ToolRef:
    """Return a :class:`ToolRef` for the built-in tool ``name``.

    ``version`` is hard-coded to ``"1"`` (the SDK-wide convention for
    built-in tools; a bump in tool behaviour should add a new name or
    bump the component refs). ``risk_level`` is read straight off the tool
    class' static default, so a class-level change in risk surfaces in the
    compiled ``AgentSpec``.

    Raises
    ------
    KeyError
        If ``name`` is not in :func:`builtin_tool_classes`. The message
        enumerates the valid names.
    """
    classes = builtin_tool_classes()
    if name not in classes:
        available = ", ".join(sorted(classes))
        raise KeyError(
            f"Unknown built-in tool {name!r}. Available: {available}"
        )
    cls = classes[name]
    return ToolRef(
        name=name,
        version="1",
        risk_level=str(_field_default(cls, "risk_level")),
    )
