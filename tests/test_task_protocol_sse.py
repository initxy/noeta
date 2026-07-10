"""T5 acceptance — the core task protocol (SSE multiplexed stream + commands).

Covers the stream-level cursor (catch-up + resume with no dup / no loss), the
canonical EventEnvelope payload, and the command endpoints (202 + ack, truth via
the stream).
"""

from __future__ import annotations

import http.client
import json
import socket
from pathlib import Path

from dataclasses import dataclass
from typing import Optional

from noeta.agent.backend import BackendConfig, EngineRoom, serve_backend
from noeta.agent.backend.stream import (
    decode_cursor,
    discover_tree,
    encode_cursor,
    stream_frames,
)
from noeta.protocols.events import EventEnvelope, TaskCreatedPayload
from noeta.protocols.messages import LLMResponse, TextBlock, Usage
from noeta.sdk import Options
from noeta.testing.fake_llm import FakeLLMProvider

_HEARTBEAT = b": keep-alive\n\n"


def _provider(n: int = 4) -> FakeLLMProvider:
    return FakeLLMProvider(
        responses=[
            LLMResponse(
                stop_reason="end_turn",
                content=[TextBlock(text=f"reply-{i}")],
                usage=Usage(uncached=1, output=1),
            )
            for i in range(n)
        ]
    )


def _room(workspace: Path) -> EngineRoom:
    return EngineRoom(
        Options(
            system_prompt="finish each turn",
            name="main",
            allowed_tools=(),
            permission_mode="bypassPermissions",
        ),
        provider=_provider(),
        workspace_dir=workspace,
    )


def _parse_frames(blob: bytes) -> list[tuple[str, dict]]:
    """Parse ``id: <cursor>\\ndata: <json>\\n\\n`` frames (skip heartbeats)."""
    out: list[tuple[str, dict]] = []
    for chunk in blob.split(b"\n\n"):
        text = chunk.decode("utf-8", "replace")
        cursor = None
        data = None
        for line in text.splitlines():
            if line.startswith("id: "):
                cursor = line[4:]
            elif line.startswith("data: "):
                data = line[6:]
        if cursor is not None and data:
            out.append((cursor, json.loads(data)))
    return out


def _collect_catchup(gen) -> list[tuple[str, dict]]:
    """Drain a stream_frames generator's catch-up phase (until the heartbeat)."""
    frames: list[tuple[str, dict]] = []
    try:
        for chunk in gen:
            if chunk == _HEARTBEAT:
                break
            frames.extend(_parse_frames(chunk))
    finally:
        gen.close()
    return frames


# ---------------------------------------------------------------------------
# Cursor token round-trip
# ---------------------------------------------------------------------------


def test_cursor_roundtrip() -> None:
    marks = {"task-a": 7, "task-b": 3}
    assert decode_cursor(encode_cursor(marks)) == marks
    assert decode_cursor(None) == {}
    assert decode_cursor("!!not-base64!!") == {}


# ---------------------------------------------------------------------------
# stream_frames: catch-up + resume (no dup, no loss)
# ---------------------------------------------------------------------------


def test_stream_catchup_delivers_envelopes(tmp_path: Path) -> None:
    room = _room(tmp_path)
    try:
        task_id = room.start(goal="hi")
        frames = _collect_catchup(
            stream_frames(room, task_id, None, heartbeat_secs=0.2)
        )
    finally:
        room.shutdown()

    assert frames, "catch-up should replay the first turn's envelopes"
    # Payload is the raw canonical envelope, all for this task, seq-ordered.
    seqs = [env["seq"] for _, env in frames]
    assert seqs == sorted(seqs)
    assert all(env["task_id"] == task_id for _, env in frames)
    assert {env["type"] for _, env in frames} >= {"TaskCreated", "MessagesAppended"}
    # The final stream cursor encodes this task's high-water seq.
    last_cursor, last_env = frames[-1]
    assert decode_cursor(last_cursor) == {task_id: last_env["seq"]}


def test_stream_resume_no_dup_no_loss(tmp_path: Path) -> None:
    room = _room(tmp_path)
    try:
        task_id = room.start(goal="hi")
        full = _collect_catchup(
            stream_frames(room, task_id, None, heartbeat_secs=0.2)
        )
        assert len(full) >= 3
        # Resume from the cursor AFTER the 2nd frame.
        resume_cursor = full[1][0]
        resumed = _collect_catchup(
            stream_frames(room, task_id, resume_cursor, heartbeat_secs=0.2)
        )
    finally:
        room.shutdown()

    delivered_again = {env["seq"] for _, env in resumed}
    already_seen = {env["seq"] for _, env in full[:2]}
    # No loss: every envelope after the resume point reappears.
    expected = {env["seq"] for _, env in full[2:]}
    assert delivered_again == expected
    # No dup: nothing at-or-before the cursor is re-sent.
    assert delivered_again.isdisjoint(already_seen)


