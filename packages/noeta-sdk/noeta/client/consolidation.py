"""Memory consolidation — the host-side half of the background curation pass.

Memory v2 phase 3 (spec: ``docs/implementation-specs/2026-07-10-memory-v2.md``;
architecture: ``docs/adr/memory-consolidation.md``). Consolidation itself is an
ordinary agent (``noeta.presets.CONSOLIDATION_AGENT``) driven as an ordinary
root task on the resident worker pool; this module supplies everything the
host needs around it:

* the **debounce marker** (``.consolidation-state.json`` in the memory root) —
  read/write helpers plus the pure :func:`consolidation_due` guard;
* the **digest builder** (:func:`build_consolidation_digest`) — recent root
  sessions' conversational text, capped and tail-truncated, with the window and
  the caps stated in a header (no silent truncation);
* the **run entry** (:func:`run_consolidation`) — decision #11's explicit
  host-callable: debounce-check, build the digest, write the marker at enqueue
  time, and seed the ``__consolidation__`` root task onto the ready queue.

Deep module, small surface: hosts call :func:`run_consolidation`; the helpers
are exported for hosts that orchestrate their own schedule (and for tests).
The runtime is untouched — everything here reads the public event stream and
writes one dot-file next to the memory store.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from noeta.core.fold import messages_from_appended
from noeta.protocols.messages import TextBlock


__all__ = [
    "CONSOLIDATION_AGENT_NAME",
    "CONSOLIDATION_MARKER_FILENAME",
    "build_consolidation_digest",
    "consolidation_due",
    "read_consolidation_marker",
    "run_consolidation",
    "write_consolidation_marker",
]


#: The reserved agent name the consolidation preset registers under. Defined
#: HERE (not in ``noeta.presets``) because the run entry below seeds by this
#: name and the digest builder excludes sessions driven by it, while the
#: import-linter ``sdk-core-not-presets`` contract forbids ``noeta.client``
#: from importing presets. ``noeta.presets`` re-exports it (one definition,
#: two import paths) next to the matching :class:`AgentDefinition`. The
#: double-underscore prefix marks it reserved: ``compile_options`` keeps
#: ``__``-prefixed names out of the parent's ``spawnable`` roster and the
#: product backend hides them from its agent list, so the name is resolvable
#: for host-seeded root tasks yet never model- or user-selectable (the
#: ``__workflow__`` precedent).
CONSOLIDATION_AGENT_NAME = "__consolidation__"

#: Marker file recording the last consolidation enqueue, stored in the memory
#: root. A dot-file that is NOT ``*.md``, so the store's non-recursive
#: ``*.md`` glob (index / recall / search) can never see it.
CONSOLIDATION_MARKER_FILENAME = ".consolidation-state.json"

#: Default debounce threshold between consolidation runs.
DEFAULT_DEBOUNCE_HOURS = 24.0

#: Digest caps (spec risk note: conservative initial values). Both are stated
#: inside the digest itself so the consolidation agent never mistakes the
#: window for the whole history.
DEFAULT_MAX_SESSIONS = 10
DEFAULT_MAX_CHARS_PER_SESSION = 16_000

#: The brief instruction prepended to the digest to form the run's goal (the
#: full curation contract lives in the preset's system prompt).
_GOAL_PREAMBLE = (
    "Memory consolidation run: curate the long-term memory store.\n"
    "Review the session-activity digest below against your memory index and "
    "apply your curation duties: merge near-duplicate memories, archive "
    "memories shown to be wrong or superseded, and write clearly-missed "
    "durable facts. Finish with a one-paragraph summary of the actions taken."
)


# ---------------------------------------------------------------------------
# Debounce marker
# ---------------------------------------------------------------------------


def _as_utc(moment: datetime) -> datetime:
    """Normalize ``moment`` to an aware UTC datetime (naive ⇒ assumed UTC)."""
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def read_consolidation_marker(memory_root: Path) -> Optional[datetime]:
    """The last recorded consolidation enqueue time, or ``None``.

    Missing file, unreadable file, malformed JSON, or a malformed / absent
    ``last_run_at`` all return ``None`` — a corrupt marker degrades to "due"
    (and to an uncapped digest window), never to an exception on the trigger
    path. A naive stored timestamp is interpreted as UTC.
    """
    path = Path(memory_root) / CONSOLIDATION_MARKER_FILENAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        parsed = datetime.fromisoformat(str(data["last_run_at"]))
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return _as_utc(parsed)


def write_consolidation_marker(memory_root: Path, *, now: datetime) -> None:
    """Record ``now`` (ISO-8601 UTC) as the last consolidation enqueue.

    Creates the memory root if needed (the marker may precede the first
    memory file). Written at ENQUEUE time by :func:`run_consolidation`, so a
    slow in-flight run cannot be re-triggered; between "might skip a run when
    a seed fails" and "might storm-enqueue", this favors under-triggering
    (the ADR's stance).
    """
    root = Path(memory_root)
    root.mkdir(parents=True, exist_ok=True)
    stamp = _as_utc(now).isoformat()
    (root / CONSOLIDATION_MARKER_FILENAME).write_text(
        json.dumps({"last_run_at": stamp}), encoding="utf-8"
    )


def consolidation_due(
    memory_root: Path,
    *,
    now: datetime,
    debounce_hours: float = DEFAULT_DEBOUNCE_HOURS,
) -> bool:
    """Whether a consolidation run is due at ``now`` (pure given ``now``).

    Due when no valid marker exists (first run / corrupt marker) or the last
    recorded run is at least ``debounce_hours`` old. Zero side effects — the
    session-stop seams may call this on every turn boundary.
    """
    last = read_consolidation_marker(memory_root)
    if last is None:
        return True
    return _as_utc(now) - last >= timedelta(hours=debounce_hours)


# ---------------------------------------------------------------------------
# Digest builder
# ---------------------------------------------------------------------------


def _genesis_payload(envelopes: list[Any]) -> Optional[Any]:
    """The stream's genesis ``TaskCreated`` payload (``None`` if malformed).

    Genesis is always seq 0 (the same invariant the backend's root filter
    relies on); the defensive scan keeps a legacy/malformed stream from being
    silently mis-classified.
    """
    for env in envelopes:
        if getattr(env, "type", None) == "TaskCreated":
            return env.payload
    return None


def _session_transcript(
    envelopes: list[Any], content_store: Any, *, max_chars: int
) -> str:
    """Role-labeled conversational text of one session, tail-truncated.

    Keeps only ``TextBlock`` text from user/assistant turns: tool results and
    tool-use blocks carry no ``TextBlock`` payload worth curating, system-role
    messages are request metadata, and host-injected ``origin="memory"`` /
    ``origin="system"`` turns would let the store echo into its own digest.
    When the transcript exceeds ``max_chars`` the TAIL is kept (recent turns
    matter most) behind an explicit ``[... earlier turns omitted]`` marker.
    """
    turns: list[str] = []
    for env in envelopes:
        if getattr(env, "type", None) != "MessagesAppended":
            continue
        try:
            messages = messages_from_appended(env, content_store)
        except Exception:
            # A pruned/unresolvable body must not sink the whole digest.
            continue
        for msg in messages:
            if msg.role == "system" or msg.origin in ("memory", "system"):
                continue
            body = "\n".join(
                block.text
                for block in msg.content
                if isinstance(block, TextBlock) and block.text.strip()
            )
            if body:
                turns.append(f"{msg.role}: {body}")
    text = "\n".join(turns)
    if len(text) > max_chars:
        tail = text[-max_chars:]
        cut = tail.find("\n")
        if 0 <= cut < len(tail) - 1:
            tail = tail[cut + 1 :]
        text = "[... earlier turns omitted]\n" + tail
    return text


def build_consolidation_digest(
    client: Any,
    *,
    since: Optional[datetime] = None,
    max_sessions: int = DEFAULT_MAX_SESSIONS,
    max_chars_per_session: int = DEFAULT_MAX_CHARS_PER_SESSION,
    include_task: Optional[Callable[[str], bool]] = None,
) -> Optional[str]:
    """A capped digest of recent root-session activity, or ``None`` when empty.

    Enumerates the client's task streams, keeps ROOT sessions (genesis
    ``parent_task_id`` is ``None`` — a subtask rides its root) with envelope
    activity strictly after ``since`` (``None`` ⇒ all history), newest first,
    capped at ``max_sessions``. Sessions driven by a reserved (``__``-prefixed)
    agent — the consolidation agent itself — are excluded so a run never
    digests its own predecessors. Each kept session contributes its
    role-labeled text (see :func:`_session_transcript`); sessions yielding no
    conversational text are skipped without consuming (or overflowing) the
    cap, so the dropped count is exact.

    ``include_task`` is the host-side digest scope (issue #53): a predicate
    over ROOT session task ids; sessions it rejects are out of the digest's
    universe entirely — they neither consume the cap nor count as omitted,
    exactly like a subtask. A multi-tenant host runs one curation pass per
    tenant by filtering to that tenant's root sessions (and pointing
    ``memory_root`` at that tenant's store, whose per-root marker then gives
    per-tenant debounce). ``None`` (default) ⇒ the whole-ledger digest,
    byte-identical to today. The header states the scoping so the
    consolidation agent never mistakes a tenant's slice for the whole ledger.

    The header states the window, the session count, how many digestible
    sessions the cap dropped, and the per-session character cap — the spec's
    "no silent caps" rule.

    Reads the PUBLIC client surface (``task_streams`` for the wall-clock
    activity bound, ``events`` for the per-stream envelopes); the one host
    internal it touches is the paired content store, which
    ``MessagesAppendedPayload.messages_ref`` bodies can only be resolved
    against (no public accessor exists, by design — refs are useless with any
    other store).
    """
    since_ts = _as_utc(since).timestamp() if since is not None else None
    summaries = [
        s for s in client.task_streams() if isinstance(getattr(s, "task_id", None), str)
    ]
    if since_ts is not None:
        summaries = [
            s for s in summaries if getattr(s, "last_event_time", 0.0) > since_ts
        ]
    summaries.sort(key=lambda s: getattr(s, "last_event_time", 0.0), reverse=True)
    content_store = client._host.content_store  # noqa: SLF001 — see docstring

    sections: list[str] = []
    omitted = 0
    for summary in summaries:
        envelopes = client.events(summary.task_id)
        genesis = _genesis_payload(envelopes)
        if genesis is None or getattr(genesis, "parent_task_id", None) is not None:
            continue  # subtask (or malformed stream): its text rides the root
        if str(getattr(genesis, "agent_name", "") or "").startswith("__"):
            continue  # reserved internal agents — never digest a curation run
        if include_task is not None and not include_task(summary.task_id):
            continue  # out of the host's digest scope — not counted anywhere
        # Transcript before cap check: the envelopes are already in memory
        # (no extra IO), and it keeps the header's dropped count exact —
        # only sessions with digestible text consume or overflow the cap.
        transcript = _session_transcript(
            envelopes, content_store, max_chars=max_chars_per_session
        )
        if not transcript:
            continue
        if len(sections) >= max_sessions:
            omitted += 1
            continue
        last_iso = datetime.fromtimestamp(
            getattr(summary, "last_event_time", 0.0), tz=timezone.utc
        ).isoformat()
        sections.append(
            f"## Session {summary.task_id} (last activity {last_iso})\n{transcript}"
        )
    if not sections:
        return None

    window = (
        f"sessions with activity after {_as_utc(since).isoformat()}"
        if since is not None
        else "all recorded sessions (no previous consolidation run)"
    )
    if include_task is not None:
        window += ", restricted to a host-selected subset of sessions"
    dropped = (
        f"; {omitted} more session(s) with digestible activity in the window "
        f"were omitted (session cap {max_sessions})"
        if omitted
        else ""
    )
    header = (
        "# Recent session activity digest\n"
        f"Window: {window}.\n"
        f"Sessions: {len(sections)} shown, newest first{dropped}.\n"
        f"Per-session transcripts are tail-truncated to "
        f"{max_chars_per_session} characters."
    )
    return header + "\n\n" + "\n\n".join(sections) + "\n"


# ---------------------------------------------------------------------------
# Run entry (decision #11's explicit host-callable)
# ---------------------------------------------------------------------------


def run_consolidation(
    client: Any,
    *,
    memory_root: Path,
    now: Optional[datetime] = None,
    debounce: bool = True,
    debounce_hours: float = DEFAULT_DEBOUNCE_HOURS,
    max_sessions: int = DEFAULT_MAX_SESSIONS,
    max_chars_per_session: int = DEFAULT_MAX_CHARS_PER_SESSION,
    include_task: Optional[Callable[[str], bool]] = None,
    on_seeded: Optional[Callable[[str], None]] = None,
) -> bool:
    """Enqueue one background consolidation run; ``True`` iff one was enqueued.

    The expected "nothing to do" conditions — debounce not elapsed, no
    session activity to digest — return ``False`` without raising and without
    touching the marker. On a real run: the marker is written FIRST (enqueue
    time — an in-flight run debounces its own turn boundaries), then the
    ``__consolidation__`` root task is seeded through the same
    ``seed_start`` + yield-to-ready-queue path the product's background verbs
    use, for a resident worker to drive. An unexpected failure (e.g. the
    consolidation agent missing from the client's registry) still raises —
    that is host mis-wiring, not an expected condition.

    ``include_task`` scopes the digest (see
    :func:`build_consolidation_digest`) so a multi-tenant host runs one
    curation pass per tenant: filter to that tenant's root sessions and point
    ``memory_root`` at that tenant's directory — the per-root marker then
    debounces each tenant independently. ``None`` (default) ⇒ the
    whole-ledger digest, byte-identical to today.

    ``on_seeded`` receives the seeded ``__consolidation__`` root task id after
    the task is durably created (its seed lease still held — no worker can
    claim it yet) and BEFORE it is handed to the ready queue. A host running
    one pass per tenant registers the id in its ``memory_root_resolver``
    mapping here, so the curation run's Engine — and therefore its ``memory_*``
    tools — resolves the SAME tenant store the digest and marker were scoped
    to. The callback must not raise: a raise propagates after the marker
    write and the durable seed, leaving a leased task that is never yielded.
    ``None`` (default, single-tenant) ⇒ no callback, unchanged behaviour.

    ``now`` is injectable for tests; ``None`` ⇒ the wall clock. There is no
    per-seed budget/contract parameter on ``seed_start`` (a task's budget is
    the agent's compile-time default), so the ~10-operation ceiling is
    enforced by the consolidation prompt alone.
    """
    moment = _as_utc(now) if now is not None else datetime.now(timezone.utc)
    if debounce and not consolidation_due(
        memory_root, now=moment, debounce_hours=debounce_hours
    ):
        return False
    since = read_consolidation_marker(memory_root)
    digest = build_consolidation_digest(
        client,
        since=since,
        max_sessions=max_sessions,
        max_chars_per_session=max_chars_per_session,
        include_task=include_task,
    )
    if digest is None:
        return False
    write_consolidation_marker(memory_root, now=moment)
    seeded = client.seed_start(
        goal=_GOAL_PREAMBLE + "\n\n" + digest, agent=CONSOLIDATION_AGENT_NAME
    )
    # Multi-tenant hook: the seed lease is still held here, so the host can
    # bind this task id in its memory_root_resolver before ANY worker can
    # resolve the curation Engine against it.
    if on_seeded is not None:
        on_seeded(seeded.task_id)
    client._yield_seeded_lease(seeded)  # noqa: SLF001 — the background-verb path
    return True
