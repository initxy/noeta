"""Workspace environment resident — the compose-side kit type.

The execution-layer counterpart of :mod:`noeta.context.environment`
(pure render + hash pieces), mirroring how
:mod:`noeta.execution.instructions` glues the instructions file to the
composer. Since the kernel final form (spec §4.5) this module is kit-type
only: the write-side activation is the generic ``init`` hook the
``workspace`` built-in contributes, recorded through the scoped
:class:`~noeta.execution.recorder.SeedRecorder` — there is no
feature-named ``record_environment`` seam anymore.

* :class:`EnvironmentKit` — what one session build consumes from the
  environment resident (the ``ContentKindSpec`` factory + the fingerprint
  rule), injected by the ``workspace`` built-in
  (``noeta.builtins.workspace.impl:build_environment_kit``) so the
  compose-time renderer and the record-time fingerprint share one source.

The impure loader half (``load_environment`` — git/platform/date capture)
is product content and lives in the ``workspace`` built-in plugin
(``noeta.builtins.workspace.impl.loaders``), reached through that plugin's
``session_pack`` contribution (compose side) and its ``init`` hook
(record side).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from noeta.context.content_channel import ContentKindSpec
from noeta.context.environment import EnvironmentSnapshot


__all__ = [
    "EnvironmentKit",
]


@dataclass(frozen=True)
class EnvironmentKit:
    """What one session build consumes from the environment resident.

    The SkillsKit pattern (phase 2c): the renderer prose, the hash rule and
    the ``ContentKindSpec`` factory are product material and live in the
    ``workspace`` built-in plugin
    (``noeta.builtins.workspace.impl:build_environment_kit``); the kernel
    receives the bundle so the compose-time renderer and the record-time
    fingerprint share a single source of truth.
    """

    #: ``snapshot -> ContentKindSpec`` — the registry item factory.
    content_kind: Callable[[EnvironmentSnapshot], ContentKindSpec]
    #: ``snapshot -> sha256(rendered bytes)`` — the recorded fingerprint.
    content_hash: Callable[[EnvironmentSnapshot], str]
