"""Workspace-context material — the environment + instructions residents.

Phase 2c: the renderer prose, hash rules and ``ContentKindSpec`` factories
for the two workspace residents moved here from
``noeta.context.{environment,instructions}`` (which keep only the kind
vocabulary constants and the snapshot types). The kernel consumes this
material solely through the injected kits
(:class:`noeta.execution.environment.EnvironmentKit` /
:class:`noeta.execution.instructions.InstructionsKit`) — the reminders
built-in precedent: registry mechanism kernel-side, rendered material here.

Red line (unchanged from the kernel days): every renderer is pure over a
wiring-time snapshot — no disk, no clock at compose time — so the same
folded state always composes the same bytes. The impure loaders live in the
sibling :mod:`.loaders` module (microkernel phase 3, D10 — moved out of the
kernel), and both residents enter a session as this plugin's
``session_pack`` contributions (:func:`build_instructions_session_pack` /
:func:`build_environment_session_pack`).
"""

from __future__ import annotations

import hashlib
from typing import Mapping

from noeta.context.composer import ContentResolve, RenderedContent
from noeta.context.content_channel import ContentKindSpec, ContentRenderer
from noeta.protocols.errors import ContentNotFound
from noeta.context.environment import (
    ENVIRONMENT_DRIFT_POLICY,
    ENVIRONMENT_KIND,
    ENVIRONMENT_NAME,
    ENVIRONMENT_VERSION,
    EnvironmentSnapshot,
)
from noeta.context.instructions import (
    INSTRUCTIONS_DRIFT_POLICY,
    INSTRUCTIONS_KIND,
    INSTRUCTIONS_VERSION,
    InstructionsSnapshot,
)
from noeta.builtins.workspace.impl.loaders import (
    build_instructions_discovery,
    build_instructions_preloader,
    discover_instructions,
    load_environment,
    load_instructions,
)
from noeta.execution.environment import EnvironmentKit
from noeta.execution.instructions import InstructionsKit
from noeta.execution.session_pack import (
    ContentKindContribution,
    PackContribution,
    SessionBuildContext,
    SessionRecorder,
)
from noeta.protocols.messages import Message, TextBlock
from pathlib import Path
from typing import Optional, cast


__all__ = [
    "DEFAULT_INSTRUCTIONS_FILENAMES",
    "build_environment_kit",
    "build_environment_renderer",
    "build_environment_session_pack",
    "build_instructions_discovery",
    "build_instructions_kit",
    "build_instructions_preloader",
    "build_instructions_renderer",
    "build_instructions_session_pack",
    "discover_instructions",
    "load_environment",
    "load_instructions",
    "environment_content_hash",
    "environment_content_kind",
    "instructions_content_hash",
    "instructions_content_kind",
    "instructions_content_kind_from",
    "render_environment_text",
    "render_instructions_text",
]


#: Workspace-root search order for the instructions file. The first
#: existing, non-empty candidate wins. NOETA.md is canonical (the
#: project's CLAUDE.md counterpart); AGENTS.md is a common GitHub /
#: repo convention supported as a fallback.
DEFAULT_INSTRUCTIONS_FILENAMES = ("NOETA.md", "AGENTS.md")


# ---------------------------------------------------------------------------
# Environment resident
# ---------------------------------------------------------------------------


def render_environment_text(snapshot: EnvironmentSnapshot) -> str:
    """Deterministic rendered body — the resident's rendered text.

    Wraps the facts in a single ``<workspace-environment>`` tag block so
    the model can tell this segment apart from surrounding prompt text.
    The format mirrors instructions' tagged style: one block, fixed field
    order, trivially diffable. The middle line states the resolution rule
    explicitly so the model knows the directory is the relative-path
    anchor, not just trivia.

    The git branch / status / date lines are appended only when captured —
    an empty field (non-git workspace, or capture that failed / was
    skipped) renders no line at all, keeping the block tight.
    """
    lines = [
        "<workspace-environment>",
        f"Working directory: {snapshot.workspace_display}",
        "File paths for read, edit, glob, and grep resolve relative to "
        "this directory.",
        f"Is a git repository: {'true' if snapshot.is_git_repo else 'false'}",
        f"Platform: {snapshot.platform}",
    ]
    if snapshot.git_branch:
        lines.append(f"Git branch: {snapshot.git_branch}")
    if snapshot.git_status:
        lines.append(f"Git status:\n{snapshot.git_status}")
    if snapshot.captured_date:
        lines.append(f"Captured at: {snapshot.captured_date}")
    lines.append("</workspace-environment>")
    return "\n".join(lines)


