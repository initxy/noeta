"""``Options`` — the recipe a library user writes, plus its pure compiler.

``Options`` is identity-layer sugar: a frozen recipe (unhashable — it carries
mapping-valued fields) that :func:`compile_options` turns into a frozen
:class:`~noeta.agent.spec.AgentSpec` plus a flat tuple of descendant specs a
host can register. Compilation is **additive and pure** — it fills SDK defaults
rather than overriding user intent, mutates no registry, and yields identical
identity for equal inputs; registration belongs to
:class:`~noeta.client.client.Client`. Wiring fields (``provider`` / ``cwd`` /
``guards`` / …) are ignored by the identity path *and* excluded from equality,
so two recipes differing only in wiring compare equal.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Protocol

from noeta.agent.spec import (
    AgentSpec,
    BudgetSpec,
    ComponentRef,
    ToolRef,
)
from noeta.client.mcp_server import SdkMcpServer
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
from noeta.protocols.policy import Policy


__all__ = [
    "AgentDefinition",
    "EFFORT_MODES",
    "Options",
    "PERMISSION_MODES",
    "PolicyFactory",
    "SystemPromptPreset",
    "PluginActivation",
    "ToolLike",
    "DEFAULT_PLUGINS",
    "compile_options",
    "effective_root_policy",
    "register_preset_prompt",
]


class ToolLike(Protocol):
    """The object form of a tool entry: anything exposing a ``.ref`` identity.

    Tool entries on :attr:`Options.allowed_tools` /
    :attr:`AgentDefinition.tools` / :attr:`PluginActivation.tools` are either
    built-in tool **name strings** or objects satisfying this Protocol
    (:class:`~noeta.tools.decorator.DecoratedTool` is the canonical
    implementation). :func:`compile_options` reads only ``.ref``; the runnable
    closure travels separately (the ``Client`` gathers ``DecoratedTool``
    instances into the host's ``custom_tools``).
    """

    @property
    def ref(self) -> ToolRef: ...


class PolicyFactory(Protocol):
    """The custom decision-policy contract behind ``Options.policy``.

    A factory ``(llm) -> Policy`` carrying its identity as ``.ref``: the ref
    enters the compiled :class:`~noeta.agent.spec.AgentSpec` (a swapped brain
    is a distinct agent) while the **same** factory object is wired as the
    host's runtime ``policy_override``. The loud runtime validation in
    :func:`_resolve_policy_ref` stands alongside this Protocol, because a
    Protocol cannot refuse a misconfigured object at compile time.
    """

    @property
    def ref(self) -> ComponentRef: ...

    def __call__(self, llm: Any) -> Policy: ...


# ---------------------------------------------------------------------------
# Activation vocabulary
# ---------------------------------------------------------------------------


#: The pinned default activation set for a bare ``Options()`` — the ``fs`` and
#: ``web`` built-in tool packs. Both are **identity-inert** in compilation (the
#: default tool set still comes from ``builtin_tool_classes()``), so listing
#: them here documents the conceptual default without perturbing the compiled
#: ``AgentSpec``. Memory / browser stay off.
DEFAULT_PLUGINS: tuple[str, ...] = ("fs", "web")


#: Built-in feature-bundle activation names that map onto an identity feature
#: flag (activating ``memory`` lands ``"memory"`` in the ``plugins`` tuple,
#: which :func:`~noeta.agent.spec.agent_activates` reads as the capability).
#: The mapping is name-preserving (a bundle's flag equals its own name), so
#: activating one folds exactly that name into identity and nothing else — the
#: tool / prompt wiring those bundles imply is already carried by the
#: capability-gated engine build and preset prompt baking.
_ACTIVATION_CAPABILITY_FLAG: Mapping[str, str] = {
    "todo_write": "todo_write",
    "ask_user_question": "ask_user_question",
    "skill_invocation": "skill_invocation",
    "memory": "memory",
    "mcp": "mcp",
    "browser": "browser",
    # The one structural capability that is ALSO authorable. ``delegation`` is
    # normally derived (a root with children delegates, a flat child does not),
    # but the derivation alone leaves no way to give a child the right to spawn.
    # Activating it is additive: it can turn delegation ON, never off (see
    # _activation_tuple).
    "delegation": "delegation",
}

#: Built-in activation names that carry **no** identity effect in compilation
#: (their runtime contributions are host-wired). Recognised so a typo in the
#: activation list still fails loudly.
_INERT_BUILTIN_ACTIVATIONS: frozenset[str] = frozenset(
    {
        "app",
        "fs",
        "governance",
        "presets",
        "providers",
        "react",
        "reminders",
        "sandbox",
        "skills",
        "storage",
        "web",
        "workspace",
    }
)

#: Every recognised built-in activation name (no plugin code executes to know
#: them — they are the SDK's own feature vocabulary).
#:
#: A superset of the built-in plugin **catalogue**
#: (``noeta.builtins.BUILTIN_PLUGIN_NAMES``): the catalogue names the plugins
#: that ship declarations, this set additionally carries the capability flags
#: that have no catalogue entry (``todo_write`` / ``mcp`` / …). The catalogue
#: sits ABOVE ``noeta.client`` in the import bands, so this module cannot read
#: it; ``noeta.builtins`` closes the loop instead by asserting the containment at
#: import (``_assert_activation_vocabulary``), which is what catches a built-in
#: that nobody added here.
BUILTIN_ACTIVATIONS: frozenset[str] = (
    frozenset(_ACTIVATION_CAPABILITY_FLAG) | _INERT_BUILTIN_ACTIVATIONS
)


@dataclass(frozen=True)
class PluginActivation:
    """The identity-plane contributions one **loaded external plugin** carries.

    Built by :class:`~noeta.client.client.Client` from a resolved
    :class:`~noeta.client.plugin_set.PluginSet` and passed to
    :func:`compile_options`, so ``noeta.client.options`` never imports the
    loader (which imports it — the edge would be a cycle). Built-in feature
    bundles are handled by name inside :func:`compile_options`; this type carries
    only the *third-party* identity contributions that **follow activation**:
    extra tools, extra child agents, extra prompt fragments. Wiring-plane
    contributions (guard / observer / provider / mcp / skills / sandbox) are not
    here — they never follow per-agent activation.
    """

    #: Tool entries (built-in name strings or ``.ref``-bearing tool objects).
    tools: tuple[str | ToolLike, ...] = ()
    #: ``(agent_name, AgentDefinition)`` child agents.
    agents: tuple[tuple[str, "AgentDefinition"], ...] = ()
    #: ``ContentKindSpec`` semi-stable residents, in contribution-name order. The
    #: ``Client`` appends these to the activating agent's content channels.
    content_kinds: tuple[ContentKindSpec, ...] = ()
    #: ``(contribution_name, text)`` prompt fragments, appended after the prompt.
    prompt_fragments: tuple[tuple[str, str], ...] = ()
    #: Extra identity feature flags this plugin's activation forces on. There is
    #: no manifest surface for these, so ``PluginSet.identity_activations()``
    #: never sets them — the field exists for a caller that drives
    #: :func:`compile_options` directly and wants an activation to imply a feature
    #: flag (a host expressing its own feature bundle, say). Names are folded into
    #: the compiled ``AgentSpec.plugins`` tuple as-is, so they must be recognised
    #: feature names (:data:`_ACTIVATION_CAPABILITY_FLAG` values — ``memory`` /
    #: ``browser`` / ``todo_write`` / …).
    capability_flags: tuple[str, ...] = ()
    #: The single ``policy`` contribution this plugin carries, a
    #: ``(llm) -> Policy`` factory exposing a ``.ref`` — or ``None``. Combined
    #: with the base ``Options.policy`` at compile: a base plus an active plugin
    #: policy, or two active plugin policies, is a loud single-valued collision.
    policy: Optional[PolicyFactory] = None


# ---------------------------------------------------------------------------
# Preset-prompt registry
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
# Surface types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SystemPromptPreset:
    """Reference a named system-prompt preset, with optional suffix append.

    Parameters
    ----------
    preset:
        Name of a preset already registered via
        :func:`register_preset_prompt`. The default ``"main"`` is the
        convention for the official coding-agent preset.
    append:
        Extra text appended (after ``"\n\n"``) to the resolved preset
        prompt. ``None`` ⇒ no suffix.
    """

    preset: str = "main"
    append: str | None = None


@dataclass(frozen=True)
class AgentDefinition:
    """Flat, non-recursive child-agent recipe (Claude Agent SDK shape).

    ``AgentDefinition`` **cannot nest** — it has no ``agents`` / ``subagents``
    field. Deep trees must be expressed by declaring every agent at the top
    level; the compiled ``AgentSpec.spawnable`` (derived from the ``agents``
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
        (``builtin_tool_classes()``). Same mixed-entry shape as
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
    #: Names of loaded plugins this child agent activates — built-in feature
    #: bundles (``"memory"`` / ``"skill_invocation"`` / ``"browser"`` …) or
    #: third-party plugin names. Enters identity: activating a feature bundle
    #: flips the matching capability flag, an external plugin contributes its
    #: identity-plane tools / agents / prompt fragments. Peer to
    #: :attr:`Options.plugins` but with no ``fs`` / ``web`` default (a child's
    #: tools come from :attr:`tools`).
    plugins: tuple[str, ...] = ()
    metadata: Mapping[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Options recipe dataclass
# ---------------------------------------------------------------------------


PERMISSION_MODES: tuple[str, ...] = ("default", "acceptEdits", "bypassPermissions")
"""Legal values for :attr:`Options.permission_mode`, in widening-trust order.

An ordered tuple rather than a set: the order is part of what this exports.
``noeta.client.capabilities.permission_modes`` hands it straight to a host's
picker, where alphabetical would put the most permissive mode first.
"""


EFFORT_MODES: tuple[str, ...] = ("low", "medium", "high", "xhigh", "max")
"""Legal values for :attr:`Options.effort` (reasoning-effort override), in
increasing-intensity order.

The single source of truth for the effort enum: ``__post_init__`` validates
against it, and ``noeta.client.capabilities.effort_modes`` projects it for the
app's ``/capabilities`` composer dropdown — which is why the order is the
intensity ramp and not the alphabet.
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
        Names of loaded plugins this agent activates. Built-in feature
        bundles (``"memory"`` / ``"skill_invocation"`` / ``"browser"`` …) fold
        into the compiled ``AgentSpec.plugins`` identity tuple; third-party
        plugin names pull in that plugin's identity-plane contributions. The
        compiler additionally folds ``"delegation"`` into the tuple when the
        agent has children and fills ``AgentSpec.spawnable`` with the child names
        structurally. Defaults to :data:`DEFAULT_PLUGINS`.
    model:
        Preferred LLM model id. A host routing hint — excluded from
        identity.
    metadata:
        Observational labels. Also excluded from identity.
    provider:
        Optional LLM provider. **Wiring, not identity** — completely
        ignored by :func:`compile_options` and identity.
    agents:
        Flat dict of ``name → AgentDefinition`` (Claude Agent SDK shape).
        Compiled into top-level descendant ``AgentSpec`` s. The parent's
        ``spawnable`` is unioned with these names.
    allowed_tools:
        Explicit tool allow-list. Entries may be built-in tool name
        strings or ``DecoratedTool`` instances (anything with a ``.ref``
        returning :class:`ToolRef`). ``None`` ⇒ **every built-in tool**
        (``builtin_tool_classes()``). Empty tuple ⇒ no tools.
    disallowed_tools:
        Tool names (by :class:`ToolRef.name`) to subtract from the parsed
        allow-list. Names that are not present are silently ignored.
    permission_mode:
        Permission-gating strategy. Must be one of ``"default"``,
        ``"acceptEdits"``, ``"bypassPermissions"``.
    max_turns:
        Syntactic sugar for ``budget.max_iterations``. Setting both
        ``budget.max_iterations`` and ``max_turns`` raises ``ValueError``
        (ambiguous).
    cwd:
        Optional working directory hint. Purely wiring-layer —
        **excluded from identity and from equality** (``compare=False``, same
        treatment as ``provider`` and ``metadata``). :func:`compile_options`
        never inspects it, so two ``Options`` differing only in ``cwd`` share
        an identity *and* compare equal.
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
    #: Names of loaded plugins this agent activates. Built-in feature bundles fold
    #: into the identity ``AgentSpec.plugins`` tuple (``plugins=["memory"]`` carries
    #: the memory capability); third-party plugin names pull in that plugin's
    #: identity-plane contributions. Defaults to :data:`DEFAULT_PLUGINS`
    #: (``fs`` / ``web`` — identity-inert). Unknown names fail compilation loudly
    #: (built-in vocabulary + the loaded ``PluginSet`` handed to ``Client``).
    plugins: tuple[str, ...] = DEFAULT_PLUGINS
    agents: Mapping[str, AgentDefinition] = field(default_factory=dict)
    allowed_tools: tuple[str | ToolLike, ...] | None = None
    disallowed_tools: tuple[str, ...] = ()
    permission_mode: str = "default"
    max_turns: int | None = None
    # --- wiring, excluded from identity AND from equality -------------------
    #
    # Every field below is declared "wiring, not identity" by this class'
    # docstring: :func:`compile_options` never reads it, so two recipes
    # differing only here compile to the same ``AgentSpec``. They are therefore
    # ``compare=False`` — ``Options`` equality says the same thing the identity
    # story does. ``hash`` follows ``compare``, and ``Options`` is unhashable
    # regardless because of its mapping-valued fields.
    model: Optional[str] = field(default=None, compare=False)
    metadata: Mapping[str, str] = field(default_factory=dict, compare=False)
    provider: Optional[LLMProvider] = field(default=None, compare=False)
    cwd: str | Path | None = field(default=None, compare=False)
    can_use_tool: Optional[Callable[[str, dict[str, Any]], bool]] = field(
        default=None, compare=False
    )
    output_schema: Optional[Mapping[str, Any]] = field(default=None, compare=False)
    thinking: Optional[str] = field(default=None, compare=False)
    effort: Optional[str] = field(default=None, compare=False)
    # --- extension points ---
    #: Identity-bearing (its ``.ref`` enters the compiled spec), so it stays
    #: in the comparison — unlike the wiring block above.
    policy: Optional[PolicyFactory] = None
    guards: tuple[Guard, ...] = field(default=(), compare=False)
    observers: tuple[Subscriber, ...] = field(default=(), compare=False)
    content_channels: tuple[ContentKindSpec, ...] = field(default=(), compare=False)
    #: Identity-bearing: an in-process server's tools are added to the agent's
    #: tool set, so they enter the compiled ``AgentSpec`` like any other tool.
    mcp_servers: tuple[SdkMcpServer, ...] = ()

    def __post_init__(self) -> None:
        if self.thinking is not None and self.thinking not in (
            "adaptive",
            "disabled",
        ):
            raise ValueError(
                f"Options.thinking must be 'adaptive', 'disabled', or None; "
                f"got {self.thinking!r}"
            )
        if self.effort is not None and self.effort not in EFFORT_MODES:
            raise ValueError(
                f"Options.effort must be one of "
                f"{EFFORT_MODES} or None; "
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


def _mcp_server_tool_entries(
    mcp_servers: tuple[SdkMcpServer, ...],
) -> tuple[str | ToolLike, ...]:
    """Flatten the in-process ``mcp_servers`` into tool entries."""
    entries: list[str | ToolLike] = []
    for server in mcp_servers:
        entries.extend(server.tools)
    return tuple(entries)


def _resolve_system_prompt(
    sp: str | SystemPromptPreset, presets: Mapping[str, str]
) -> str:
    """Resolve ``Options.system_prompt`` into a concrete instructions string.

    Plain strings pass through. :class:`SystemPromptPreset` looks the name up
    in ``presets``; a missing preset raises ``ValueError`` enumerating the
    registered names. The registry is a **parameter** so :func:`compile_options`
    stays a pure function of its inputs — the global ``_PRESET_PROMPTS`` is merely
    its default.
    """
    if isinstance(sp, str):
        return sp
    if sp.preset not in presets:
        registered = ", ".join(sorted(presets)) or "(none)"
        raise ValueError(
            f"Unknown system-prompt preset {sp.preset!r}. "
            f"Registered presets: {registered}. "
            f"Use register_preset_prompt(name, prompt) to register one."
        )
    base = presets[sp.preset]
    if sp.append is not None:
        return base + "\n\n" + sp.append
    return base


def _append_fragments(
    prompt: str, activations: tuple[tuple[str, PluginActivation], ...]
) -> str:
    """Append external plugin prompt fragments after ``prompt``.

    Fragments are ordered ``(plugin, contribution name)`` across all activated
    plugins and joined after a blank-line separator, mirroring how the presets
    bake the memory-policy fragment. No activation ⇒ ``prompt`` unchanged.
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
    """The single decision-policy factory for an agent (single-valued).

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
            f"no override"
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
    """Append the activated plugins' tools to ``base``, loudly on any conflict.

    Three conflict outcomes are errors rather than silent drops, because each one
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

    Every name must be a recognised built-in activation (:data:`BUILTIN_ACTIVATIONS`)
    or the name of a loaded plugin in ``plugins`` — anything else raises
    ``ValueError`` naming both the offending name and ``where`` (unknown
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
            known = ", ".join(sorted(BUILTIN_ACTIVATIONS))
            loaded = (
                ", ".join(sorted(plugins)) if plugins else "<none loaded>"
            )
            raise ValueError(
                f"unknown plugin activation {name!r} on {where} — not a built-in "
                f"activation ({known}) and not in the loaded plugin set "
                f"({loaded}). Load it before activating, or fix the name."
            )
    return frozenset(flags), tuple(external)


def _activation_tuple(
    activation_names: tuple[str, ...],
    flags: frozenset[str],
    subagent_names: tuple[str, ...],
    *,
    derive_delegation: bool,
) -> tuple[str, ...]:
    """Fold the resolved activation into the identity ``plugins`` tuple.

    The tuple is the sorted union of:

    * the activation **names** themselves — the built-in feature bundles, the
      identity-inert tool packs (``DEFAULT_PLUGINS`` included), and any external
      plugin names;
    * the feature **flags** the activation forces on — built-in bundle flags
      equal their own names (already present), plus any external-plugin
      ``capability_flags``, so a plugin that forces ``memory`` lands ``"memory"``
      in the tuple even though it was activated under its own name (identity
      stays the tuple alone — the forced flag is folded in here, at compile);
    * ``"delegation"`` when ``derive_delegation`` and the agent has an inline
      child roster. The combination is a union, never an override: a root with
      children delegates whether or not it also activated the ``delegation``
      bundle, and a flat child that activated the bundle delegates despite no
      inline roster.

    Membership in this tuple is the whole capability rule
    (:func:`~noeta.agent.spec.agent_activates`); ``spawnable`` is carried on the
    ``AgentSpec`` separately — activation cannot name an agent, so the ``agents``
    dict stays its only authoring path.
    """
    names = set(activation_names) | set(flags)
    if derive_delegation and subagent_names:
        names.add("delegation")
    return tuple(sorted(names))


# ---------------------------------------------------------------------------
# compile_options
# ---------------------------------------------------------------------------


def effective_root_policy(
    options: Options,
    plugins: Optional[Mapping[str, PluginActivation]] = None,
) -> object:
    """The single ``(llm) -> Policy`` factory a root agent runs, or ``None``.

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
    preset_prompts: Optional[Mapping[str, str]] = None,
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
        keyed by plugin name. ``Client`` builds this from a resolved
        :class:`~noeta.client.plugin_set.PluginSet`. ``None`` (a bare compile)
        means only built-in feature-bundle activations are recognised — any other
        activation name fails loudly. Activation is identity-affecting; wiring-plane
        plugin effects (guard / observer / provider / …) do not pass through here.
    preset_prompts:
        The ``name -> prompt`` registry a :class:`SystemPromptPreset` resolves
        against. ``None`` (the default) reads the process-wide registry
        :func:`register_preset_prompt` populates — the documented,
        last-writer-wins convenience. Passing an explicit mapping makes the
        compile a pure function of its arguments, which is what a caller
        wanting a hermetic compile (a test, a host with its own preset set)
        needs: without it, whether a recipe compiles at all depended on
        whichever module had been imported first.

    Returns
    -------
    tuple[AgentSpec, tuple[AgentSpec, ...]]
        ``main_spec`` is the top-level agent. ``descendants`` is a flat list
        of every agent declared via ``options.agents`` (no recursive nesting
        — see the module-level docstring).
    """
    presets = _PRESET_PROMPTS if preset_prompts is None else preset_prompts
    seen_names: set[str] = set()
    seen_names.add(options.name)
    descendant_specs: list[AgentSpec] = []

    if options.permission_mode not in PERMISSION_MODES:
        legal = ", ".join(PERMISSION_MODES)
        raise ValueError(
            f"Invalid permission_mode {options.permission_mode!r}. "
            f"Must be one of: {legal}."
        )

    # Resolve the root agent's activation: built-in feature bundles fold into
    # capability flags; external plugin names pull in their identity-plane
    # contributions (extra tools / child agents / prompt fragments).
    root_flags, root_external = _resolve_activation(
        options.plugins, plugins, where="Options"
    )
    effective_agents = _merge_plugin_agents(options.agents, root_external)

    def _compile_defn_tools(
        defn_tools: tuple[Any, ...] | None,
        external: tuple[tuple[str, PluginActivation], ...],
        *,
        where: str,
    ) -> tuple[ToolRef, ...]:
        """Resolve an AgentDefinition.tools field (``None`` = full built-in set,
        same default as the main Options), plus any tools its activation
        contributes (loudly on a clash)."""
        if defn_tools is None:
            base: tuple[Any, ...] = tuple(sorted(builtin_tool_classes()))
        else:
            base = defn_tools
        return _merge_plugin_tools(
            _compile_tool_list(tuple(base), ()), external, (), where=where
        )

    agent_defn_names: list[str] = []
    for agent_name, defn in sorted(effective_agents.items()):
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

        # A child's own activation (feature surfaces follow activation).
        child_flags, child_external = _resolve_activation(
            defn.plugins, plugins, where=f"AgentDefinition {agent_name!r}"
        )
        child_tools = _compile_defn_tools(
            defn.tools, child_external, where=f"AgentDefinition {agent_name!r}"
        )
        child_plugins = _activation_tuple(
            defn.plugins, child_flags, (), derive_delegation=False
        )
        child_instructions = _append_fragments(defn.prompt, child_external)
        # Single-valued policy: a flat child never carries a base
        # ``Options.policy``, so this only fails loudly when a child activates TWO
        # policy-contributing plugins. A child keeps ``POLICY_REF`` identity; the
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
            plugins=child_plugins,  # flat children: no spawnable union
            spawnable=(),
            metadata=child_metadata,
            default_model=defn.model,
        )
        descendant_specs.append(child_spec)

    # Reserved (double-underscore) agent names compile into the registry like
    # any other child — resolvable by name for HOST-seeded root tasks — but are
    # kept out of the parent's ``spawnable`` auto-union, so they never enter the
    # model-facing ``spawn_subagent`` directory (and never churn the parent's
    # stable prefix). The filter is absolute — ``spawnable`` is a structural
    # ``AgentSpec`` field derived from the child roster, never an authoring
    # override, so a reserved name re-enters the model-facing directory only if
    # listed here. An agent that should be delegatable simply does not carry the
    # reserved prefix.
    all_child_names = tuple(
        sorted(n for n in agent_defn_names if not n.startswith("__"))
    )

    instructions = _append_fragments(
        _resolve_system_prompt(options.system_prompt, presets), root_external
    )

    # Replacement semantics: allowed_tools=None ⇒ full built-in set; any tuple ⇒
    # exactly those. In-process MCP servers contribute their tools on top of
    # whichever base applies — an explicit, separate source, so they are added
    # even under a replacement allow-list. External plugin activation contributes
    # its tools on top.
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

    # The decision policy ref (identity): combine the base ``Options.policy`` with
    # any active plugin ``policy`` contribution (single-valued — a collision fails
    # loudly here). The resolved factory's ``.ref`` enters identity so a swapped
    # brain is a distinct agent; the ``Client`` wires the SAME resolved factory as
    # the host policy_override.
    effective_policy = _resolve_effective_policy(
        options.policy, root_external, where="Options"
    )
    policy_ref = (
        _resolve_policy_ref(effective_policy)
        if effective_policy is not None
        else POLICY_REF
    )

    skill_refs = tuple(ComponentRef(name=s) for s in options.skills)

    # The identity plugins tuple: activation names + forced flags + the structural
    # delegation derivation from the child roster.
    root_plugins = _activation_tuple(
        options.plugins, root_flags, all_child_names, derive_delegation=True
    )

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

    main = AgentSpec(
        name=options.name,
        instructions=instructions,
        policy=policy_ref,
        composer=COMPOSER_REF,
        tools=tool_refs,
        skills=skill_refs,
        default_budget=budget,
        plugins=root_plugins,
        spawnable=all_child_names,
        metadata=dict(options.metadata),
        default_model=options.model,
    )

    return main, tuple(descendant_specs)
