"""``Options`` — the human-friendly recipe type + pure :func:`compile_options`.

Part of the SDK public face (slice 4a, D2/D6) and surface alignment
(Claude Agent SDK shape). ``Options`` is **only** a recipe:
lightweight, mutable-feeling on the surface (though frozen for hashability),
written by a library user. :func:`compile_options` turns it into a frozen
:class:`~noeta.agent.spec.AgentSpec` + flat list of descendant ``AgentSpec`` s
that a runtime host can register.

Design notes:

* ``Options`` is *identity-layer sugar*. Compilation is **additive** — it
  fills in SDK defaults (policy=react, composer=three_segment) rather than
  overriding user intent. If the caller supplies a ``capabilities`` we keep
  it verbatim; only ``spawnable`` is unioned with the ``agents`` dict's keys
  so a parent that forgot to list a child's name does not silently drop the
  delegation right.
* ``provider`` / ``workspace_dir`` / storage wiring live on
  ``Options`` as **optional fallbacks** (D5: identity vs host binding).
  :func:`compile_options` and the identity path **completely ignore**
  them; :class:`~noeta.client.client.Client` constructor kwargs take
  precedence when supplied.
* :func:`compile_options` is **pure**: no registry mutation, no side
  effects, deterministic identity for equal inputs. Registration is
  left to ``Client`` (slice 4b).
* Bare ``Options`` (no explicit tool fields) defaults to the full
  built-in tool set (``BUILTIN_TOOL_CLASSES`` — the 11 tools read/glob/grep,
  edit/write/apply_patch, shell_run/shell_poll/shell_kill, webfetch/web_search),
  matching Claude Agent SDK's "agent gets every tool" default. To opt out, set
  ``allowed_tools=()``.
* Child agents are declared via the flat ``agents: dict[str, AgentDefinition]``
  (Claude Agent SDK shape). There is **no recursive nesting** — deep trees
  must be expressed by declaring every agent at the top level and using the
  ``Capabilities.spawnable`` mechanism to wire delegation paths.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from noeta.agent.spec import (
    AgentSpec,
    BudgetSpec,
    Capabilities,
    ComponentRef,
    ToolRef,
)
from noeta.client.parts import (
    BUILTIN_TOOL_CLASSES,
    COMPOSER_REF,
    POLICY_REF,
    builtin_tool_ref,
)
from noeta.context.content_channel import ContentKindSpec
from noeta.protocols.event_log import Subscriber
from noeta.protocols.hooks import Guard
from noeta.protocols.messages import LLMProvider


__all__ = [
    "AgentDefinition",
    "Options",
    "SystemPromptPreset",
    "compile_options",
    "register_preset_prompt",
]


# ---------------------------------------------------------------------------
# Preset-prompt registry (populated by a later batch)
# ---------------------------------------------------------------------------


_PRESET_PROMPTS: dict[str, str] = {}
"""Named system-prompt presets.

