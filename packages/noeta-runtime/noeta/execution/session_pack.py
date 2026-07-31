"""The session-pack contract: the construction half of a capability. A plugin
contributes a factory (manifest surface ``session_pack``) that reads the
kernel-built :class:`SessionBuildContext` and returns a
:class:`PackContribution`; the builder runs every pack through one
priority-ordered loop and enumerates no capability by name.

Priority is a byte-order contract, not a hint: tool dict insertion order feeds
the Engine's ``ToolSchemaRecorded`` emission and the stable-prefix hash. Content
kinds order on their OWN priority because the tool order and the semi_stable
render order genuinely differ, and one per-pack integer cannot express both.
Side-state a pack hands the kernel is a typed field on
:class:`PackContribution`, so adding one is an SPI change; pack-internal state
stays inside the factory closure.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import (
    Any,
    Callable,
    Mapping,
    Optional,
    Protocol,
    runtime_checkable,
)

from noeta.context.content_channel import ContentKindSpec
from noeta.protocols.content_store import ContentStore
from noeta.protocols.tool import Tool
from noeta.protocols.values import ContentRef
from noeta.runtime.exec_env import ExecEnv
from noeta.runtime.workspace import WorkspaceRoot


__all__ = [
    "SessionBuildContext",
    "SessionRecorder",
    "InitHook",
    "ContentKindContribution",
    "PackContribution",
    "SessionPackFactory",
    "SessionPackEntry",
]


@dataclass(frozen=True, slots=True)
class SessionBuildContext:
    """What one session build offers every pack — generic slots only.

    Frozen, so a pack can never perturb a later pack's inputs. A pack that finds
    itself inapplicable (backend absent, capability flag off, config missing)
    returns the empty :class:`PackContribution` — applicability is the pack's own
    check against this context, never a kernel ``if``.
    """

    #: The kernel-built containment root — packs consume it, never build their
    #: own.
    workspace: WorkspaceRoot
    workspace_dir: Path
    content_store: ContentStore
    #: ``None`` ⇒ host-local IO; a sandbox ``ExecEnv`` routes pack IO into
    #: the session's container.
    exec_env: Optional[ExecEnv]
    model: str
    #: The bound model's vendor family; ``None`` for an uncatalogued selector.
    provider_family: Optional[str]
    #: The agent's tool whitelist. Only the base pack filters by it; capability
    #: packs append past it by design.
    allowed_tools: frozenset[str]
    #: Named backend bag — host-populated, feature-agnostic. The names belong to
    #: the contributing plugins (``"browser"``, ``"app_preview"``), never to the
    #: kernel. An absent name means the capability has no live backing.
    backends: Mapping[str, object]
    #: The agent's derived capability flags by name — the per-agent activation
    #: truth a pack self-gates on.
    capability_flags: Mapping[str, bool]
    #: Per-plugin config bag: ``plugin name → its own keys``. Each pack parses
    #: only its own entry and fails loudly on what it cannot read. A knob with a
    #: single consumer lives here rather than as a typed context slot, and is
    #: promoted only when a second, unrelated plugin demonstrably reads it.
    plugin_config: Mapping[str, Mapping[str, object]]

    def config(self, plugin: str) -> Mapping[str, object]:
        """``plugin``'s config entry, or an empty mapping."""
        return self.plugin_config.get(plugin, {})

    def flag(self, name: str) -> bool:
        """``name``'s capability flag, defaulting to off."""
        return bool(self.capability_flags.get(name, False))


@runtime_checkable
class SessionRecorder(Protocol):
    """The one scoped write verb a plugin gets, handed to its ``init`` hook at
    session seed time.

    The kernel stamps the envelope and emits the ``ContextContentRecorded``
    through its single-writer path, so a plugin never touches a raw
    ``EventLog``. Not a security fence (in-process Python has none) — invariant
    preservation by construction: lifecycle events, message origin, and slice
    ownership cannot be corrupted by a buggy plugin, and the well-typed path is
    the easy path.
    """

    def record_content(
        self,
        *,
        kind: str,
        name: str,
        version: str,
        ref: ContentRef,
        policy: str,
    ) -> None:
        """Activate/refresh the ``(kind, name)`` resident at ``ref``'s bytes."""
        ...


#: A contribution's pre-loop activation hook, run once per session build
#: (including resume) at seed time. The recorder's no-op-on-unchanged-hash rule
#: is what makes it idempotent per drive.
InitHook = Callable[[SessionRecorder], None]


@dataclass(frozen=True, slots=True)
class ContentKindContribution:
    """One content kind + its registration priority.

    Registration order IS the semi_stable layout, and it differs from the tool
    order, so kinds sort on their own integer — ascending, ties broken by the
    pack loop order. Built-in bands: skill=100, memory=200, instructions=300,
    environment=400; extension kinds append after every built-in resident.
    """

    priority: int
    spec: ContentKindSpec


@dataclass(frozen=True, slots=True)
class PackContribution:
    """What one pack hands back to the builder. All fields optional; the
    empty contribution is the universal "not applicable" answer.

    ``tools`` merge in loop order with later-wins semantics, so a later pack may
    deliberately shadow an earlier name. Accidental cross-plugin collisions are
    caught upstream at the manifest merge, not here.
    """

    tools: Mapping[str, Tool] = field(default_factory=dict)
    content_kinds: tuple[ContentKindContribution, ...] = ()
    #: Records this pack's resident content items at seed time. ``None`` ⇒ the
    #: pack activates no pre-loop residents.
    init: Optional[InitHook] = None
    # Typed side-state below: each field is read by one specific kernel seam,
    # so adding one is an SPI change, reviewed as such.
    #: Control-tool entries. Each factory typically closes over the pack's own
    #: build state, keeping pack-specific types out of kernel code. The builder
    #: runs them through the SAME mount loop as host-supplied entries; name
    #: collisions across the two sources stay loud.
    control_tools: tuple[Any, ...] = ()
    #: Opaque guard-input bundle for the injected guards factory. Single-writer
    #: across the pack loop; its shape is a producer/consumer-plugin contract and
    #: the builder never reads inside it.
    guard_facts: Optional[Any] = None
    content_discovery: Optional[Any] = None
    content_preloader: Optional[Any] = None


#: The empty contribution — the canonical "this pack does not apply" value.
EMPTY_CONTRIBUTION = PackContribution()


@runtime_checkable
class SessionPackFactory(Protocol):
    """The uniform factory signature every session pack implements."""

    def __call__(self, ctx: SessionBuildContext) -> PackContribution: ...


@dataclass(frozen=True, slots=True)
class SessionPackEntry:
    """One resolved pack in the builder's loop.

    The builder sorts entries by ``(priority, name)``; the plugin merge has
    already resolved cross-plugin ties by ``(priority, plugin, name)`` upstream.
    """

    name: str
    priority: int
    factory: Callable[[SessionBuildContext], PackContribution]