def environment_content_hash(snapshot: EnvironmentSnapshot) -> str:
    """``sha256`` over the *rendered* bytes — one source of truth.

    Same rule as instructions: the recorded ``content_hash`` IS what the
    model actually saw, so hashing the rendered output (not the raw fields)
    keeps the record-time and compose-time ``content_hash`` in lock-step.
    """
    return hashlib.sha256(
        render_environment_text(snapshot).encode("utf-8")
    ).hexdigest()


def build_environment_renderer(
    snapshot: EnvironmentSnapshot,
) -> ContentRenderer:
    """The environment renderer — pure over (folded state, content store).

    Renders one ``role="user"`` message holding the tagged environment block
    **resolved from the ContentStore at the resident's active hash** (spec §6)
    when :data:`ENVIRONMENT_NAME` is active; anything else renders nothing. The
    ``snapshot`` is not read at compose time — the ledger's active hash fully
    determines the bytes; retained only so the sibling
    :func:`environment_content_kind` can share the builder call shape.
    """

    def _render(names: list[str], resolve: ContentResolve) -> RenderedContent:
        if ENVIRONMENT_NAME not in names:
            return RenderedContent(messages=[], selected_skills=[])
        text = resolve(ENVIRONMENT_KIND, ENVIRONMENT_NAME).decode("utf-8")
        return RenderedContent(
            messages=[
                Message(role="user", content=[TextBlock(text=text)])
            ],
            selected_skills=[],
        )

    return _render


def environment_content_kind(
    snapshot: EnvironmentSnapshot,
) -> ContentKindSpec:
    """The environment kind's registry item — the WHOLE integration surface.

    Registered next to the skill / memory / instructions kinds in a
    ``ContentChannelRegistry`` so the environment block lives in the
    semi-stable segment, with its ``content_hash`` recorded through the
    generic ``(kind, name)`` seam under the ``evolving`` policy its
    recordings carry.
    """
    content_hash = environment_content_hash(snapshot)

    def _hashes(name: str) -> tuple[str, str] | None:
        if name != ENVIRONMENT_NAME:
            return None
        return (ENVIRONMENT_VERSION, content_hash)

    return ContentKindSpec(
        kind=ENVIRONMENT_KIND,
        renderer=build_environment_renderer(snapshot),
        hashes=_hashes,
        policy=ENVIRONMENT_DRIFT_POLICY,
    )


def build_environment_kit() -> EnvironmentKit:
    """The kernel builder's ``environment_kit`` injection (phase 2c)."""
    return EnvironmentKit(
        content_kind=environment_content_kind,
        content_hash=environment_content_hash,
    )


# ---------------------------------------------------------------------------
# Instructions resident
# ---------------------------------------------------------------------------


def render_instructions_text(snapshot: InstructionsSnapshot) -> str:
    """Deterministic rendered body — the resident's rendered text.

    Wraps the raw instructions in a single ``<workspace-instructions
    source="…">`` tag block so the model can tell this segment apart
    from surrounding prompt text. The tag format mirrors memory's
    plain-text rendering style: one heading, one body, kept trivially
    diffable.
    """
    return (
        f'<workspace-instructions source="{snapshot.name}">\n'
        f"{snapshot.text}\n"
        f"</workspace-instructions>"
    )


def instructions_content_hash(snapshot: InstructionsSnapshot) -> str:
    """``sha256`` over the *rendered* bytes — one source of truth.

    Same rule as memory: the recorded ``content_hash`` IS what the model
    actually saw, so hashing the rendered output (not the raw file) keeps
    the record-time and compose-time ``content_hash`` in lock-step.
    """
    return hashlib.sha256(
        render_instructions_text(snapshot).encode("utf-8")
    ).hexdigest()


