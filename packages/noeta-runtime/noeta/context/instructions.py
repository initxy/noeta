"""Project instructions file — the content channel's THIRD resident (vocabulary).

Phase 2c: this module keeps only the kind **vocabulary** — the channel
constants and the :class:`InstructionsSnapshot` type — shared by the kernel
loader/discovery/recording seams (``noeta.execution.instructions``), fold,
and the ``workspace`` built-in plugin. The tag-block renderer, hash rule,
``ContentKindSpec`` factories AND the candidate-filename convention
(``NOETA.md``/``AGENTS.md``) live in ``noeta.builtins.workspace.impl`` and
reach the kernel only through the injected
:class:`noeta.execution.instructions.InstructionsKit`.

Two deliberate matches with memory (unchanged):

* **Drift policy is ``evolving``** — the instructions file evolves day
  to day together with the repo, so the recording carries the ``evolving``
  policy: the ``content_hash`` is recorded as provenance and free to move.
* **Residents are named** — the root file under its basename, discovered
  subdirectory files under their workspace-relative paths.
"""

from __future__ import annotations

from dataclasses import dataclass


__all__ = [
    "INSTRUCTIONS_DRIFT_POLICY",
    "INSTRUCTIONS_KIND",
    "INSTRUCTIONS_VERSION",
    "InstructionsSnapshot",
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
