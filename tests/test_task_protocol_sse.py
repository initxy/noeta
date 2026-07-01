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

from noeta.agent.backend import BackendConfig, EngineRoom, serve_backend
from noeta.agent.backend.stream import (
    decode_cursor,
    encode_cursor,
    stream_frames,
)
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
