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
  overriding user intent. ``delegation`` / ``spawnable`` are derived
  structurally from the ``agents`` dict keys, so a parent never silently drops
  a child's delegation right. Reserved ``__``-prefixed names are the one
  exception:
  they compile into the registry but stay out of the auto-union (internal,
  host-driven identities — see :func:`compile_options`).
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
  must be expressed by declaring every agent at the top level; the compiled
  ``AgentSpec.capabilities.spawnable`` (derived from the ``agents`` keys) wires
  the delegation paths.
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
    COMPOSER_REF,
    POLICY_REF,
    builtin_tool_classes,
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
    "PluginActivation",
    "DEFAULT_PLUGINS",
    "compile_options",
    "effective_root_policy",
    "register_preset_prompt",
]


# ---------------------------------------------------------------------------
# Activation vocabulary (spec D5 / D6)
# ---------------------------------------------------------------------------


#: The pinned default activation set for a bare ``Options()`` — the ``fs`` and
#: ``web`` built-in tool packs. Both are **identity-inert** in compilation (the
#: default 11-tool set still comes from ``BUILTIN_TOOL_CLASSES`` so a bare
#: ``Options()`` is byte-identical — the parity golden pins this), so listing
#: them here documents the conceptual default without perturbing the compiled
#: ``AgentSpec``. Memory / browser stay off, matching the parity contract.
DEFAULT_PLUGINS: tuple[str, ...] = ("fs", "web")


#: Built-in feature-bundle activation names that map onto a ``Capabilities``
#: identity flag (D5: ``memory=True`` becomes ``plugins=["memory"]`` etc.). These
#: are the runtime's own stable feature vocabulary; activating one flips the
#: matching identity flag and nothing else — the tool / prompt wiring those
#: bundles imply is already carried by the (unchanged) capability-gated engine
#: build and preset prompt baking, so activation stays byte-identity-safe.
_ACTIVATION_CAPABILITY_FLAG: Mapping[str, str] = {
    "todo_write": "todo_write",
    "ask_user_question": "ask_user_question",
    "skill_invocation": "skill_invocation",
    "memory": "memory",
    "mcp": "mcp",
    "browser": "browser",
    # The one structural capability that is ALSO authorable. ``delegation`` is
    # normally derived (a root with children delegates, a flat child does not),
    # but the derivation alone leaves no way to give a child the right to spawn —
    # the job the retired ``AgentDefinition.capabilities`` did. Activating it is
    # additive: it can turn delegation ON, never off (see _capabilities_from).
    "delegation": "delegation",
}

#: Built-in activation names that carry **no** identity effect in compilation
#: (their runtime contributions are host-wired or land in a later milestone).
#: Recognised so a typo in the activation list still fails loudly.
_INERT_BUILTIN_ACTIVATIONS: frozenset[str] = frozenset(
    {"fs", "web", "skills", "reminders", "governance", "providers", "presets", "sandbox"}
)

#: Every recognised built-in activation name (no plugin code executes to know
#: them — they are the SDK's own feature vocabulary).
#:
#: This is a superset of the built-in plugin **catalogue**
#: (``noeta.builtins.BUILTIN_PLUGIN_NAMES``): the catalogue names the plugins
#: that ship declarations, this set additionally carries the capability flags
#: that have no catalogue entry (``todo_write`` / ``mcp`` / …). The catalogue
#: sits ABOVE ``noeta.client`` in the import bands, so this module cannot read
#: it; ``noeta.builtins`` closes the loop instead by asserting the containment at
#: import (``_assert_activation_vocabulary``), which is what catches a new
#: built-in that nobody added here.
BUILTIN_ACTIVATIONS: frozenset[str] = (
    frozenset(_ACTIVATION_CAPABILITY_FLAG) | _INERT_BUILTIN_ACTIVATIONS
)

#: Backwards-compatible private alias (this module's own call sites).
_BUILTIN_ACTIVATIONS = BUILTIN_ACTIVATIONS