def build_instructions_renderer(
    snapshot: InstructionsSnapshot,
) -> ContentRenderer:
    """The single-file instructions renderer — pure over (state, store).

    Renders one ``role="user"`` message holding the tagged instructions body
    **resolved from the ContentStore at the resident's active hash** (spec §6)
    when the snapshot's name is active; anything else renders nothing. The
    ``snapshot`` is not read at compose time; retained only to name the active
    resident and share the builder call shape.
    """
    active_name = snapshot.name

    def _render(names: list[str], resolve: ContentResolve) -> RenderedContent:
        if active_name not in names:
            return RenderedContent(messages=[], selected_skills=[])
        try:
            text = resolve(INSTRUCTIONS_KIND, active_name).decode("utf-8")
        except ContentNotFound:
            return RenderedContent(messages=[], selected_skills=[])
        return RenderedContent(
            messages=[
                Message(role="user", content=[TextBlock(text=text)])
            ],
            selected_skills=[],
        )

    return _render


def instructions_content_kind(
    snapshot: InstructionsSnapshot,
) -> ContentKindSpec:
    """The single-file instructions kind (the root ``NOETA.md``/``AGENTS.md``).

    Sugar over :func:`instructions_content_kind_from` with a one-entry
    mapping — byte-identical rendering for the root-only host.
    """
    return instructions_content_kind_from({snapshot.name: snapshot})


def instructions_content_kind_from(
    snapshots: Mapping[str, InstructionsSnapshot],
) -> ContentKindSpec:
    """The instructions kind's registry item — the WHOLE integration surface.

    ``snapshots`` maps resident name → preloaded snapshot: the root file
    under its basename, plus (discovery mode,
    docs/adr/anchored-content-placement.md) every discovered subdirectory
    file under its workspace-relative path. The mapping still feeds the
    ``content_hash`` seam (:func:`_hashes`) and the discovery hook, but the
    renderer no longer reads it — it **resolves each active name's bytes from
    the ContentStore at the ledger's active hash** (spec §6), so the composed
    instructions are a pure function of (folded state, store). A name active
    in the ledger whose bytes are absent (vanished file, degraded preload)
    renders nothing: the ``evolving`` policy tolerates drift, and a resolve
    miss may only omit.
    """

    def _render(names: list[str], resolve: ContentResolve) -> RenderedContent:
        messages: list[Message] = []
        for name in names:
            try:
                text = resolve(INSTRUCTIONS_KIND, name).decode("utf-8")
            except ContentNotFound:
                continue
            if not text.strip():
                continue
            messages.append(
                Message(role="user", content=[TextBlock(text=text)])
            )
        return RenderedContent(messages=messages, selected_skills=[])

    def _hashes(name: str) -> tuple[str, str] | None:
        snapshot = snapshots.get(name)
        if snapshot is None:
            return None
        return (INSTRUCTIONS_VERSION, instructions_content_hash(snapshot))

    return ContentKindSpec(
        kind=INSTRUCTIONS_KIND,
        renderer=_render,
        hashes=_hashes,
        policy=INSTRUCTIONS_DRIFT_POLICY,
    )


def build_instructions_kit() -> InstructionsKit:
    """The kernel builder's ``instructions_kit`` injection (phase 2c)."""
    return InstructionsKit(
        content_kind_from=instructions_content_kind_from,
        content_hash=instructions_content_hash,
        filenames=DEFAULT_INSTRUCTIONS_FILENAMES,
    )


# ---------------------------------------------------------------------------
# Session packs (microkernel phase 3) — the residents' construction halves.
# ---------------------------------------------------------------------------


