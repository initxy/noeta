"""The public SPI for storage backend authors.

Satisfying the L0 storage Protocols is not enough to be a correct backend: an
EventLog must restore typed payloads from canonical bytes and apply the same
``PayloadTooLarge`` cap, and a Dispatcher must apply the same stale-reclaim cap
and the same wake-match rule, or it silently drifts from every other backend
under one shared contract suite. Those four decisions are domain rules rather
than storage mechanics, so third-party and built-in durable backends alike route
through this single facade; the implementations stay private behind it.
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