@dataclass(frozen=True)
class PluginActivation:
    """The identity-plane contributions one **loaded external plugin** carries.

    Built by :class:`~noeta.client.client.Client` from a resolved
    :class:`~noeta.client.plugin_set.PluginSet` and passed to
    :func:`compile_options`, so ``noeta.client.options`` never imports the
    loader (which imports it — the edge would be a cycle). Built-in feature
    bundles are handled by name inside :func:`compile_options`; this type carries
    only the *third-party* identity contributions that **follow activation**
    (D6): extra tools, extra child agents, extra prompt fragments. Wiring-plane
    contributions (guard / observer / provider / mcp / skills / sandbox) are not
    here — they never follow per-agent activation.
    """

    #: Tool entries (built-in name strings or ``.ref``-bearing tool objects).
    tools: tuple[Any, ...] = ()
    #: ``(agent_name, AgentDefinition)`` child agents.
    agents: tuple[tuple[str, "AgentDefinition"], ...] = ()
    #: ``ContentKindSpec`` semi-stable residents, in contribution-name order. The
    #: ``Client`` appends these to the activating agent's content channels.
    content_kinds: tuple[Any, ...] = ()
    #: ``(contribution_name, text)`` prompt fragments, appended after the prompt.
    prompt_fragments: tuple[tuple[str, str], ...] = ()
    #: Extra ``Capabilities`` flags this plugin's activation forces on. There is
    #: no manifest surface for these, so ``PluginSet.identity_activations()``
    #: never sets them — the field exists for a caller that drives
    #: :func:`compile_options` directly and wants an activation to imply a
    #: capability flag (a host expressing its own feature bundle, say). Names must
    #: be :class:`~noeta.agent.spec.Capabilities` field names.
    capability_flags: tuple[str, ...] = ()
    #: The single ``policy`` contribution this plugin carries (D10), a
    #: ``(llm) -> Policy`` factory exposing a ``.ref`` — or ``None``. Combined
    #: with the base ``Options.policy`` at compile: a base plus an active plugin
    #: policy, or two active plugin policies, is a loud single-valued collision.
    policy: Optional[Any] = None


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
    trees must be expressed by declaring every agent at the top level; the
    compiled ``AgentSpec.capabilities.spawnable`` (derived from the ``agents``
    keys) wires the delegation paths.

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
    #: **Activation (D5).** Names of loaded plugins this child agent activates —
    #: built-in feature bundles (``"memory"`` / ``"skill_invocation"`` /
    #: ``"browser"`` …) or third-party plugin names. Enters identity: activating
    #: a feature bundle flips the matching capability flag, an external plugin
    #: contributes its identity-plane tools / agents / prompt fragments. Peer to
    #: :attr:`Options.plugins` but with no ``fs`` / ``web`` default (a child's
    #: tools come from :attr:`tools`).
    plugins: tuple[str, ...] = ()
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
    plugins:
        **Activation (D5).** Names of loaded plugins this agent activates —
        the successor to the retired ``capabilities=`` field. Built-in feature
        bundles (``"memory"`` / ``"skill_invocation"`` / ``"browser"`` …) map
        onto the compiled ``AgentSpec.capabilities`` identity flags; third-party
        plugin names pull in that plugin's identity-plane contributions. The
        compiler always fills in ``delegation=bool(children)`` and
        ``spawnable=tuple(child names)`` structurally. Defaults to
        :data:`DEFAULT_PLUGINS`.
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
    #: **Activation (D5).** Names of loaded plugins this agent activates. Built-in
    #: feature bundles map onto identity capability flags (``plugins=["memory"]``
    #: is the successor to ``Capabilities(memory=True)``); third-party plugin
    #: names pull in that plugin's identity-plane contributions. Defaults to
    #: :data:`DEFAULT_PLUGINS` (``fs`` / ``web`` — identity-inert, so a bare
    #: ``Options()`` compiles byte-identically). Unknown names fail compilation
    #: loudly (built-in vocabulary + the loaded ``PluginSet`` handed to
    #: ``Client``).
    plugins: tuple[str, ...] = DEFAULT_PLUGINS
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


def _append_fragments(
    prompt: str, activations: tuple[tuple[str, PluginActivation], ...]
) -> str:
    """Append external plugin prompt fragments after ``prompt`` (D6 / D10).

    Fragments are ordered ``(plugin, contribution name)`` across all activated
    plugins and joined after a blank-line separator, mirroring how the presets
    bake the memory-policy fragment. No activation ⇒ ``prompt`` unchanged
    (byte-identity for the preset / parity path, which uses only built-in
    bundles that contribute no fragment through this channel).
    """
    frags = sorted(
        (plugin, name, text)
        for plugin, act in activations
        for name, text in act.prompt_fragments
    )
    if not frags:
        return prompt
    return prompt + "\n\n" + "\n\n".join(text for _plugin, _name, text in frags)


