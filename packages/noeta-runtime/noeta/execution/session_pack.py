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
    Sequence,
    runtime_checkable,
)

from noeta.context.content_channel import ContentKindSpec
from noeta.protocols.content_store import ContentStore
from noeta.protocols.tool import Tool
from noeta.runtime.exec_env import ExecEnv
from noeta.runtime.shell_policy import ShellMode
from noeta.runtime.workspace import FsWriteMode, WorkspaceRoot, WriteRootsResolver


__all__ = [
    "SessionBuildContext",
    "ContentKindContribution",
    "PackContribution",
    "SessionPackFactory",
    "SessionPackEntry",
    "EXPORT_SKILLS_KIT",
    "EXPORT_CONTENT_DISCOVERY",
    "EXPORT_CONTENT_PRELOADER",
    "EXPORT_MEMORY_STORE",
    "EXPORT_MEMORY_ENTRIES",
    "EXPORT_INSTRUCTIONS_SNAPSHOT",
    "EXPORT_INSTRUCTIONS_SNAPSHOTS",
    "EXPORT_ENVIRONMENT_SNAPSHOT",
]


# ---------------------------------------------------------------------------
# Export keys — the closed vocabulary of pack side-state the kernel consumes.
# Each key names an existing kernel seam: the SessionInputs field / post-tools
# phase that reads it. A pack MAY export other keys; the kernel ignores them
# (they still ride SessionInputs.session_exports for host-side consumers).
# ---------------------------------------------------------------------------

#: The skills kit (``noeta.execution.skills.SkillsKit``): feeds the control
#: schemas' skill menu, the guard grants, and ``SessionInputs.skill_registry``.
EXPORT_SKILLS_KIT = "skills_kit"
#: The opaque memory store handle → ``SessionInputs.memory_store``.
EXPORT_MEMORY_STORE = "memory_store"
#: The load-time memory index snapshot → ``SessionInputs.memory_entries``
#: (shared by the composer's renderer and the pre-loop record seam).
EXPORT_MEMORY_ENTRIES = "memory_entries"
#: The root instructions snapshot → ``SessionInputs.instructions_snapshot``.
EXPORT_INSTRUCTIONS_SNAPSHOT = "instructions_snapshot"
#: The SHARED MUTABLE ``name → snapshot`` mapping the instructions kind
#: renders from; the discovery hook / resume preloader add entries at
#: tool/step time, so identity (not a copy) is the contract.
EXPORT_INSTRUCTIONS_SNAPSHOTS = "instructions_snapshots"
#: The workspace environment snapshot → ``SessionInputs.environment_snapshot``.
EXPORT_ENVIRONMENT_SNAPSHOT = "environment_snapshot"
#: The post-tool content-discovery hook → ``SessionInputs.content_discovery``
#: (wired into ``Engine(content_discovery=…)`` by the host).
EXPORT_CONTENT_DISCOVERY = "content_discovery"
#: The per-step resume preloader → ``SessionInputs.content_preloader``
#: (wired into ``Engine(content_preloader=…)`` by the host).
EXPORT_CONTENT_PRELOADER = "content_preloader"


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
    #: loudly on what it cannot read.
    plugin_config: Mapping[str, Mapping[str, object]]
    #: Shared write/shell safety inputs any pack may honour.
    write_mode: FsWriteMode
    shell_mode: ShellMode
    shell_allowlist: Sequence[Mapping[str, Any]]
    write_path_globs: tuple[str, ...]
    write_roots: Optional[WriteRootsResolver]

    def config(self, plugin: str) -> Mapping[str, object]:
        """``plugin``'s config entry, or an empty mapping."""
        return self.plugin_config.get(plugin, {})

    def flag(self, name: str) -> bool:
        """``name``'s capability flag, defaulting to off."""
        return bool(self.capability_flags.get(name, False))


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
    exports: Mapping[str, object] = field(default_factory=dict)


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
