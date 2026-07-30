"""Instructions file resident — the compose-side kit type.

The execution-layer counterpart of :mod:`noeta.context.instructions`
(pure render + hash pieces), mirroring how
:mod:`noeta.execution.memory` glues the memory subsystem to the composer.
Since the kernel final form (spec §4.5) this module is kit-type only: the
write-side activation is the generic ``init`` hook the ``workspace``
built-in contributes, recorded through the scoped
:class:`~noeta.execution.recorder.SeedRecorder` — there is no
feature-named ``record_instructions`` seam anymore.

* :class:`InstructionsKit` — what one session build consumes from the
  instructions resident (the ``ContentKindSpec`` factory, the fingerprint
  rule, and the candidate-filename search order), injected by the
  ``workspace`` built-in
  (``noeta.builtins.workspace.impl:build_instructions_kit``) so the
  pre-loop loader, the mid-loop discovery hook and the compose-time
  renderer all share one source of truth.

The impure loader half (``load_instructions`` — the ``NOETA.md`` /
``AGENTS.md`` convention — plus the ``read``-triggered discovery walk and
the resume preloader) is product content and lives in the ``workspace``
built-in plugin (``noeta.builtins.workspace.impl.loaders``), reached
through that plugin's ``session_pack`` contribution (compose side, which
also exports the Engine's ``content_discovery`` / ``content_preloader``
hooks) and its ``init`` hook (record side).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Mapping

from noeta.context.content_channel import ContentKindSpec
from noeta.context.instructions import InstructionsSnapshot


__all__ = [
    "InstructionsKit",
]


@dataclass(frozen=True)
class InstructionsKit:
    """What one session build consumes from the instructions resident.

    The SkillsKit pattern (phase 2c): the tag-block renderer, the hash
    rule, the ``ContentKindSpec`` factory AND the candidate-filename
    search order (``NOETA.md`` / ``AGENTS.md`` — a product convention,
    not kernel vocabulary) live in the ``workspace`` built-in plugin
    (``noeta.builtins.workspace.impl:build_instructions_kit``); the
    kernel receives the bundle so the pre-loop loader, the mid-loop
    discovery hook and the compose-time renderer all share a single
    source of truth.
    """

    #: ``{name: snapshot} -> ContentKindSpec`` — the registry item factory
    #: (the mapping is deliberately mutable and shared with discovery).
    content_kind_from: Callable[
        [Mapping[str, InstructionsSnapshot]], ContentKindSpec
    ]
    #: ``snapshot -> sha256(rendered bytes)`` — the recorded fingerprint.
    content_hash: Callable[[InstructionsSnapshot], str]
    #: Workspace-root search order for instruction files; the first
    #: existing, non-empty candidate wins (per directory).
    filenames: tuple[str, ...]