Populated by :func:`register_preset_prompt`. Referenced by
:class:`SystemPromptPreset` at compile time.
"""


def register_preset_prompt(name: str, prompt: str) -> None:
    """Register a named system-prompt preset.

    Subsequent ``SystemPromptPreset(preset=name)`` references will resolve
    to ``prompt``. If ``name`` was already registered the new value
    silently overwrites the old (last-writer-wins, consistent with
    :class:`Options` being a recipe-layer convenience).
    """
    _PRESET_PROMPTS[name] = prompt


# ---------------------------------------------------------------------------
# New surface types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SystemPromptPreset:
    """Reference a named system-prompt preset, with optional suffix append.

    Parameters
    ----------
    preset:
        Name of a preset previously registered via
        :func:`register_preset_prompt`. The default ``"main"`` is the
        convention for the official coding-agent preset (populated by
        the next issue / slice).
    append:
        Extra text appended (after ``"\n\n"``) to the resolved preset
        prompt. ``None`` ⇒ no suffix.
    """

    preset: str = "main"
    append: str | None = None


@dataclass(frozen=True)
class AgentDefinition:
    """Flat, non-recursive child-agent recipe (Claude Agent SDK shape).

    Unlike the legacy ``Options.subagents`` tuple, ``AgentDefinition``
    **cannot nest** — it has no ``agents`` / ``subagents`` field. Deep
    trees must be expressed by declaring every agent at the top level and
    using the ``Capabilities.spawnable`` mechanism.

    Parameters
    ----------
    description:
        Short human-readable description; required (empty or whitespace-only
        raises ``ValueError`` at compile time). Carried into the child's
        ``AgentSpec.metadata["description"]`` so UI surfaces can show it.
    prompt:
        Child agent's instructions / system prompt. Mapped to
        ``AgentSpec.instructions`` verbatim. Required.
    tools:
        Tool list for this child. ``None`` ⇒ every built-in tool
        (``BUILTIN_TOOL_CLASSES``). Same mixed-entry shape as
        :attr:`Options.allowed_tools`: strings for built-in names, or
        ``DecoratedTool`` instances exposing a ``.ref`` property.
    model:
        Preferred model id for this child. ``None`` ⇒ host default.
        Excluded from identity (matches ``Options.model``
        semantics).
    capabilities:
        **Advanced field.** Child-agent behaviour capabilities, peer to
        :attr:`Options.capabilities`. Note: a child ``AgentDefinition``'s
        capabilities do **not** get the ``spawnable`` union — children are
        flat leaves and should not delegate further. ``None`` ⇒ compiler
        fills in ``Capabilities()`` (all False, empty spawnable).
    metadata:
        Extra observational labels, merged into the child's
        ``AgentSpec.metadata`` (``description`` is written automatically by
        this recipe — keys here cannot override its meaning but may add
        others). **Wiring-layer, excluded from identity** (peer to
        ``Options.metadata``) — used as a host-binding hint slot; e.g. a spec
        can pass ``{"write_path_globs": "plans/*.md"}``
        to tell the host to inject a path allow-list into ``write`` without
        affecting the spec's identity.
        Defaults to an empty dict.
    """

    description: str
    prompt: str
    tools: tuple[Any, ...] | None = None
    model: str | None = None
    capabilities: Optional[Capabilities] = None
    metadata: Mapping[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Options recipe dataclass
# ---------------------------------------------------------------------------


_PERMISSION_MODES = frozenset(
    {"default", "acceptEdits", "bypassPermissions"}
)
"""Legal values for :attr:`Options.permission_mode`.

Three modes: default / acceptEdits / bypassPermissions (plan removed).
"""


_EFFORT_MODES = frozenset({"low", "medium", "high", "xhigh", "max"})
"""Legal values for :attr:`Options.effort` (reasoning-effort override).

