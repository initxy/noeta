"""SDK built-in parts table.

This module is the **single source** for the built-in tool-name → class
mapping and the policy/composer ComponentRefs (roster removed; there is no
``noeta.agent.roster.specs`` mirror). The SDK owns these identity constants;
the product side consumes the compiled output via :mod:`noeta.client.options`
+ :mod:`noeta.presets`, with no second copy.

Keeping the table in ``noeta.client`` (rather than importing from ``noeta.agent``)
makes ``noeta-sdk`` self-contained so library users do not pull in the coding-agent
product package.
"""

from __future__ import annotations

import importlib
from dataclasses import MISSING, fields
from typing import Any, Callable, Mapping, Optional, Protocol, cast

from noeta.agent.spec import ComponentRef, ToolRef
from noeta.context.reminders import ReminderSpec
from noeta.execution.builder import CompactionConfig
from noeta.execution.session_pack import SessionPackEntry
from noeta.protocols.messages import Usage
from noeta.protocols.policy import Policy


__all__ = [
    "BUILTIN_TOOL_CLASSES",
    "COMPOSER_REF",
    "POLICY_REF",
    "ReactImpl",
    "builtin_tool_classes",
    "builtin_tool_ref",
    "browser_tool_names",
    "catalog_price",
    "default_app_tools_factory",
    "default_browser_tools_factory",
    "default_environment_kit",
    "default_guards_factory",
    "default_instructions_kit",
    "default_memory_index_kit",
    "default_policy_factory",
    "default_reminder_specs",
    "default_session_packs",
    "default_shell_rules",
    "derive_compaction_config",
    "mcp_impl",
    "memory_impl",
    "provider_family",
    "react_impl",
    "resolve_model_alias",
]


def _resolve_ref(ref: str) -> Any:
    """Resolve an explicit ``pkg.mod:attr`` manifest ref (microkernel M1).

    The same doorway the plugin loader uses — a dynamic import at the
    sanctioned execution boundary. SDK core keeps **no static edge** into the
    builtins band.
    """
    module_name, _, attr = ref.partition(":")
    obj: Any = importlib.import_module(module_name)
    for part in attr.split("."):
        obj = getattr(obj, part)
    return obj


_TOOL_CLASS_CACHE: Optional[dict[str, type]] = None


def builtin_tool_classes() -> dict[str, type]:
    """name → class for the default built-in tools, **loader-resolved**.

    Microkernel M1 (D2): SDK core holds no static tool table. The ``fs`` /
    ``web`` built-in plugin manifests declare the default 11 tools; this
    function reads those manifests (inert data) and resolves each ``tool``
    contribution's ref at the sanctioned execution boundary (client build /
    compile). Memoized — the classes are import-stable for the process.

    ``web_search`` is listed like every other name, but the *runtime pack*
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




# --- providers built-in accessors (microkernel M2) -------------------------
# The model catalog lives in the ``providers`` built-in plugin
# (``noeta.builtins.providers.impl.catalog``); SDK core reaches it only
# through the loader's dynamic-import doorway. The catalog module is
# import-stable for the process, so the resolution is memoized.

_CATALOG_MOD: Optional[Any] = None


def _catalog() -> Any:
    global _CATALOG_MOD
    if _CATALOG_MOD is None:
        _CATALOG_MOD = importlib.import_module(
            "noeta.builtins.providers.impl.catalog"
        )
    return _CATALOG_MOD


def derive_compaction_config(model: str) -> CompactionConfig:
    """Catalog-derived compaction knobs for ``model`` (loader-resolved).

    The SDK-side accessor for the ``providers`` built-in's
    ``derive_compaction_config`` — the value the kernel builder takes
    pre-resolved through ``build_session_inputs(compaction=…)``.
    """
    config: CompactionConfig = _catalog().derive_compaction_config(model)
    return config


def provider_family(model: str) -> Optional[str]:
    """The bound model's vendor family (loader-resolved catalog judgment).

    Injected into ``build_session_inputs(provider_family=…)`` for the
    edit↔apply_patch assembly mutex; ``None`` for any uncatalogued selector.
    """
    family: Optional[str] = _catalog().provider_family(model)
    return family


def default_memory_index_kit() -> Any:
    """The memory built-in's index kit (phase 2c).

    Renderer prose + hash rule + ``ContentKindSpec`` factory for the memory
    index resident, injected as the kernel builder's ``memory_index_kit``
    and consumed by the driver's pre-loop ``record_memory_index``.
    """
    build = _resolve_ref("noeta.builtins.memory.impl:build_memory_index_kit")
    return build()


def default_environment_kit() -> Any:
    """The workspace built-in's environment kit (phase 2c)."""
    build = _resolve_ref(
        "noeta.builtins.workspace.impl:build_environment_kit"
    )
    return build()