# ---------------------------------------------------------------------------
# Command endpoints + SSE over HTTP
# ---------------------------------------------------------------------------


def test_command_endpoints_ack_and_stream_over_http(tmp_path: Path) -> None:
    config = BackendConfig(host="127.0.0.1", port=0, workspace_dir=tmp_path)
    server, url, shutdown = serve_backend(config, engine_room=_room(tmp_path))
    host, port = server.server_address[:2]
    try:
        # POST /tasks → 202 + {task_id} (truth rides the stream, not the body).
        conn = http.client.HTTPConnection(host, port, timeout=5)
        conn.request(
            "POST", "/tasks", body=json.dumps({"goal": "hi"}),
            headers={"Content-Type": "application/json"},
        )
        resp = conn.getresponse()
        assert resp.status == 202
        task_id = json.loads(resp.read())["task_id"]
        conn.close()
        assert task_id

        # GET /stream?task=<id> → SSE; read the catch-up frames over the wire.
        sock = socket.create_connection((host, port), timeout=5)
        sock.sendall(
            f"GET /stream?task={task_id} HTTP/1.1\r\n"
            f"Host: {host}\r\nConnection: close\r\n\r\n".encode()
        )
        sock.settimeout(1.5)
        blob = b""
        try:
            while b"MessagesAppended" not in blob:
                buf = sock.recv(8192)
                if not buf:
                    break
                blob += buf
        except socket.timeout:
            pass
        sock.close()

        # The HTTP body starts with the status line + headers; frames follow.
        assert b"text/event-stream" in blob
        body = blob.split(b"\r\n\r\n", 1)[1] if b"\r\n\r\n" in blob else blob
        frames = _parse_frames(body)
        assert frames, "stream should deliver the first turn over HTTP"
        assert all(env["task_id"] == task_id for _, env in frames)
        assert any(env["type"] == "MessagesAppended" for _, env in frames)

        # A second command acks 202 too.
        conn = http.client.HTTPConnection(host, port, timeout=5)
        conn.request(
            "POST", f"/tasks/{task_id}/messages",
            body=json.dumps({"goal": "again"}),
            headers={"Content-Type": "application/json"},
        )
        resp = conn.getresponse()
        assert resp.status == 202
        conn.close()
    finally:
        shutdown()


# ---------------------------------------------------------------------------
# discover_tree — subtree scoping + per-room parent cache
# ---------------------------------------------------------------------------


@dataclass
class _FakeStreamSummary:
    task_id: str
    last_seq: int = 0
    last_event_time: float = 0.0


class _FakeRoom:
    """Duck-typed ``EngineRoom`` stand-in: ``discover_tree``/``_parent_of``
    only ever call ``task_streams()`` and ``events_after()``, so a fake
    exercising just those two lets the tree-walk + cache logic be pinned
    without driving a real engine through a full ``spawn_subagent`` turn.

    Deliberately a plain class (not a ``@dataclass``, which would default to
    an ``eq``-based ``__hash__ = None`` and break its use as a
    ``WeakKeyDictionary`` key in :func:`discover_tree`'s parent cache) — the
    default identity-based hash/eq is exactly what the real ``EngineRoom``
    also relies on.
    """

    def __init__(self, parents: dict[str, Optional[str]]) -> None:
        self.parents = parents
        self.reads: list[str] = []

    def task_streams(self) -> list[_FakeStreamSummary]:
        return [_FakeStreamSummary(task_id=tid) for tid in self.parents]

    def events_after(self, task_id: str, after_seq: Optional[int]) -> list[EventEnvelope]:
        self.reads.append(task_id)
        payload = TaskCreatedPayload(
            goal="g", policy_name="p", parent_task_id=self.parents[task_id]
        )
        return [EventEnvelope.build(task_id=task_id, type="TaskCreated", payload=payload)]


def test_discover_tree_walks_parent_links_to_the_requested_root_only() -> None:
    room = _FakeRoom(
        parents={
            "root": None,
            "child-a": "root",
            "child-b": "root",
            "grandchild": "child-a",
            "unrelated-root": None,
            "unrelated-child": "unrelated-root",
        }
    )
    tree = discover_tree(room, "root")
    assert tree == {"root", "child-a", "child-b", "grandchild"}


def test_discover_tree_caches_resolved_parents_across_calls() -> None:
    """A stream's ``parent_task_id`` never changes once created, so a second
    discovery against the same room must not re-read already-resolved
    streams — this is the fix for the O(total tasks ever) per-connect cost."""
    room = _FakeRoom(parents={"root": None, "child": "root"})

    discover_tree(room, "root")
    assert set(room.reads) == {"root", "child"}

    room.reads.clear()
    discover_tree(room, "root")
    assert room.reads == []

    # A new stream appearing later is resolved on the next call; the
    # already-cached ones stay untouched.
    room.parents["late-child"] = "root"
    tree = discover_tree(room, "root")
    assert room.reads == ["late-child"]
    assert tree == {"root", "child", "late-child"}