The single source of truth for the effort enum: ``__post_init__`` validates
against it, and ``noeta.client.capabilities.effort_modes`` projects it for the
app's ``/capabilities`` composer dropdown.
"""


@dataclass(frozen=True)
class Options:
    """Human-friendly recipe for compiling one or more :class:`AgentSpec` s.

    Parameters
    ----------
    system_prompt:
        The agent's instructions. Either a plain string (used verbatim) or
        a :class:`SystemPromptPreset` reference (resolved at compile time
        against the preset registry). Required.
    name:
        Stable agent name. Mapped to ``AgentSpec.name``. The default ``"main"``
        is the convention for single-agent recipes; multi-agent recipes should
        give each subagent a distinct name (a duplicate raises ``ValueError``
        at compile time).
    skills:
        Skill names; each is wrapped as ``ComponentRef(name, version="1")``.
    budget:
        Default budget caps. ``None`` ⇒ :class:`BudgetSpec` with
        ``max_subtask_depth=3`` (runaway-recursion guard).
    capabilities:
        Behaviour-shaping capabilities surfaced to the policy. ``None`` ⇒ the
        compiler fills in ``delegation=bool(children)`` and
        ``spawnable=tuple(child names)``. When supplied explicitly, the
        caller's values are kept as-is except ``spawnable`` is **unioned**
        with the inline child names.
    model:
        Preferred LLM model id. A host routing hint — excluded from
        identity.
    metadata:
        Observational labels. Also excluded from identity.
    provider:
        Optional LLM provider. **Wiring, not identity (D5)** — completely
        ignored by :func:`compile_options` and identity.
    agents:
        Flat dict of ``name → AgentDefinition`` (Claude Agent SDK shape).
        Compiled into top-level descendant ``AgentSpec`` s. The parent's
        ``capabilities.spawnable`` is unioned with these names.
    allowed_tools:
        Explicit tool allow-list. Entries may be built-in tool name
        strings or ``DecoratedTool`` instances (anything with a ``.ref``
        returning :class:`ToolRef`). ``None`` ⇒ **all 11 built-in tools**
        (D2 default; ``BUILTIN_TOOL_CLASSES``). Empty tuple ⇒ no tools.
    disallowed_tools:
        Tool names (by :class:`ToolRef.name`) to subtract from the parsed
        allow-list. Names that are not present are silently ignored.
    permission_mode:
        Permission-gating strategy. Must be one of ``"default"``,
        ``"acceptEdits"``, ``"bypassPermissions"`` (three modes; plan
        removed). Currently validated only; runtime wiring is a later slice.
    max_turns:
        Syntactic sugar for ``budget.max_iterations``. Setting both
        ``budget.max_iterations`` and ``max_turns`` raises ``ValueError``
        (ambiguous).
    cwd:
        Optional working directory hint (``str | Path | None``). Purely
        wiring-layer — **explicitly excluded from identity**
        (same treatment as ``provider`` and ``metadata``).
        :func:`compile_options` never inspects it so two otherwise-equal
        ``Options`` differing only in ``cwd`` share an identity.
    can_use_tool:
        Optional per-tool-call callback used to auto-approve or -deny a
        gated tool call before the task suspends waiting for a human.
        Signature ``(tool_name: str, arguments: dict) -> bool``: return
        ``True`` to let the call through, ``False`` to refuse it. When
        the callback decides, its resolution is recorded as a
        ``ToolCallApprovalResolved`` event with ``resolver="can_use_tool"``
        so the audit trail matches a manual approval. ``None`` (the
        default) disables the auto-resolver — gated calls suspend
        normally. **Purely wiring-layer** — :func:`compile_options` and
        the identity path completely ignore it, matching the treatment
        of ``provider`` and ``cwd``. Two otherwise-equal ``Options`` that
        differ only in ``can_use_tool`` share an identity.
    output_schema:
        Optional JSON Schema describing the shape of the final answer.
        When set, the LLM is instructed to emit structured JSON matching
        this schema and the resulting ``FinishDecision.answer`` is
        deserialized to a Python dict/list on success (invalid JSON
        falls back to the raw text so a task never fails purely on
        parsing). **Purely wiring-layer** — completely ignored by
        :func:`compile_options` and identity, matching the
        treatment of ``provider``/``cwd``/``can_use_tool``. Two
        otherwise-equal ``Options`` differing only in ``output_schema``
        share an identity. Must be a ``Mapping`` (e.g. ``dict``) when
        not ``None``.
    thinking:
        Optional reasoning-mode override: ``"adaptive"`` or
        ``"disabled"``. ``None`` (the default) means no override — the
        provider's default applies. **Purely wiring-layer** — excluded
        from identity, never inspected by
        :func:`compile_options`. Invalid values raise ``ValueError`` at
        construction time.
    effort:
        Optional reasoning-effort override. Valid values: ``"low"``,
        ``"medium"``, ``"high"``, ``"xhigh"``, ``"max"``. ``None``
        (the default) means provider-default. **Purely wiring-layer** —
        excluded from identity, never inspected by
        :func:`compile_options`. Invalid values raise ``ValueError`` at
        construction time.
    policy:
        **Extension point.** A custom decision policy that replaces the
        default ReAct brain. Must be a callable ``(llm) -> Policy`` carrying
        a ``.ref`` property returning a :class:`~noeta.agent.spec.ComponentRef`
        (its identity). ``None`` ⇒ the built-in ReAct policy
        (``ComponentRef("react", "1")``). **Identity-bearing** — the custom
        ref enters the ``AgentSpec`` so a swapped brain is a distinct agent.
    guards:
        **Extension point.** Custom :class:`~noeta.protocols.hooks.Guard`
        instances (synchronous allow/deny/approve checks) registered after the
        built-in guard stack. **Wiring-layer** — excluded from identity.
    observers:
        **Extension point.** Post-commit event subscribers
        (``Callable[[EventEnvelope], None]``) the :class:`Client` subscribes
        alongside the defaults and tears down on ``shutdown``.
        **Wiring-layer** — excluded from identity.
    content_channels:
        **Extension point.** Custom
        :class:`~noeta.context.content_channel.ContentKindSpec` channels
        appended after the built-in content residents. This is the **only**
        composer extension seam (the composer itself is not replaceable —
        stable-prefix cache hard constraint). **Wiring-layer** — excluded from
        identity (the in-process resume re-supplies the same channels).
    mcp_servers:
        **Extension point.** In-process MCP servers built by
        :func:`noeta.sdk.create_sdk_mcp_server`; each exposes a bundle of
        ``@tool`` functions. Their tools are added to the agent's tool set
        (so they enter identity, like any other declared tool) and wired as
        runnable closures.

    Multi-turn / resume: a multi-turn conversation is driven through the
    :class:`Client` verbs (``send_goal`` / ``reopen``), not an ``Options``
    flag — the in-process ``Client`` holds the live task. Durable cross-process
    resume is a host/storage concern (host config, not ``Options``).
    """

    system_prompt: str | SystemPromptPreset
    name: str = "main"
    skills: tuple[str, ...] = ()
    budget: Optional[BudgetSpec] = None
    capabilities: Optional[Capabilities] = None
    model: Optional[str] = None
    metadata: Mapping[str, str] = field(default_factory=dict)
    provider: Optional[LLMProvider] = None
    agents: Mapping[str, AgentDefinition] = field(default_factory=dict)
    allowed_tools: tuple[Any, ...] | None = None
    disallowed_tools: tuple[str, ...] = ()
    permission_mode: str = "default"
    max_turns: int | None = None
    cwd: object = None
    can_use_tool: object = None
    output_schema: Optional[Mapping[str, Any]] = None
    thinking: Optional[str] = None
    effort: Optional[str] = None
    # --- (T3) extension points -----
    policy: Optional[Any] = None
    guards: tuple[Guard, ...] = ()
    observers: tuple[Subscriber, ...] = ()
    content_channels: tuple[ContentKindSpec, ...] = ()
    mcp_servers: tuple[Any, ...] = ()
    #: Runtime LLM provider. This is a wiring (host-binding) concern — D5 — and
    #: is **explicitly excluded** from identity. See docstring.
    #: ``cwd``, ``can_use_tool``, ``output_schema``, ``thinking`` and
    #: ``effort`` are treated identically (wiring, not identity).

    def __post_init__(self) -> None:
        if self.thinking is not None and self.thinking not in (
            "adaptive",
            "disabled",
        ):
            raise ValueError(
                f"Options.thinking must be 'adaptive', 'disabled', or None; "
                f"got {self.thinking!r}"
            )
        if self.effort is not None and self.effort not in _EFFORT_MODES:
            raise ValueError(
                f"Options.effort must be one of "
                f"{tuple(sorted(_EFFORT_MODES))} or None; "
                f"got {self.effort!r}"
            )
        if self.output_schema is not None and not isinstance(
            self.output_schema, Mapping
        ):
            raise ValueError(
                "Options.output_schema must be a Mapping (e.g. dict) or None; "
                f"got {type(self.output_schema).__name__}"
            )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _compile_tool(entry: object) -> ToolRef:
    """Resolve one tool entry (string or .ref-bearing object) into a :class:`ToolRef`."""
    if hasattr(entry, "ref"):
        ref = entry.ref
        if not isinstance(ref, ToolRef):
            raise TypeError(
                f"tool entry .ref returned {type(ref).__name__}, "
                f"expected ToolRef"
            )
        return ref
    if isinstance(entry, str):
        return builtin_tool_ref(entry)
    raise TypeError(
        f"Unsupported tool entry of type {type(entry).__name__!r}: "
        f"expected DecoratedTool (or any object with a .ref property "
        f"returning a ToolRef) or a built-in tool name string"
    )


def _resolve_policy_ref(policy: object) -> ComponentRef:
    """Resolve a custom ``Options.policy`` provider into its identity ref.

    The provider must expose a ``.ref`` property returning a
    :class:`ComponentRef` (the policy's identity). Anything else raises
    ``TypeError`` so a misconfigured custom policy fails loudly at compile
    time rather than minting an agent with a guessed identity.
    """
    ref = getattr(policy, "ref", None)
    if not isinstance(ref, ComponentRef):
        raise TypeError(
            "Options.policy must expose a `.ref` property returning a "
            f"ComponentRef; got {type(policy).__name__} with "
            f"ref={type(ref).__name__}"
        )
    return ref


def _mcp_server_tool_entries(mcp_servers: tuple[Any, ...]) -> tuple[Any, ...]:
    """Flatten the in-process ``mcp_servers`` (SdkMcpServer) into tool entries.

    Each server is duck-typed by its ``.tools`` attribute (the SDK
    ``SdkMcpServer`` value object) — keeping ``noeta.client`` free of an
    upward import on ``noeta.sdk`` where ``SdkMcpServer`` is defined.
    """
    entries: list[Any] = []
    for server in mcp_servers:
        entries.extend(getattr(server, "tools", ()))
    return tuple(entries)


def _resolve_system_prompt(sp: str | SystemPromptPreset) -> str:
    """Resolve ``Options.system_prompt`` into a concrete instructions string.

    Plain strings pass through. :class:`SystemPromptPreset` looks up
    ``_PRESET_PROMPTS[preset]``; a missing preset raises ``ValueError``
    enumerating the registered names.
    """
    if isinstance(sp, str):
        return sp
    if sp.preset not in _PRESET_PROMPTS:
        registered = ", ".join(sorted(_PRESET_PROMPTS)) or "(none)"
        raise ValueError(
            f"Unknown system-prompt preset {sp.preset!r}. "
            f"Registered presets: {registered}. "
            f"Use register_preset_prompt(name, prompt) to register one."
        )
    base = _PRESET_PROMPTS[sp.preset]
    if sp.append is not None:
        return base + "\n\n" + sp.append
    return base


def _compile_tool_list(
    entries: tuple[Any, ...],
    disallowed: tuple[str, ...],
) -> tuple[ToolRef, ...]:
    """Parse each entry, drop names in ``disallowed``, de-duplicate preserving order."""
    seen_names: set[str] = set()
    out: list[ToolRef] = []
    disallowed_set = set(disallowed)
    for entry in entries:
        ref = _compile_tool(entry)
        if ref.name in disallowed_set:
            continue
        if ref.name in seen_names:
            continue
        seen_names.add(ref.name)
        out.append(ref)
    return tuple(out)


def _capabilities_for(
    explicit: Optional[Capabilities], subagent_names: tuple[str, ...]
) -> Capabilities:
    """Resolve :class:`Capabilities` per the D2 additive rule.

    Rule: if the caller gave an explicit ``capabilities`` we keep every
    flag as-is, but *union* ``spawnable`` with the inline subagent names
    so a subagent the caller declared but forgot to list in ``spawnable``
    is still delegatable. ``delegation`` is **not** forced on — if the
    caller explicitly set ``delegation=False`` we respect it.

    If no explicit capabilities are given we synthesise them:
    ``delegation=bool(agents)`` and ``spawnable`` = all child names.
    """
    if explicit is None:
        return Capabilities(
            delegation=bool(subagent_names),
            spawnable=subagent_names,
        )

    existing = set(explicit.spawnable)
    for n in subagent_names:
        existing.add(n)
    new_spawnable = tuple(sorted(existing))
    if new_spawnable == explicit.spawnable:
        return explicit
    return dataclasses.replace(explicit, spawnable=new_spawnable)


# ---------------------------------------------------------------------------
# compile_options
# ---------------------------------------------------------------------------


def compile_options(
    options: Options,
) -> tuple[AgentSpec, tuple[AgentSpec, ...]]:
    """Pure-compile an :class:`Options` recipe into ``(main, descendants)``.

    The function is **referentially transparent**: equal ``Options`` inputs
    produce equal ``AgentSpec`` s (structural equality — the agents resolve and
    bind identically).

    Parameters
    ----------
    options:
        The top-level recipe to compile.

    Returns
    -------
    tuple[AgentSpec, tuple[AgentSpec, ...]]
        ``main_spec`` is the top-level agent. ``descendants`` is a flat list
        of every agent declared via ``options.agents`` (no recursive nesting
        — see the module-level docstring).
    """
    # -- Seed the global name set ------------------------------------------
    seen_names: set[str] = set()
    seen_names.add(options.name)
    descendant_specs: list[AgentSpec] = []

    # -- permission_mode validation ----------------------------------------
    if options.permission_mode not in _PERMISSION_MODES:
        legal = ", ".join(sorted(_PERMISSION_MODES))
        raise ValueError(
            f"Invalid permission_mode {options.permission_mode!r}. "
            f"Must be one of: {legal}."
        )

    # -- 1. Compile the flat `agents` dict -----------------------
    def _compile_defn_tools(defn_tools: tuple[Any, ...] | None) -> tuple[ToolRef, ...]:
        """Shared helper: resolve an AgentDefinition.tools field
        (``None`` = full built-in set, same default as the main Options)."""
        if defn_tools is None:
            base = tuple(sorted(BUILTIN_TOOL_CLASSES))
        else:
            base = defn_tools
        return _compile_tool_list(base, ())

    agent_defn_names: list[str] = []
    for agent_name, defn in sorted(options.agents.items()):
        # description non-empty check
        if not defn.description or not defn.description.strip():
            raise ValueError(
                f"AgentDefinition for {agent_name!r} has empty or "
                f"whitespace-only `description` — a non-blank description "
                f"is required."
            )
        if agent_name in seen_names:
            raise ValueError(
                f"Duplicate subagent name {agent_name!r} — each `agents` "
                f"dict key must be distinct and must not collide with the "
                f"root agent name."
            )
        seen_names.add(agent_name)
        agent_defn_names.append(agent_name)

        child_tools = _compile_defn_tools(defn.tools)
        child_caps = defn.capabilities if defn.capabilities is not None else Capabilities()
        # description is recipe-owned; extra wiring labels (e.g.
        # ``write_path_globs``) merge UNDER it so a defn can never silently
        # clobber the description the registry / UI reads. metadata is
        # identity-excluded, so this never shifts a spec's identity.
        child_metadata: dict[str, str] = {
            **dict(defn.metadata),
            "description": defn.description,
        }
        child_spec = AgentSpec(
            name=agent_name,
            instructions=defn.prompt,
            policy=POLICY_REF,
            composer=COMPOSER_REF,
            tools=child_tools,
            skills=(),
            default_budget=BudgetSpec(max_subtask_depth=3),
            capabilities=child_caps,  # flat children: defn.capabilities verbatim, no spawnable union
            metadata=child_metadata,
            default_model=defn.model,
        )
        descendant_specs.append(child_spec)

    all_child_names = tuple(sorted(agent_defn_names))

    # -- 2. Resolve system_prompt -------------------------------------------
    instructions = _resolve_system_prompt(options.system_prompt)

    # -- 3. Resolve tools (replacement branch only) ----------------------------
    # Replacement semantics (D4): allowed_tools=None ⇒ full built-in set;
    # any tuple ⇒ exactly those. In-process MCP servers (Options.mcp_servers)
    # contribute their tools on top of whichever base applies — they are an
    # explicit, separate source, so they are added even under a replacement
    # allow-list.
    if options.allowed_tools is None:
        base = tuple(sorted(BUILTIN_TOOL_CLASSES))
    else:
        base = options.allowed_tools
    tool_entries = tuple(base) + _mcp_server_tool_entries(options.mcp_servers)
    tool_refs = _compile_tool_list(tool_entries, options.disallowed_tools)

    # -- 3b. Resolve the decision policy ref (identity) ---------------------
    policy_ref = (
        _resolve_policy_ref(options.policy)
        if options.policy is not None
        else POLICY_REF
    )

    # -- 4. Resolve skills --------------------------------------------------
    skill_refs = tuple(ComponentRef(name=s) for s in options.skills)

    # -- 5. Resolve capabilities (additive union with child names) ----------
    caps = _capabilities_for(options.capabilities, all_child_names)

    # -- 6. Budget + max_turns merging --------------------------------------
    if options.budget is None:
        budget = BudgetSpec(max_subtask_depth=3)
    else:
        budget = options.budget

    if options.max_turns is not None:
        if budget.max_iterations is not None:
            raise ValueError(
                "Both `budget.max_iterations` and `max_turns` are set — "
                "they express the same iteration cap and cannot be "
                "supplied together (ambiguous)."
            )
        budget = dataclasses.replace(budget, max_iterations=options.max_turns)

    # -- 7. Build main spec -------------------------------------------------
    main = AgentSpec(
        name=options.name,
        instructions=instructions,
        policy=policy_ref,
        composer=COMPOSER_REF,
        tools=tool_refs,
        skills=skill_refs,
        default_budget=budget,
        capabilities=caps,
        metadata=dict(options.metadata),
        default_model=options.model,
    )

    return main, tuple(descendant_specs)