def default_instructions_kit() -> Any:
    """The workspace built-in's instructions kit (phase 2c).

    Carries the tag renderer / hash rule / kind factory AND the
    ``NOETA.md``/``AGENTS.md`` filename convention the kernel loader and
    the discovery hook walk.
    """
    build = _resolve_ref(
        "noeta.builtins.workspace.impl:build_instructions_kit"
    )
    return build()


def default_shell_rules() -> tuple[Any, ...]:
    """The fs built-in's curated shell allowlist (phase 2c).

    The host's approval predicate composes its effective allowlist from this
    base + host config + the project's remembered rules — the same table the
    ``shell_run`` tool enforces, so the two can never drift.
    """
    rules: tuple[Any, ...] = _resolve_ref(
        "noeta.builtins.fs.impl.shell_rules:DEFAULT_SHELL_RULES"
    )
    return rules




def catalog_price(model: str, usage: Usage) -> float:
    """USD for one round-trip's ``Usage`` (loader-resolved catalog pricing).

    The pricing callback the SDK host injects into ``RuntimeLLMClient``.
    """
    cost: float = _catalog().price(model, usage)
    return cost


def resolve_model_alias(selector: str) -> str:
    """Friendly alias → real model-id (loader-resolved catalog table).

    The alias resolver the SDK client injects into the
    ``InteractionDriver`` (identity for any non-alias selector).
    """
    resolved: str = _catalog().resolve_alias(selector)
    return resolved


# --- browser built-in accessors (microkernel M3) ---------------------------


def default_browser_tools_factory() -> Callable[..., Any]:
    """The browser tool pack factory for the kernel builder.

    Resolved from the ``browser`` built-in plugin's body
    (``noeta.builtins.browser.impl:build_browser_tools``) — the injection the
    microkernel builder requires (its ``browser_tools_factory`` param); the
    kernel itself imports no browser tool.
    """
    return _resolve_ref("noeta.builtins.browser.impl:build_browser_tools")


def browser_tool_names() -> tuple[str, ...]:
    """The five noeta-owned browser tool names (loader-resolved roster).

    The host's approval gating needs the fixed roster; it lives beside the
    pack in the browser built-in and is resolved through the same doorway.
    """
    names: tuple[str, ...] = _resolve_ref(
        "noeta.builtins.browser.impl:BROWSER_TOOL_NAMES"
    )
    return names


# --- app built-in accessor (microkernel M3) --------------------------------


def default_app_tools_factory() -> Callable[..., Any]:
    """The app-preview pack factory for the kernel builder.

    Resolved from the ``app`` built-in plugin's body
    (``noeta.builtins.app.impl:build_app_tools``) — the injection the
    microkernel builder requires (its ``app_tools_factory`` param); the
    kernel itself imports no tool implementation.
    """
    return _resolve_ref("noeta.builtins.app.impl:build_app_tools")


# --- mcp built-in accessor (microkernel M3) --------------------------------

_MCP_MOD: Optional[Any] = None


def mcp_impl() -> Any:
    """The ``mcp`` built-in's impl module, loader-resolved (memoized).

    SDK core reaches the connector implementation only through this doorway —
    ``build_mcp_tools`` / ``mcp_provenance_from_specs`` (the host's live-MCP
    path) hang off the returned module; the vocabulary (specs, errors,
    ``MCP_PREFIX``) is kernel-side in :mod:`noeta.runtime.mcp`.
    """
    global _MCP_MOD
    if _MCP_MOD is None:
        _MCP_MOD = importlib.import_module("noeta.builtins.mcp.impl")
    return _MCP_MOD


# --- memory built-in accessors (microkernel M3) ----------------------------

_MEMORY_MOD: Optional[Any] = None


def memory_impl() -> Any:
    """The ``memory`` built-in's impl module, loader-resolved (memoized).

    SDK core reaches the store / recall implementation only through this
    doorway — ``build_memory_pack`` / ``load_memory_store`` /
    ``memory_reminder_provider`` / the late-read
    ``DEFAULT_GLOBAL_MEMORY_DIR`` all hang off the returned module.
    """
    global _MEMORY_MOD
    if _MEMORY_MOD is None:
        _MEMORY_MOD = importlib.import_module("noeta.builtins.memory.impl")
    return _MEMORY_MOD




def default_policy_factory() -> Callable[..., Any]:
    """The default policy factory builder for the kernel builder
    (microkernel phase 2b).

    Resolved from the ``react`` built-in plugin's body
    (``noeta.builtins.react.impl:build_react_policy_factory``) — the
    injection the microkernel builder requires (its
    ``default_policy_factory`` param); the kernel itself imports no policy
    implementation. ``Options.policy`` / the plugin ``policy`` surface (D10)
    still override the default at the builder.
    """
    return _resolve_ref("noeta.builtins.react.impl:build_react_policy_factory")


