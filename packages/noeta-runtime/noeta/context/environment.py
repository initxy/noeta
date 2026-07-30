"""Workspace environment block — the content channel's FOURTH resident (vocabulary).

Phase 2c: this module keeps only the kind **vocabulary** — the channel
constants and the :class:`EnvironmentSnapshot` type — shared by the kernel
loader/recording seam (``noeta.execution.environment``), fold, and the
``workspace`` built-in plugin. The renderer prose, hash rule and
``ContentKindSpec`` factory live in ``noeta.builtins.workspace.impl`` and
reach the kernel only through the injected
:class:`noeta.execution.environment.EnvironmentKit`.

Why a content-channel resident and NOT the system prompt (unchanged): the
system prompt is the composer's ``stable_prefix``, whose hash is the
prompt-cache key — churning it busts the provider KV cache, so it must
stay byte-stable across steps. The workspace path is volatile across
machines / sessions, so it belongs in ``semi_stable`` alongside
instructions, under the ``evolving`` drift policy (``content_hash``
recorded as provenance, free to move).
"""

from __future__ import annotations

from dataclasses import dataclass


__all__ = [
    "ENVIRONMENT_DRIFT_POLICY",
    "ENVIRONMENT_KIND",
    "ENVIRONMENT_NAME",
    "ENVIRONMENT_VERSION",
    "EnvironmentSnapshot",
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
    working in (relative fs-tool paths resolve against it); ``is_git_repo``
    is whether a ``.git`` entry exists at the root; ``platform`` is the
    host platform tag (``sys.platform``).

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