def build_instructions_session_pack(ctx: SessionBuildContext) -> PackContribution:
    """The instructions resident as a ``session_pack`` contribution (band 400).

    Reads this plugin's own config entry (``instructions_enabled`` /
    ``instructions_file`` / ``instructions_discovery``), loads the root
    snapshot once, and contributes the instructions content kind (kind band
    300 — after skill/memory, before environment) whenever a root snapshot
    loaded OR discovery is armed (an empty mapping renders nothing until the
    first discovered activation, zero footprint). The SHARED mutable
    ``name → snapshot`` mapping always rides the exports — the discovery
    hook / resume preloader (also exported here when armed) add entries to
    the same dict at tool/step time.
    """
    cfg = ctx.config("workspace")
    enabled = bool(cfg.get("instructions_enabled", False))
    discovery = bool(cfg.get("instructions_discovery", False))
    override = cast(Optional[Path], cfg.get("instructions_file"))
    kit = build_instructions_kit()
    content_store = ctx.content_store
    snapshots: dict[str, InstructionsSnapshot] = {}
    root_snapshot: Optional[InstructionsSnapshot] = None
    if enabled:
        snapshot = load_instructions(
            ctx.workspace_dir,
            filenames=kit.filenames,
            override_path=override,
            exec_env=ctx.exec_env,
        )
        if snapshot is not None:
            # The root file lives under its basename (resident name unchanged
            # → byte-identical rendering); discovered files join later under
            # relative paths.
            snapshots[snapshot.name] = snapshot
            root_snapshot = snapshot
    kinds: tuple[ContentKindContribution, ...] = ()
    if snapshots or discovery:
        kinds = (
            ContentKindContribution(300, kit.content_kind_from(snapshots)),
        )
    content_discovery = None
    content_preloader = None
    if discovery:
        content_discovery = build_instructions_discovery(
            ctx.workspace,
            snapshots,
            kit=kit,
            content_store=content_store,
            render_text=render_instructions_text,
            exec_env=ctx.exec_env,
        )
        content_preloader = build_instructions_preloader(
            ctx.workspace_dir, snapshots, exec_env=ctx.exec_env
        )

    def _init(rec: SessionRecorder) -> None:
        """Pre-loop activation of the ROOT instructions resident (spec §4.5).

        Records the same root snapshot the composer's kind renders from; the
        ``ref.hash`` equals the rendered-instructions sha256 the fingerprint
        always carried, so the event payload matches the retired
        ``record_instructions`` call (the envelope now attributes
        ``actor="plugin:instructions"``). Discovered subtree files activate
        later through the content-discovery hook, not here. No root file ⇒ no-op.
        """
        if root_snapshot is None:
            return
        body = render_instructions_text(root_snapshot).encode("utf-8")
        ref = content_store.put(body, media_type="text/markdown")
        rec.record_content(
            kind=INSTRUCTIONS_KIND,
            name=root_snapshot.name,
            version=INSTRUCTIONS_VERSION,
            ref=ref,
            policy=INSTRUCTIONS_DRIFT_POLICY,
        )

    return PackContribution(
        content_kinds=kinds,
        init=_init,
        content_discovery=content_discovery,
        content_preloader=content_preloader,
        instructions_snapshot=root_snapshot,
        instructions_snapshots=snapshots,
    )


def build_environment_session_pack(ctx: SessionBuildContext) -> PackContribution:
    """The environment resident as a ``session_pack`` contribution (band 500).

    Always on (a workspace always exists): captures the session-static
    workspace facts once so the composer's renderer AND the pre-loop
    ``record_environment`` share the same snapshot, and contributes the
    environment content kind LAST of the built-in residents (kind band 400)
    so the semi_stable byte layout is unchanged for sessions that never
    activate it.
    """
    snapshot = load_environment(ctx.workspace_dir, exec_env=ctx.exec_env)
    kit = build_environment_kit()
    content_store = ctx.content_store

    def _init(rec: SessionRecorder) -> None:
        """Pre-loop activation of the environment resident (spec §4.5).

        Records the same snapshot the composer's kind renders from; ``ref.hash``
        equals the rendered-environment sha256 the fingerprint always carried,
        so the event payload matches the retired ``record_environment`` call
        (the envelope now attributes ``actor="plugin:environment"``).
        """
        body = render_environment_text(snapshot).encode("utf-8")
        ref = content_store.put(body, media_type="text/markdown")
        rec.record_content(
            kind=ENVIRONMENT_KIND,
            name=ENVIRONMENT_NAME,
            version=ENVIRONMENT_VERSION,
            ref=ref,
            policy=ENVIRONMENT_DRIFT_POLICY,
        )

    return PackContribution(
        content_kinds=(
            ContentKindContribution(400, kit.content_kind(snapshot)),
        ),
        init=_init,
        environment_snapshot=snapshot,
    )
