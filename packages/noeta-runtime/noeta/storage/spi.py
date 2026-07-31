"""``noeta.storage.spi`` — the public SPI for storage backend authors.

"A backend only needs the Protocols" is nominally true and practically
false: any EventLog backend must restore typed payloads from canonical
bytes, and any Dispatcher backend must apply the stale-reclaim cap and the
wake-match rule — or silently drift from the built-ins. A third-party
backend implements the L0 storage Protocols in ``noeta.protocols`` and
routes these shared domain rules through this facade; the built-in durable
backends (``noeta.builtins.storage.impl``, noeta-sdk) do exactly the same,
standing in for the external author. The implementations stay private
(``noeta.storage._payload_restore`` / ``_reclaim`` / ``_wake_match``) —
this module is their one documented entry.

* :func:`restore_payload` — the event-type → typed-payload restore table.
  A persistent EventLog stores the envelope payload as canonical bytes and
  rebuilds the typed payload dataclass on read; a reflection test in the
  contract suite fails CI when a new ``*Payload`` class lacks an entry, so
  every backend restoring through this table stays complete.
* :func:`enforce_payload_cap` — the ``PayloadTooLarge`` cap every
  persistent EventLog applies on emit, measured on the canonical bytes it
  is about to store (large bodies must go through the ContentStore).
* :func:`reclaim_hits_cap` — the poison-task terminal decision: a task at
  ``reclaim_max`` consecutive no-progress stale-lease reclaims drops to
  ``terminal`` instead of requeueing forever.
* :func:`wake_matches` — the projection-matching invariant deciding
  whether a wake event satisfies a task's ``wake_on`` condition (delegates
  to :func:`noeta.protocols.wake.matches_wake`, with the ``None`` guard).
"""

from __future__ import annotations

from noeta.storage._payload_restore import (
    _enforce_payload_cap as enforce_payload_cap,
    _restore_payload as restore_payload,
)
from noeta.storage._reclaim import reclaim_hits_cap
from noeta.storage._wake_match import _matches as wake_matches


__all__ = [
    "enforce_payload_cap",
    "reclaim_hits_cap",
    "restore_payload",
    "wake_matches",
]
