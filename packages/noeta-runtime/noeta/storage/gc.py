"""Content garbage collection — mark from the event log, sweep the store.

The event log is append-only and the content store is content-addressed, so
the only bodies that can go are the ones no event will ever ask for again. Two
kinds exist by construction, and this module is where that judgement lives:

* an LLM request body (``LLMRequestStarted.request_ref``) — the whole View the
  model saw, different on every step, derivable from folded state plus the
  composer, read back by nothing. A ref is minted for it without a body by
  default (``RuntimeLLMClient``); a host that opted into recording gets a
  transient copy that the next sweep past the grace reclaims;
* a superseded ``TaskSnapshot`` body — fold reads only a task's latest one;
  the older ones are reachable only through a bounded fold, which now rebuilds
  from the events when the body is gone.

Everything else an event references, directly or through another body, stays.
The transitive step is textual on purpose: tool outputs are plugin-defined, so
the runtime cannot enumerate every shape that carries a ref, and keeping a blob
whose hash merely appears in some tool's text is the cheap direction of the
error. Data loss is the expensive one.

The sweep itself is the adapter's (``sweep(live, *, grace_seconds, vacuum)``,
duck-typed like ``purge_task``); the grace is what makes a sweep safe while
turns are running — a body ``put`` (or re-``put``: a dedup hit refreshes its
age) inside the grace is never a candidate, and the adapter re-checks the age
under its own write lock, so the put→emit window of a live turn cannot lose
a body to a sweep that marked before the event landed.
"""

from __future__ import annotations

import dataclasses
import re
from dataclasses import dataclass
from typing import Any, Optional, Protocol

from noeta.protocols.content_store import ContentStore, SweepOutcome
from noeta.protocols.event_log import EventLogReader, EventLogTaskIndex
from noeta.protocols.values import ContentRef


__all__ = [
    "CollectGarbageResult",
    "GcEventLog",
    "collect_garbage",
    "live_content_hashes",
]


class GcEventLog(EventLogReader, EventLogTaskIndex, Protocol):
    """What the mark needs from an event log: every stream, and each one's
    events — the two read capabilities, no write."""


#: Content hashes are hex SHA-256; anything shaped like one in a payload or a
#: body is treated as a reference.
_HEX64_BYTES = re.compile(rb"[0-9a-f]{64}")
_HEX64_TEXT = re.compile(r"[0-9a-f]{64}")

#: Bodies fetched per ``get_many`` round-trip while closing the reachable set.
#: Small because a batch can hold snapshot bodies of several megabytes each.
_SCAN_CHUNK = 64

#: The one event whose ref is never a retention root.
_REQUEST_EVENT_TYPE = "LLMRequestStarted"
_SNAPSHOT_EVENT_TYPE = "TaskSnapshot"


@dataclass(frozen=True, slots=True)
class CollectGarbageResult:
    """What :func:`collect_garbage` did.

    ``ok`` is ``False`` only when the store cannot sweep (``reason ==
    "unsupported"``); every count is then zero. ``live`` is the number of
    hashes the mark found reachable, ``swept`` / ``bytes_freed`` what the
    sweep deleted, ``vacuumed`` whether the backend also compacted its file.
    """

    ok: bool
    live: int
    swept: int
    bytes_freed: int
    vacuumed: bool
    reason: Optional[str] = None


def live_content_hashes(
    event_log: GcEventLog, content_store: ContentStore
) -> set[str]:
    """Every content hash some event still needs, closed over the bodies.

    Roots are the payloads of every event in every stream, minus the two
    exclusions the module docstring names; the closure adds every hash found
    in a reachable body until nothing new appears.
    """
    roots: set[str] = set()
    for summary in event_log.list_task_streams():
        events = event_log.read(summary.task_id)
        latest_snapshot_seq = max(
            (env.seq for env in events if env.type == _SNAPSHOT_EVENT_TYPE),
            default=None,
        )
        for env in events:
            if env.type == _REQUEST_EVENT_TYPE:
                continue
            if env.type == _SNAPSHOT_EVENT_TYPE and env.seq != latest_snapshot_seq:
                continue
            _collect_hashes(env.payload, roots)

    live = set(roots)
    frontier = list(roots)
    while frontier:
        chunk, frontier = frontier[:_SCAN_CHUNK], frontier[_SCAN_CHUNK:]
        bodies = content_store.get_many(_refs_for(chunk))
        for body in bodies.values():
            for match in _HEX64_BYTES.findall(body):
                found = match.decode("ascii")
                if found not in live:
                    live.add(found)
                    frontier.append(found)
    return live


def collect_garbage(
    event_log: GcEventLog,
    content_store: ContentStore,
    *,
    grace_seconds: float = 3600.0,
    vacuum: bool = False,
) -> CollectGarbageResult:
    """Reclaim every body no event references, older than ``grace_seconds``.

    Safe to call while tasks run (see the module docstring for why the grace
    is what makes it so); ``vacuum=True`` additionally asks the backend to
    compact its file, which on sqlite rewrites the whole database under the
    write lock — a quiet-hours call, not a per-sweep default. A store without
    a ``sweep`` method reports ``ok=False, reason="unsupported"`` rather than
    raising, so a host can wire the call unconditionally.
    """
    if grace_seconds < 0:
        raise ValueError(
            f"grace_seconds must be non-negative, got {grace_seconds!r}"
        )
    sweep = getattr(content_store, "sweep", None)
    if sweep is None:
        return _unsupported()
    live = live_content_hashes(event_log, content_store)
    try:
        outcome: SweepOutcome = sweep(
            live, grace_seconds=grace_seconds, vacuum=vacuum
        )
    except NotImplementedError:
        return _unsupported()
    return CollectGarbageResult(
        ok=True,
        live=len(live),
        swept=outcome.rows,
        bytes_freed=outcome.bytes,
        vacuumed=outcome.vacuumed,
    )


# ---------------------------------------------------------------------------
# internal helpers
# ---------------------------------------------------------------------------


def _unsupported() -> CollectGarbageResult:
    return CollectGarbageResult(
        ok=False, live=0, swept=0, bytes_freed=0, vacuumed=False,
        reason="unsupported",
    )


def _refs_for(hashes: list[str]) -> list[ContentRef]:
    # ``get_many`` is hash-only; size and media_type are not consulted.
    return [ContentRef(hash=h, size=0, media_type="") for h in hashes]


def _collect_hashes(value: Any, out: set[str]) -> None:
    """Walk a restored payload (dataclasses, dicts, lists, scalars) and add
    every ``ContentRef.hash`` and every bare 64-hex string it carries."""
    if isinstance(value, ContentRef):
        out.add(value.hash)
    elif isinstance(value, str):
        if len(value) == 64 and _HEX64_TEXT.fullmatch(value):
            out.add(value)
    elif isinstance(value, (bytes, bytearray)):
        for match in _HEX64_BYTES.findall(bytes(value)):
            out.add(match.decode("ascii"))
    elif isinstance(value, dict):
        for key, item in value.items():
            _collect_hashes(key, out)
            _collect_hashes(item, out)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            _collect_hashes(item, out)
    elif dataclasses.is_dataclass(value) and not isinstance(value, type):
        for field in dataclasses.fields(value):
            _collect_hashes(getattr(value, field.name), out)