def _resolve_effective_policy(
    base_policy: object,
    external: tuple[tuple[str, PluginActivation], ...],
    *,
    where: str,
) -> object:
    """The single decision-policy factory for an agent (D10; single-valued).

    Combines the base ``Options.policy`` with the ``policy`` contribution of each
    activated external plugin. Zero ⇒ ``None`` (the built-in ReAct policy). More
    than one — a base plus an active plugin policy, or two active plugin
    policies — is a loud collision naming **both** sides (the same rule as
    ``provider``); there is no override.
    """
    sources: list[tuple[str, object]] = []
    if base_policy is not None:
        sources.append((f"{where}.policy", base_policy))
    for plugin, act in external:
        if act.policy is not None:
            sources.append((f"active plugin {plugin!r}", act.policy))
    if len(sources) > 1:
        both = " and ".join(label for label, _ in sources)
        raise ValueError(
            f"policy is single-valued on {where} but supplied by {both} — "
            f"no override (D10)"
        )
    return sources[0][1] if sources else None


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


def _merge_plugin_tools(
    base: tuple[ToolRef, ...],
    external: tuple[tuple[str, PluginActivation], ...],
    disallowed: tuple[str, ...],
    *,
    where: str,
) -> tuple[ToolRef, ...]:
    """Append the activated plugins' tools to ``base``, loudly on any conflict (D4).

    Three outcomes that used to be silent drops are errors here, because each one
    leaves an activated plugin whose tool simply does not exist at runtime while
    every listing still reports it as contributed:

    * a plugin tool whose name is already taken (by a built-in, by the caller's
      own ``allowed_tools``, or by another activated plugin) — named on both sides;
    * a plugin tool named in ``disallowed_tools`` — activation and subtraction
      contradict each other, and guessing which the caller meant is not our call;
    * (implicitly) two plugins contributing the same tool name, which
      ``PluginSet.merged()`` also rejects at load.
    """
    owner: dict[str, str] = {ref.name: where for ref in base}
    disallowed_set = set(disallowed)
    out = list(base)
    for plugin, act in external:
        for entry in act.tools:
            ref = _compile_tool(entry)
            if ref.name in disallowed_set:
                raise ValueError(
                    f"active plugin {plugin!r} contributes the tool {ref.name!r}, "
                    f"which {where} also lists in `disallowed_tools` — activation "
                    f"and subtraction contradict; drop one"
                )
            prior = owner.get(ref.name)
            if prior is not None:
                raise ValueError(
                    f"tool {ref.name!r} on {where} is contributed by both {prior} "
                    f"and active plugin {plugin!r} — no override"
                )
            owner[ref.name] = f"active plugin {plugin!r}"
            out.append(ref)
    return tuple(out)


def _merge_plugin_agents(
    base: Mapping[str, "AgentDefinition"],
    external: tuple[tuple[str, PluginActivation], ...],
) -> dict[str, "AgentDefinition"]:
    """Fold the activated plugins' child agents into the base roster (no override).

    A plugin whose child agent shadows one the caller declared would silently
    re-point every ``spawn_subagent`` at the plugin's implementation, so a clash
    with the base ``Options.agents`` — or between two activated plugins — names
    both contributors and fails.
    """
    merged: dict[str, AgentDefinition] = dict(base)
    owner: dict[str, str] = {name: "Options.agents" for name in base}
    for plugin, act in external:
        for name, defn in act.agents:
            prior = owner.get(name)
            if prior is not None:
                raise ValueError(
                    f"child agent {name!r} is contributed by both {prior} and "
                    f"active plugin {plugin!r} — no override"
                )
            owner[name] = f"active plugin {plugin!r}"
            merged[name] = defn
    return merged


def _resolve_activation(
    activation: tuple[str, ...],
    plugins: Optional[Mapping[str, PluginActivation]],
    *,
    where: str,
) -> tuple[frozenset[str], tuple[tuple[str, PluginActivation], ...]]:
    """Split an activation list into capability flags + external contributions.

    Every name must be a recognised built-in activation (:data:`_BUILTIN_ACTIVATIONS`)
    or the name of a loaded plugin in ``plugins`` — anything else raises
    ``ValueError`` naming both the offending name and ``where`` (spec D5: unknown
    activation fails compilation loudly). Built-in feature bundles resolve to the
    matching identity flag (or to nothing, for the identity-inert bundles);
    external plugin names resolve to a ``(plugin name, PluginActivation)`` pair
    so downstream ordering (prompt fragments / policy collisions) can name the
    contributing plugin.
    """
    flags: set[str] = set()
    external: list[tuple[str, PluginActivation]] = []
    for name in activation:
        flag = _ACTIVATION_CAPABILITY_FLAG.get(name)
        if flag is not None:
            flags.add(flag)
        elif name in _INERT_BUILTIN_ACTIVATIONS:
            continue
        elif plugins is not None and name in plugins:
            act = plugins[name]
            external.append((name, act))
            flags.update(act.capability_flags)
        else:
            known = ", ".join(sorted(_BUILTIN_ACTIVATIONS))
            loaded = (
                ", ".join(sorted(plugins)) if plugins else "<none loaded>"
            )
            raise ValueError(
                f"unknown plugin activation {name!r} on {where} — not a built-in "
                f"activation ({known}) and not in the loaded plugin set "
                f"({loaded}). Load it before activating, or fix the name."
            )
    return frozenset(flags), tuple(external)


