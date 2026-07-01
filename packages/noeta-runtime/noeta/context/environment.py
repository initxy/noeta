"""Workspace environment block — the content channel's FOURTH resident.

Sibling of :mod:`noeta.context.instructions`, specialised to a tiny set
of *session-static* environment facts: the workspace directory the agent
operates in (the anchor relative paths resolve against), whether it is a
git repo, and the platform. The model otherwise has no way to know the
absolute workspace path — the system prompt only says it works "inside a
single workspace directory" — so a model-guessed relative path that
misses resolves to ``not a file``. Handing it the directory up front is
the source-side reduction of that error.

Why a content-channel resident and NOT the system prompt: the system
prompt is the composer's ``stable_prefix``, whose hash is the prompt-cache
key — churning it busts the provider KV cache and spikes cost, so it must
stay byte-stable across steps. The workspace path is volatile across
machines / sessions, so it belongs in ``semi_stable`` alongside
instructions, under the ``evolving`` drift policy (``content_hash``
recorded as provenance, free to move) — exactly like the instructions
file. The generic
channel absorbs it like it absorbed memory and instructions: one
:class:`ContentKindSpec`, no composer change, no runtime change.

Two deliberate matches with instructions:

* **Drift policy is ``evolving``** — an absolute path in the rendered
  bytes changes when the same recording runs on another machine (the
  same known trade-off skill base dirs already carry), so the recording
  carries the ``evolving`` policy: the ``content_hash`` is recorded as
  provenance and free to move.
* **One resident body** — a single workspace has one environment block;
  the resident name is the constant :data:`ENVIRONMENT_NAME`.

Red line: every function here is pure over a wiring-time
:class:`EnvironmentSnapshot` — the renderer closes over preloaded state
and never touches the disk / clock at compose time. The impure half
(reading the workspace path / probing ``.git``) lives in
:mod:`noeta.execution.environment`, before anything enters the ledger.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from noeta.context.composer import RenderedSkills
from noeta.context.content_channel import ContentKindSpec, ContentRenderer
from noeta.protocols.messages import Message, TextBlock


__all__ = [
    "ENVIRONMENT_DRIFT_POLICY",
    "ENVIRONMENT_KIND",
    "ENVIRONMENT_NAME",
    "ENVIRONMENT_VERSION",
    "EnvironmentSnapshot",
    "build_environment_renderer",
    "environment_content_hash",
    "environment_content_kind",
    "render_environment_text",
]


#: The content channel kind key — matches ``TaskState.active_content``
#: and ``ContextContentRecorded.kind``.
ENVIRONMENT_KIND = "environment"
#: The single resident name (a workspace has exactly one environment
#: block). The View source label reads ``environment:workspace``.
ENVIRONMENT_NAME = "workspace"
#: Declared shape version of the rendered body (not its content — content
#: is free to evolve under the ``evolving`` policy). Bumped to ``"2"`` when
#: the git branch / status / capture-date lines joined the rendered block.
ENVIRONMENT_VERSION = "2"
#: The drift policy environment recordings carry: hash recorded, drift
#: allowed (advisory-only) — an absolute path moves across machines.
ENVIRONMENT_DRIFT_POLICY = "evolving"


@dataclass(frozen=True, slots=True)
class EnvironmentSnapshot:
    """Preloaded, session-static workspace facts captured at wiring time.

    ``workspace_display`` is the directory string the model is told it is
    working in (relative ``read``/``edit``/``glob``/``grep`` paths resolve
    against it); ``is_git_repo`` is whether a ``.git`` entry exists at the
    root; ``platform`` is the host platform tag (``sys.platform``).

    ``git_branch`` / ``git_status`` / ``captured_date`` are a once-at-start
    snapshot of the git branch, ``git status --short`` (truncated) and the
    host date/time, captured at wiring time alongside the rest. They are
    session-static by deliberate choice — memoized at session start, NOT
    refreshed per turn (mirrors Claude Code's memoized git status), so the
    rendered bytes stay stable and never churn the prompt cache; a model
    that wants live state runs ``git status`` itself. Each is the empty
    string when capture fails or does not apply (non-git workspace), and an
    empty line is omitted from the rendered block.
    """

    workspace_display: str
    is_git_repo: bool
    platform: str
    git_branch: str = ""
    git_status: str = ""
    captured_date: str = ""


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
    """Bind a snapshot to the channel's renderer shape.

    Pure over the snapshot: renders one ``role="user"`` message holding
    the tagged environment block when :data:`ENVIRONMENT_NAME` is active;
    anything else renders nothing (a never-activated kind leaves the
    ``semi_stable`` bytes untouched — zero footprint).
    """
    rendered_text = render_environment_text(snapshot)

    def _render(names: list[str]) -> RenderedSkills:
        if ENVIRONMENT_NAME not in names:
            return RenderedSkills(messages=[], selected_skills=[])
        return RenderedSkills(
            messages=[
                Message(role="user", content=[TextBlock(text=rendered_text)])
            ],
            selected_skills=[],
        )

    return _render


def environment_content_kind(
    snapshot: EnvironmentSnapshot,
) -> ContentKindSpec:
    """The environment kind's registry item — the WHOLE integration surface.

    Register this next to ``skill_content_kind`` / ``memory_content_kind``
    / ``instructions_content_kind`` in a
    :class:`noeta.context.content_channel.ContentChannelRegistry` and the
    environment block lives in the semi-stable segment, with its
    ``content_hash`` recorded through the generic ``(kind, name)`` seam
    under the ``evolving`` policy its recordings carry.
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
