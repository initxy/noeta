"""The session-pack contract — one generic seam for session-assembled
capability packs (microkernel phase 3).

A *session pack* is the construction half of a capability: the factory a
plugin contributes (manifest surface ``session_pack``) that receives the
kernel-built :class:`SessionBuildContext` and returns a
:class:`PackContribution` — tools, content kinds, and named session exports.
The builder (:mod:`noeta.execution.builder`) runs every pack through ONE
priority-ordered loop; it enumerates no capability by name.

Ordering is the ``reminder`` / ``tool_result_transform`` precedent: integer
``priority`` ascending, ties broken by ``(plugin, name)`` upstream in the
plugin merge. Tool dict insertion order feeds the Engine's deterministic
``ToolSchemaRecorded`` emission and the stable-prefix hash, so a pack's
priority is part of its byte-order contract — the built-in bands are pinned
in :data:`~noeta.execution.builder._TOOL_PIPELINE`'s successor table and
locked by the ``tests/test_session_pack_goldens.py`` goldens.

Content kinds order on their OWN priority (:class:`ContentKindContribution`),
independent of the tool priority: the two orders genuinely differ (the skill
kind renders first in semi_stable while the script tool appends fifth), so a
single per-pack integer cannot express both.

``exports`` is the named side-state a pack hands the kernel's existing seams
(and, through :class:`~noeta.execution.builder.SessionInputs`, the host). A
key is admitted only when an existing kernel seam consumes it — the
:data:`EXPORT_*` constants below are that closed vocabulary; new needs go
through a spec, not a new key.
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

    Built by :func:`~noeta.execution.builder.build_session_inputs` before the
    pack loop; frozen so a pack can never perturb a later pack's inputs. A
    pack that finds itself inapplicable (its backend absent, its capability
    flag off, its config missing) returns the empty :class:`PackContribution`
    — applicability is the pack's own check against this context, never a
    kernel ``if``.
    """

    #: The kernel-built containment root (host ``from_path`` or lexical
    #: ``for_container`` under a sandbox ``exec_env``) — packs consume it,
    #: never build their own.
    workspace: WorkspaceRoot
    workspace_dir: Path
    content_store: ContentStore
    #: ``None`` ⇒ host-local IO; a sandbox ``ExecEnv`` routes pack IO into
    #: the session's container.
    exec_env: Optional[ExecEnv]
    model: str
    #: The bound model's vendor family (providers-catalog judgment); ``None``
    #: for any uncatalogued selector.
    provider_family: Optional[str]
    #: The agent's tool whitelist. Only the base pack (fs/web) filters by it;
    #: capability packs append past it by design.
    allowed_tools: frozenset[str]
    #: Named backend bag — host-populated, feature-agnostic. Well-known names
    #: are the contributing plugins' vocabulary (e.g. the sandbox provider's
    #: ``"browser"``, the product gateway's ``"app_preview"``), never the
    #: kernel's. An absent name means the capability has no live backing.
    backends: Mapping[str, object]
    #: The agent's derived capability flags by name (``"memory"``,
    #: ``"browser"``, …) — the per-agent activation truth a pack self-gates on.
    capability_flags: Mapping[str, bool]
    #: Per-plugin config bag: ``plugin name → its own keys``. The host maps
    #: its public fields in; each pack parses only its own entry and fails
    #: loudly on what it cannot read. Feature knobs with a single consumer live
    #: here, never as a typed context slot — the write/shell safety inputs are
    #: the ``"fs"`` entry (sole consumer: the fs pack); a key is promoted to a
    #: typed slot only when a second, unrelated plugin demonstrably reads it.
    plugin_config: Mapping[str, Mapping[str, object]]

    def config(self, plugin: str) -> Mapping[str, object]:
        """``plugin``'s config entry, or an empty mapping."""
        return self.plugin_config.get(plugin, {})

    def flag(self, name: str) -> bool:
        """``name``'s capability flag, defaulting to off."""
        return bool(self.capability_flags.get(name, False))


