"""stream — the SSE multiplexed envelope stream.

One SSE stream per
conversation carries the root Task **and all its subtasks'** ``EventEnvelope``s;
the frontend demultiplexes by ``taskId``. Resume rides a **stream-level cursor**
(the SSE ``id:``), NOT the per-task ``seq`` — the merged stream interleaves
several tasks whose ``seq``s are each monotonic only within their own task, so
the cursor compactly encodes "how far each task has been pushed" (a ``{task_id: seq}`` map).
On reconnect with ``Last-Event-ID``, each sub-stream resumes from its own
per-task cursor and the streams re-merge — no duplicate, no loss (the frontend
still folds by ``taskId`` then ``seq``, so cross-task merge order is irrelevant).

The payload is the canonical envelope, wired **raw** (``noeta.sdk.envelope_to_dict``);
the backend does not pre-project (D7). Large objects ride a ``ContentRef`` only —
their bytes come from the T6 ``/content/{hash}`` service, never this stream.

Besides envelopes, the stream carries **ephemeral token-delta frames**
(``event: delta``, ADR ``token-streaming-projection.md``): named SSE events
with NO ``id:`` line, so the resume cursor never moves for a delta and a
reconnect replays none of them — the final ``MessagesAppended`` envelope
repaints the truth. Delta frames may be dropped under backpressure; envelope
frames are never dropped.
"""

from __future__ import annotations

import base64
import json
import queue
import threading
from typing import Any, Iterator

from noeta.sdk import envelope_to_dict

from noeta.agent.backend.engine_room import EngineRoom


# ---------------------------------------------------------------------------
# Stream cursor: {task_id: last_seq} <-> compact url-safe token
# ---------------------------------------------------------------------------


def encode_cursor(marks: dict[str, int]) -> str:
    raw = json.dumps(marks, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(token: str | None) -> dict[str, int]:
    if not token:
        return {}
    pad = "=" * (-len(token) % 4)
    try:
        raw = base64.urlsafe_b64decode(token + pad)
        data = json.loads(raw)
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, int] = {}
    for k, v in data.items():
        try:
            out[str(k)] = int(v)
        except (TypeError, ValueError):
            continue
    return out


# ---------------------------------------------------------------------------
# SSE frame formatting
# ---------------------------------------------------------------------------


def format_frame(envelope_obj: dict[str, Any], cursor: str) -> bytes:
    """One SSE event: ``id:`` = stream cursor, ``data:`` = canonical envelope."""
    body = json.dumps(envelope_obj, separators=(",", ":"))
    return f"id: {cursor}\ndata: {body}\n\n".encode("utf-8")


def format_delta_frame(task_id: str, call_id: str, delta: Any) -> bytes:
    """One ephemeral token-delta frame: named ``event: delta``, **no** ``id:``.

    Omitting the ``id:`` line is load-bearing: the SSE cursor must never
    advance for a delta, so ``Last-Event-ID`` resume replays envelopes only
    and a reconnect loses deltas by construction (the final event repaints).
    Named, so ``EventSource.onmessage`` (the envelope path) never sees it.
    """
    body = json.dumps(
        {
            "task_id": task_id,
            "call_id": call_id,
            "kind": delta.kind,
            "text": delta.text,
            "index": delta.index,
        },
        separators=(",", ":"),
    )
    return f"event: delta\ndata: {body}\n\n".encode("utf-8")


_HEARTBEAT = b": keep-alive\n\n"

#: Backpressure guard for delta frames: when a connection's pending queue is
#: deeper than this, new DELTA frames are dropped (never envelope frames — the
#: durable stream stays lossless; a lost delta is repainted by the final event).
_DELTA_QUEUE_LIMIT = 500


# ---------------------------------------------------------------------------
# Subtask-tree discovery
# ---------------------------------------------------------------------------


def _parent_of(engine_room: EngineRoom, task_id: str) -> str | None:
    """Read ``task_id``'s genesis ``TaskCreated`` → its ``parent_task_id``."""
    for env in engine_room.events_after(task_id, None):
        if env.type == "TaskCreated":
            return getattr(env.payload, "parent_task_id", None)
    return None


def discover_tree(engine_room: EngineRoom, root: str) -> set[str]:
    """Every task in ``root``'s subtree (root + transitive subtasks)."""
    parent: dict[str, str | None] = {}
    for summary in engine_room.task_streams():
        tid = summary.task_id
        parent[tid] = _parent_of(engine_room, tid)
    tree = {root}
    changed = True
    while changed:
        changed = False
        for tid, p in parent.items():
            if tid not in tree and p in tree:
                tree.add(tid)
                changed = True
    return tree


