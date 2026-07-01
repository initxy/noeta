"""Project instructions file — the content channel's THIRD resident.

Mirror of :mod:`noeta.context.memory` specialised to the one-file
``NOETA.md`` / ``AGENTS.md`` workspace instructions mechanism (the
project's counterpart to Claude Code's ``CLAUDE.md``). The
generic channel absorbs this exactly like it absorbed memory: one
:class:`ContentKindSpec` registered next to the skill and memory kinds,
no composer change, no runtime change.

Two deliberate matches with memory:

* **Drift policy is ``evolving``** — the instructions file evolves day
  to day together with the repo, so the recording carries the ``evolving``
  policy: the ``content_hash`` is recorded as provenance and free to move.
* **Only ONE resident body** — a single workspace has a single
  instructions file; future multi-file variants (e.g. a repo-scoped
  instructions directory) would add names, not mechanisms.

Red line: every function here is pure over a wiring-time
:class:`InstructionsSnapshot` — the renderer closes over preloaded
state and never touches the disk at compose time. The impure half
(reading the workspace file) lives in
:mod:`noeta.execution.instructions`, before anything enters the ledger.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from noeta.context.composer import RenderedSkills
from noeta.context.content_channel import ContentKindSpec, ContentRenderer
from noeta.protocols.messages import Message, TextBlock


__all__ = [
    "INSTRUCTIONS_DRIFT_POLICY",
    "INSTRUCTIONS_KIND",
    "INSTRUCTIONS_VERSION",
    "InstructionsSnapshot",
    "build_instructions_renderer",
    "instructions_content_hash",
    "instructions_content_kind",
    "render_instructions_text",
]


#: The content channel kind key — matches ``TaskState.active_content``
#: and ``ContextContentRecorded.kind``.
INSTRUCTIONS_KIND = "instructions"
#: Declared shape version of the rendered body (not its content — content
#: is free to evolve under the ``evolving`` policy).
INSTRUCTIONS_VERSION = "1"
#: The drift policy instructions recordings carry: hash recorded, drift
#: allowed (advisory-only).
INSTRUCTIONS_DRIFT_POLICY = "evolving"


@dataclass(frozen=True, slots=True)
class InstructionsSnapshot:
    """Preloaded instructions file contents captured at wiring time.

    ``name`` is the file's basename (e.g. ``"NOETA.md"``) so the View
    source label reads ``instructions:NOETA.md``; ``text`` is the file
    body as read from disk (UTF-8 decoded, unmodified — the wrapping
    tag is the renderer's job, not the loader's). ``None`` is never a
    legal field value; callers that want "no instructions" must
    short-circuit and omit this kind entirely.
    """

    name: str
    text: str


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
    """Bind a snapshot to the channel's renderer shape.

    Pure over the snapshot: renders one ``role="user"`` message holding
    the tagged instructions body when the snapshot's name is active AND
    the snapshot text is non-empty; anything else renders nothing (an
    absent-instructions host leaves the ``semi_stable`` bytes
    untouched — zero footprint).
    """
    active_name = snapshot.name
    non_empty = bool(snapshot.text and snapshot.text.strip())
    rendered_text = (
        render_instructions_text(snapshot) if non_empty else ""
    )

    def _render(names: list[str]) -> RenderedSkills:
        if active_name not in names or not non_empty:
            return RenderedSkills(messages=[], selected_skills=[])
        return RenderedSkills(
            messages=[
                Message(role="user", content=[TextBlock(text=rendered_text)])
            ],
            selected_skills=[],
        )

    return _render


def instructions_content_kind(
    snapshot: InstructionsSnapshot,
) -> ContentKindSpec:
    """The instructions kind's registry item — the WHOLE integration surface.

    Register this next to ``skill_content_kind`` / ``memory_content_kind``
    in a :class:`ContentChannelRegistry` and the instructions file lives
    in the semi-stable segment, with its ``content_hash`` recorded through
    the generic ``(kind, name)`` seam under the ``evolving`` policy its
    recordings carry.
    """
    content_hash = instructions_content_hash(snapshot)

    def _hashes(name: str) -> tuple[str, str] | None:
        if name != snapshot.name:
            return None
        return (INSTRUCTIONS_VERSION, content_hash)

    return ContentKindSpec(
        kind=INSTRUCTIONS_KIND,
        renderer=build_instructions_renderer(snapshot),
        hashes=_hashes,
        policy=INSTRUCTIONS_DRIFT_POLICY,
    )