@runtime_checkable
class SessionRecorder(Protocol):
    """The one scoped write verb a plugin gets (spec §4.4).

    Handed by the kernel to a contribution's ``init`` hook at session seed
    time. ``record_content`` activates (or refreshes) a resident content item:
    the kernel stamps the envelope and emits one ``ContextContentRecorded``
    through its single-writer path, so a plugin never touches a raw
    ``EventLog``. This is not a security fence (in-process Python has none) —
    it is invariant preservation by construction: lifecycle events, message
    origin, and slice ownership cannot be corrupted by a buggy plugin, and the
    well-typed path is the easy path. The verb no-ops when ``(kind, name)`` is
    already active with an identical hash and records a refresh otherwise.
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


#: A contribution's pre-loop activation hook: ``(SessionRecorder) -> None``.
#: Run once per session build (including resume) at seed time; its recording
#: side effects are gated by the recorder's no-op-on-unchanged-hash rule, so
#: it is idempotent per drive (spec §6). The generic successor of the three
#: feature-named kernel seed recorders — third-party packs record their own
#: resident kinds through the same seam, with zero kernel edits.
InitHook = Callable[[SessionRecorder], None]


@dataclass(frozen=True, slots=True)
class ContentKindContribution:
    """One content kind + its registration priority.

    Registration order IS the semi_stable layout
    (:class:`~noeta.context.content_channel.ContentChannelRegistry`), and it
    differs from the tool order, so kinds sort on their own integer —
    ascending, ties broken by the pack loop order. Built-in bands: skill=100,
    memory=200, instructions=300, environment=400; host/extension kinds
    append after every built-in resident.
    """

    priority: int
    spec: ContentKindSpec


@dataclass(frozen=True, slots=True)
class PackContribution:
    """What one pack hands back to the builder. All fields optional; the
    empty contribution is the universal "not applicable" answer.

    ``tools`` merge in loop order with later-wins semantics — the existing
    construction-order contract (a later pack may deliberately shadow an
    earlier name, exactly as ``custom_tools`` always has). Cross-plugin
    accidental collisions are caught upstream at the manifest merge
    (``session_pack`` collides on ``name``), not here.
    """

    tools: Mapping[str, Tool] = field(default_factory=dict)
    content_kinds: tuple[ContentKindContribution, ...] = ()
    #: Pre-loop activation hook (spec §4.5): a closure that records this pack's
    #: resident content items through the kernel-handed :class:`SessionRecorder`
    #: at seed time. ``None`` ⇒ the pack activates no pre-loop residents.
    init: Optional[InitHook] = None
    # --- typed side-state fields (spec §4.3: typed fields, no stringly bag) ---
    # The kernel-consumed contributions: named, typed fields, each read by a
    # specific kernel seam. Adding one is an SPI change, reviewed as such.
    #: The skills kit (``noeta.execution.skills.SkillsKit``): feeds the build's
    #: skill content kind + guard grants and the ``skill`` control-tool mount's
    #: menu (the builder threads it onto the ``ControlToolBuildContext``).
    skills_kit: Optional[Any] = None
    #: The post-tool content-discovery hook → ``Engine(content_discovery=…)``.
    content_discovery: Optional[Any] = None
    #: The per-step resume preloader → ``Engine(content_preloader=…)``.
    content_preloader: Optional[Any] = None
    # Build-inspection pass-throughs: a pack's own state that never crosses INTO
    # a kernel seam — it rides ``SessionInputs`` only as a build-result
    # inspection surface for tests. (A future purification moves these fully
    # into the factory closures + reworks the inspecting tests, spec §4.3
    # "closures, not exports".)
    memory_store: Optional[Any] = None
    memory_entries: tuple[Any, ...] = ()
    instructions_snapshot: Optional[Any] = None
    instructions_snapshots: Mapping[str, Any] = field(default_factory=dict)
    environment_snapshot: Optional[Any] = None


#: The empty contribution — the canonical "this pack does not apply" value.
EMPTY_CONTRIBUTION = PackContribution()


@runtime_checkable
class SessionPackFactory(Protocol):
    """The uniform factory signature every session pack implements."""

    def __call__(self, ctx: SessionBuildContext) -> PackContribution: ...


@dataclass(frozen=True, slots=True)
class SessionPackEntry:
    """One resolved pack in the builder's loop: name + priority + factory.

    ``name`` is the contribution name (collision key + tie-breaker);
    ``priority`` is the manifest's declared integer band. The builder sorts
    entries by ``(priority, name)`` — the plugin merge has already resolved
    cross-plugin ties by ``(priority, plugin, name)`` upstream.
    """

    name: str
    priority: int
    factory: Callable[[SessionBuildContext], PackContribution]