# ---------------------------------------------------------------------------
# The multiplexed stream
# ---------------------------------------------------------------------------


def stream_frames(
    engine_room: EngineRoom,
    root: str,
    last_event_id: str | None,
    *,
    heartbeat_secs: float = 15.0,
) -> Iterator[bytes]:
    """Yield SSE frames for ``root``'s subtree, resuming from ``last_event_id``.

    Subscribes BEFORE catch-up so no envelope committed mid-catch-up is lost;
    the live loop skips any envelope whose ``seq`` the catch-up already
    delivered (``seq <= mark``), so there is no duplicate either.

    Also subscribes to the delta hub: ephemeral token-delta frames for the
    same subtree interleave with the envelopes on the SAME pending queue
    (already pre-formatted — see :func:`format_delta_frame`), bypassing the
    cursor, the seq dedup, and ``marks`` entirely.
    """
    marks = decode_cursor(last_event_id)
    # Queue entries are either envelope objects (the durable path — formatted
    # at drain time so ``marks`` sees them) or pre-formatted ``bytes`` (the
    # ephemeral delta path). ``isinstance(item, bytes)`` is the discriminator.
    pending: "queue.Queue[Any]" = queue.Queue()
    tree: set[str] = set()
    lock = threading.Lock()

    def on_env(env: Any) -> None:
        # Fires on a worker thread post-commit (all tasks). Filter to the tree;
        # a new subtask joins when its TaskCreated names an in-tree parent.
        with lock:
            in_tree = env.task_id in tree
            if not in_tree and env.type == "TaskCreated":
                parent = getattr(env.payload, "parent_task_id", None)
                if env.task_id == root or parent in tree:
                    tree.add(env.task_id)
                    in_tree = True
        if in_tree:
            pending.put(env)

    def on_delta(task_id: str, call_id: str, delta: Any) -> None:
        # Fires on the LLM drive thread while a streaming call is in flight.
        # ``tree`` mutates concurrently (catch-up seeding on the drain thread,
        # subtask joins in on_env on worker threads), so membership is read
        # under the SAME lock those writers hold — the pattern on_env already
        # uses. A delta for a subtask that has not joined yet is dropped:
        # deltas are ephemeral previews, the final envelope carries the truth.
        with lock:
            in_tree = task_id in tree
        if not in_tree:
            return
        # Backpressure: a slow consumer must never make deltas pile up behind
        # the lossless envelope stream — drop the delta, never an envelope.
        if pending.qsize() > _DELTA_QUEUE_LIMIT:
            return
        pending.put(format_delta_frame(task_id, call_id, delta))

    unsub = engine_room.subscribe(on_env)
    unsub_deltas = engine_room.subscribe_deltas(on_delta)
    try:
        with lock:
            tree |= discover_tree(engine_room, root)
            tree_now = sorted(tree)
        # Catch-up: each sub-stream from its own per-task cursor. A task with no
        # mark resumes from the start (``after_seq=None``); ``-1`` as the dedup
        # floor so a genesis envelope at ``seq == 0`` is NOT skipped.
        for tid in tree_now:
            for env in engine_room.events_after(tid, marks.get(tid)):
                if env.seq <= marks.get(env.task_id, -1):
                    continue
                marks[env.task_id] = env.seq
                yield format_frame(envelope_to_dict(env), encode_cursor(marks))
        # Live: drain the queue, deduping against catch-up by per-task seq.
        while True:
            try:
                item = pending.get(timeout=heartbeat_secs)
            except queue.Empty:
                # No event within the heartbeat window: emit a comment frame.
                # This also bounds how long a consumer blocks — on server
                # shutdown the next write fails and the generator terminates,
                # so no explicit stop sentinel is needed.
                yield _HEARTBEAT
                continue
            if isinstance(item, bytes):
                # Pre-formatted delta frame: ephemeral, no seq — bypasses the
                # dedup and never touches ``marks`` (the cursor stands still).
                yield item
                continue
            env = item
            if env.seq <= marks.get(env.task_id, -1):
                continue
            marks[env.task_id] = env.seq
            yield format_frame(envelope_to_dict(env), encode_cursor(marks))
    finally:
        unsub_deltas()
        unsub()