def _capabilities_from(
    flags: frozenset[str],
    subagent_names: tuple[str, ...],
    *,
    derive_delegation: bool,
) -> Capabilities:
    """Fold activation flags + the inline child roster into identity ``Capabilities``.

    ``flags`` are the capability names the activation forces on (built-in
    feature bundles plus any external plugin ``capability_flags``).
    ``spawnable`` is always the sorted child roster — activation cannot name an
    agent, so the ``agents`` dict stays its only authoring path (spec D5/D6).

    ``delegation`` is normally structural (``derive_delegation`` — the root gets
    it from having children; a flat child does not), but the
    :data:`_ACTIVATION_CAPABILITY_FLAG` entry can additionally turn it ON. The
    combination is a union, never an override: a root with children delegates
    whether or not it activated the flag, and a child that activated it delegates
    despite having no inline roster of its own (the successor to the retired
    ``AgentDefinition.capabilities=Capabilities(delegation=True)``).
    """
    kw = {flag: True for flag in flags}
    seeded = dataclasses.replace(Capabilities(), **kw) if kw else Capabilities()

    spawnable = tuple(sorted(set(seeded.spawnable) | set(subagent_names)))
    delegation = seeded.delegation or (bool(subagent_names) if derive_delegation else False)
    if delegation == seeded.delegation and spawnable == seeded.spawnable:
        return seeded
    return dataclasses.replace(seeded, delegation=delegation, spawnable=spawnable)


# ---------------------------------------------------------------------------
# compile_options
# ---------------------------------------------------------------------------


def effective_root_policy(
    options: Options,
    plugins: Optional[Mapping[str, PluginActivation]] = None,
) -> object:
    """The single ``(llm) -> Policy`` factory a root agent runs (D10), or ``None``.

    The ``Client`` wires this as the host's process-wide ``policy_override`` — the
    *runtime* half of the policy surface, the twin of the identity ``.ref``
    :func:`compile_options` bakes into the ``AgentSpec``. Same single-valued
    collision rule (a base ``Options.policy`` plus an active plugin policy, or two
    active plugin policies, fails loudly).
    """
    _flags, external = _resolve_activation(options.plugins, plugins, where="Options")
    return _resolve_effective_policy(options.policy, external, where="Options")