class ReactImpl(Protocol):
    """The typed shape of the ``react`` built-in's impl module.

    A dynamic-import doorway returning ``Any`` would silently un-type every
    call site behind it — a renamed constructor kwarg in the built-in would
    keep mypy green and fail at runtime. Declaring the shape structurally
    restores the check without a static import, the same discipline the
    ``SkillsFactory`` / ``PolicyFactoryBuilder`` injections already use.

    Only what SDK core actually reaches through the doorway belongs here; the
    default policy construction goes through :func:`default_policy_factory`,
    not this Protocol.
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

    SDK core reaches the decision-mapping policy implementation only through
    this doorway — ``OrchestrationPolicy`` / ``StructuredOutputPolicy`` /
    ``WORKFLOW_SYSTEM_PROMPT`` (the host's workflow path) hang off the
    returned module, typed by :class:`ReactImpl`. The cast is where the
    dynamic import meets the static contract; ``tests/test_react_doorway.py``
    pins the module against it so the two cannot drift apart silently.
    """
    global _REACT_MOD
    if _REACT_MOD is None:
        _REACT_MOD = cast(
            ReactImpl, importlib.import_module("noeta.builtins.react.impl")
        )
    return _REACT_MOD




# NOTE: there was a ``skills_impl()`` module doorway here, mirroring
# :func:`react_impl`. Nothing ever called it: SDK core reaches the skills
# built-in through :func:`default_skills_kit_factory` alone — the kit is the
# whole interface, so no consumer needs the module object. Deleted rather than
# left exported, where it would read as the sanctioned path.


def default_guards_factory() -> Callable[..., Any]:
    """The default guard-stack factory for the kernel builder.

    Resolved from the ``governance`` built-in plugin's body
    (``noeta.builtins.governance.impl:build_default_guards``) — the injection
    the microkernel builder requires (its ``guards_factory`` param); the
    kernel itself imports no guard implementation.
    """
    return _resolve_ref("noeta.builtins.governance.impl:build_default_guards")


_SESSION_PACK_CACHE: dict[frozenset[str], tuple[SessionPackEntry, ...]] = {}


def default_session_packs(
    *, disabled: frozenset[str] = frozenset()
) -> tuple[SessionPackEntry, ...]:
    """The built-in ``session_pack`` entries, **loader-resolved** and ordered.

    Microkernel phase 3: the built-in manifests declare their
    session-construction factories (ref + priority); this function reads
    those manifests (inert data), resolves each ``session_pack``
    contribution at the sanctioned execution boundary, and returns the
    :class:`SessionPackEntry` tuple sorted ``(priority, plugin, name)`` —
    the ONE injection the kernel builder's generic pack loop requires
    (replacing the per-feature factory accessors). ``disabled`` drops a
    built-in's pack entirely (``disabled_builtins=["skills"]``), the honest
    expression of a turned-off capability. Memoized per disabled-set — the
    factories are pure module-level functions, import-stable for the process.
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


_REMINDER_SPEC_CACHE: Optional[tuple[ReminderSpec, ...]] = None


def default_reminder_specs() -> tuple[ReminderSpec, ...]:
    """The three built-in compose-time reminders, **loader-resolved**.

    Microkernel M2 (D2): the ``reminders`` built-in plugin's manifest declares
    the three renders (ref + priority); this function reads that manifest
    (inert data) and resolves each ``reminder`` contribution at the sanctioned
    execution boundary, returning the :class:`ReminderSpec` tuple the kernel
    builder requires as its ``base_reminders`` injection. Memoized — the
    renders are pure module-level functions, import-stable for the process.
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


def __getattr__(name: str) -> Any:
    # Compatibility surface: ``BUILTIN_TOOL_CLASSES`` stays importable as a
    # module attribute, now backed by the loader-resolved table.
    if name == "BUILTIN_TOOL_CLASSES":
        return builtin_tool_classes()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


#: ReAct decision-mapping behaviour version — SDK single source
#: (``roster.specs._REACT_POLICY`` removed).
POLICY_REF = ComponentRef("react", "1")
#: Three-segment context composer version — SDK single source
#: (``roster.specs._THREE_SEGMENT_COMPOSER`` removed).
COMPOSER_REF = ComponentRef("three_segment", "v3")


def _field_default(cls: type, field_name: str) -> Any:
    """Return the static dataclass-field default of ``cls.field_name``.

    SDK single implementation (``roster.specs._field_default`` removed).
    Raises ``TypeError`` if the field has no static default (callers would
    otherwise silently get a ``MISSING`` sentinel into the AgentSpec identity,
    which is the bug this guard prevents).
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
        If ``name`` is not in :data:`BUILTIN_TOOL_CLASSES`. The message
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