def compile_options(
    options: Options,
    *,
    plugins: Optional[Mapping[str, PluginActivation]] = None,
) -> tuple[AgentSpec, tuple[AgentSpec, ...]]:
    """Pure-compile an :class:`Options` recipe into ``(main, descendants)``.

    The function is **referentially transparent**: equal ``Options`` inputs
    produce equal ``AgentSpec`` s (structural equality — the agents resolve and
    bind identically).

    Parameters
    ----------
    options:
        The top-level recipe to compile.
    plugins:
        The identity-plane contributions of the loaded **external** plugins,
        keyed by plugin name (spec D5). ``Client`` builds this from a resolved
        :class:`~noeta.client.plugin_set.PluginSet`. ``None`` (the parity path /
        a bare compile) means only built-in feature-bundle activations are
        recognised — any other activation name fails loudly. Activation is
        identity-affecting; wiring-plane plugin effects (guard / observer /
        provider / …) do not pass through here.

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

    # -- 0. Resolve the root agent's activation (D5) ------------------------
    # Built-in feature bundles fold into capability flags; external plugin names
    # pull in their identity-plane contributions (extra tools / child agents /
    # prompt fragments). Presets activate only built-in bundles, so `root_external`
    # is empty and the compiled bytes are byte-identical to the pre-redesign
    # `capabilities=` recipe form (the parity contract).
    root_flags, root_external = _resolve_activation(
        options.plugins, plugins, where="Options"
    )
    effective_agents = _merge_plugin_agents(options.agents, root_external)

    # -- 1. Compile the flat `agents` dict -----------------------
    def _compile_defn_tools(
        defn_tools: tuple[Any, ...] | None,
        external: tuple[tuple[str, PluginActivation], ...],
        *,
        where: str,
    ) -> tuple[ToolRef, ...]:
        """Shared helper: resolve an AgentDefinition.tools field
        (``None`` = full built-in set, same default as the main Options),
        plus any tools its activation contributes (loudly on a clash)."""
        if defn_tools is None:
            base: tuple[Any, ...] = tuple(sorted(builtin_tool_classes()))
        else:
            base = defn_tools
        return _merge_plugin_tools(
            _compile_tool_list(tuple(base), ()), external, (), where=where
        )

    agent_defn_names: list[str] = []
    for agent_name, defn in sorted(effective_agents.items()):
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

        # A child's own activation (D6: feature surfaces follow activation).
        child_flags, child_external = _resolve_activation(
            defn.plugins, plugins, where=f"AgentDefinition {agent_name!r}"
        )
        child_tools = _compile_defn_tools(
            defn.tools, child_external, where=f"AgentDefinition {agent_name!r}"
        )
        child_caps = _capabilities_from(
            child_flags, (), derive_delegation=False
        )
        child_instructions = _append_fragments(defn.prompt, child_external)
        # D10 single-valued policy: a flat child never carries a base
        # ``Options.policy``, so this only fails loudly when a child activates
        # TWO policy-contributing plugins. A child keeps ``POLICY_REF`` identity
        # (like it ignores the root ``Options.policy`` for its own spec); the
        # runtime decision policy is the host's process-wide ``policy_override``.
        _resolve_effective_policy(None, child_external, where=f"AgentDefinition {agent_name!r}")
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
            instructions=child_instructions,
            policy=POLICY_REF,
            composer=COMPOSER_REF,
            tools=child_tools,
            skills=(),
            default_budget=BudgetSpec(max_subtask_depth=3),
            capabilities=child_caps,  # flat children: no spawnable union
            metadata=child_metadata,
            default_model=defn.model,
        )
        descendant_specs.append(child_spec)

    # Reserved (double-underscore) agent names compile into the registry like
    # any other child — resolvable by name for HOST-seeded root tasks — but are
    # kept out of the parent's ``spawnable`` auto-union, so they never enter
    # the model-facing ``spawn_subagent`` directory (and never churn the
    # parent's stable prefix). The ``__workflow__`` precedent, now generalized:
    # ``__consolidation__`` (noeta.presets) rides this rule. The filter is
    # absolute — with ``Capabilities`` retired there is no ``spawnable``
    # override, and reintroducing one would put the reserved name back in the
    # model-facing directory. An agent that should be delegatable simply does
    # not carry the reserved prefix.
    all_child_names = tuple(
        sorted(n for n in agent_defn_names if not n.startswith("__"))
    )

    # -- 2. Resolve system_prompt (+ activation prompt fragments) ----------
    instructions = _append_fragments(
        _resolve_system_prompt(options.system_prompt), root_external
    )

    # -- 3. Resolve tools (replacement branch only) ----------------------------
    # Replacement semantics (D4): allowed_tools=None ⇒ full built-in set;
    # any tuple ⇒ exactly those. In-process MCP servers (Options.mcp_servers)
    # contribute their tools on top of whichever base applies — they are an
    # explicit, separate source, so they are added even under a replacement
    # allow-list. External plugin activation (D6) contributes its tools on top.
    if options.allowed_tools is None:
        base = tuple(sorted(builtin_tool_classes()))
    else:
        base = options.allowed_tools
    tool_entries = tuple(base) + _mcp_server_tool_entries(options.mcp_servers)
    tool_refs = _merge_plugin_tools(
        _compile_tool_list(tool_entries, options.disallowed_tools),
        root_external,
        options.disallowed_tools,
        where="Options",
    )

    # -- 3b. Resolve the decision policy ref (identity, D10) ----------------
    # Combine the base ``Options.policy`` with any active plugin ``policy``
    # contribution (single-valued — a collision fails loudly here). The resolved
    # factory's ``.ref`` enters identity so a swapped brain is a distinct agent;
    # the ``Client`` wires the SAME resolved factory as the host policy_override.
    effective_policy = _resolve_effective_policy(
        options.policy, root_external, where="Options"
    )
    policy_ref = (
        _resolve_policy_ref(effective_policy)
        if effective_policy is not None
        else POLICY_REF
    )

    # -- 4. Resolve skills --------------------------------------------------
    skill_refs = tuple(ComponentRef(name=s) for s in options.skills)

    # -- 5. Resolve capabilities (activation flags + additive child union) --
    caps = _capabilities_from(
        root_flags, all_child_names, derive_delegation=True
    )

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
