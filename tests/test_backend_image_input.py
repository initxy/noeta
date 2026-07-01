"""Backend image-input ingestion — base64 attachments → ``ImageBlock`` (T2).

The thin backend accepts ``images: [{media_type, data_base64}]`` on the two
goal-bearing commands (``POST /tasks`` and ``POST /tasks/{id}/messages``).
``noeta.agent.backend.image_input.build_image_blocks`` validates + decodes each
entry, stores the bytes through ``EngineRoom.put_content`` (content-addressed),
and wraps the ref in an ``ImageBlock`` that rides the user turn. A bad attachment
(non-whitelisted MIME / illegal base64 / over 5MB) is the client's fault → HTTP
400, task neither created nor advanced.

Covered:

* ``build_image_blocks`` directly (real ``EngineRoom``): decode/validate/put,
  the content-address (``ref.hash == sha256(body)``) round-trips through
  ``get_content``, and each reject path raises ``ImageInputError``;
* HTTP happy path on both commands: the image lands as an ``ImageBlock`` in the
  user message and ``GET /content/{hash}`` returns the original decoded bytes;
* HTTP 400 mapping for bad MIME / bad base64 / over-5MB, with no task created;
* the no-images path stays the text-only path (regression guard).
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import json
import time
from pathlib import Path
from typing import Any

import pytest

from noeta.agent.backend import BackendConfig, EngineRoom, serve_backend
from noeta.agent.backend.image_input import (
    MAX_IMAGE_BYTES,
    ImageInputError,
    build_image_blocks,
)
from noeta.protocols.canonical import from_canonical_bytes
from noeta.protocols.messages import ImageBlock, LLMResponse, TextBlock, Usage
from noeta.sdk import Options
from noeta.testing.fake_llm import FakeLLMProvider

# A real 1x1 transparent PNG (valid magic bytes so the /content sniff agrees).
PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8z8BQDwAE"
    "hQGAhKmMIQAAAABJRU5ErkJggg=="
)
PNG_B64 = base64.b64encode(PNG_BYTES).decode("ascii")


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------


def _provider(n: int = 6) -> FakeLLMProvider:
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


def _post(host: str, port: int, path: str, payload: dict[str, Any]) -> tuple[int, Any]:
    conn = http.client.HTTPConnection(host, port, timeout=10)
    conn.request(
        "POST",
        path,
        body=json.dumps(payload),
        headers={"Content-Type": "application/json"},
    )
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    return resp.status, (json.loads(data) if data else None)


def _get_json(host: str, port: int, path: str) -> tuple[int, Any]:
    conn = http.client.HTTPConnection(host, port, timeout=10)
    conn.request("GET", path)
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    return resp.status, (json.loads(data) if data else None)


def _get_bytes(host: str, port: int, path: str) -> tuple[int, bytes]:
    conn = http.client.HTTPConnection(host, port, timeout=10)
    conn.request("GET", path)
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    return resp.status, data


def _user_image_hashes(room: EngineRoom, task_id: str) -> list[str]:
    """The ContentRef hashes of every ``ImageBlock`` in ``task_id``'s user turns.

    The folded message view drops images (D4), so dereference each
    ``MessagesAppended`` envelope's ``messages_ref`` and pick the user-role
    ``ImageBlock``s out of the rebuilt messages.
    """
    out: list[str] = []
    for env in room.events(task_id):
        payload = getattr(env, "payload", None)
        if type(payload).__name__ != "MessagesAppendedPayload":
            continue
        raw = room.get_content(payload.messages_ref.hash)
        if raw is None:
            continue
        for msg in from_canonical_bytes(raw):
            if getattr(msg, "role", None) != "user":
                continue
            for block in getattr(msg, "content", []):
                if isinstance(block, ImageBlock):
                    out.append(block.source.hash)
    return out


def _wait_user_image(room: EngineRoom, task_id: str, want_hash: str) -> bool:
    """Poll for ``want_hash`` to appear as a user ``ImageBlock`` (insurance)."""
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if want_hash in _user_image_hashes(room, task_id):
            return True
        time.sleep(0.02)
    return False


# ---------------------------------------------------------------------------
# build_image_blocks — direct, against a real EngineRoom
# ---------------------------------------------------------------------------


def test_build_image_blocks_decodes_and_stores(tmp_path: Path) -> None:
    room = _room(tmp_path)
    blocks = build_image_blocks(
        room, [{"media_type": "image/png", "data_base64": PNG_B64}]
    )
    assert len(blocks) == 1
    ref = blocks[0].source
    # Content-addressed: the ref hash is sha256 of the decoded bytes …
    assert ref.hash == hashlib.sha256(PNG_BYTES).hexdigest()
    assert ref.media_type == "image/png"
    # … and dereferencing returns the original bytes.
    assert room.get_content(ref.hash) == PNG_BYTES


def test_build_image_blocks_empty_and_none(tmp_path: Path) -> None:
    room = _room(tmp_path)
    assert build_image_blocks(room, None) == []
    assert build_image_blocks(room, []) == []


def test_build_image_blocks_normalizes_media_type(tmp_path: Path) -> None:
    room = _room(tmp_path)
    blocks = build_image_blocks(
        room, [{"media_type": "  IMAGE/PNG ", "data_base64": PNG_B64}]
    )
    assert blocks[0].source.media_type == "image/png"


def test_build_image_blocks_rejects_bad_media_type(tmp_path: Path) -> None:
    room = _room(tmp_path)
    with pytest.raises(ImageInputError):
        build_image_blocks(
            room, [{"media_type": "image/svg+xml", "data_base64": PNG_B64}]
        )


def test_build_image_blocks_rejects_bad_base64(tmp_path: Path) -> None:
    room = _room(tmp_path)
    with pytest.raises(ImageInputError):
        build_image_blocks(
            room, [{"media_type": "image/png", "data_base64": "@@not-base64@@"}]
        )


def test_build_image_blocks_rejects_oversize(tmp_path: Path) -> None:
    room = _room(tmp_path)
    oversize = base64.b64encode(b"\x00" * (MAX_IMAGE_BYTES + 1)).decode("ascii")
    with pytest.raises(ImageInputError):
        build_image_blocks(
            room, [{"media_type": "image/png", "data_base64": oversize}]
        )


def test_build_image_blocks_rejects_non_dict_entry(tmp_path: Path) -> None:
    room = _room(tmp_path)
    with pytest.raises(ImageInputError):
        build_image_blocks(room, ["not-an-object"])


# ---------------------------------------------------------------------------
# HTTP — happy path through task_protocol
# ---------------------------------------------------------------------------


def test_create_task_with_image(tmp_path: Path) -> None:
    room = _room(tmp_path)
    server, _url, shutdown = serve_backend(
        BackendConfig(host="127.0.0.1", port=0, workspace_dir=tmp_path),
        engine_room=room,
    )
    host, port = server.server_address[:2]
    try:
        status, body = _post(
            host,
            port,
            "/tasks",
            {
                "goal": "what is in this image?",
                "permission_mode": "bypassPermissions",
                "images": [{"media_type": "image/png", "data_base64": PNG_B64}],
            },
        )
        assert status == 202, body
        task_id = body["task_id"]
        want = hashlib.sha256(PNG_BYTES).hexdigest()
        # The image rode the opening user turn as an ImageBlock.
        assert _wait_user_image(room, task_id, want)
        # GET /content/{hash} returns the original decoded bytes.
        c_status, raw = _get_bytes(host, port, f"/content/{want}")
        assert c_status == 200
        assert raw == PNG_BYTES
    finally:
        shutdown()


def test_send_goal_with_image(tmp_path: Path) -> None:
    room = _room(tmp_path)
    server, _url, shutdown = serve_backend(
        BackendConfig(host="127.0.0.1", port=0, workspace_dir=tmp_path),
        engine_room=room,
    )
    host, port = server.server_address[:2]
    try:
        # Open a text-only conversation first.
        status, body = _post(
            host,
            port,
            "/tasks",
            {"goal": "first turn", "permission_mode": "bypassPermissions"},
        )
        assert status == 202, body
        task_id = body["task_id"]

        # Follow-up turn carries the image.
        status, body = _post(
            host,
            port,
            f"/tasks/{task_id}/messages",
            {
                "goal": "and this image?",
                "permission_mode": "bypassPermissions",
                "images": [{"media_type": "image/png", "data_base64": PNG_B64}],
            },
        )
        assert status == 202, body
        want = hashlib.sha256(PNG_BYTES).hexdigest()
        assert _wait_user_image(room, task_id, want)
        c_status, raw = _get_bytes(host, port, f"/content/{want}")
        assert c_status == 200
        assert raw == PNG_BYTES
    finally:
        shutdown()


# ---------------------------------------------------------------------------
# HTTP — 400 mapping (bad attachment → no task created/advanced)
# ---------------------------------------------------------------------------


def test_create_task_bad_media_type_is_400_no_task(tmp_path: Path) -> None:
    room = _room(tmp_path)
    server, _url, shutdown = serve_backend(
        BackendConfig(host="127.0.0.1", port=0, workspace_dir=tmp_path),
        engine_room=room,
    )
    host, port = server.server_address[:2]
    try:
        status, body = _post(
            host,
            port,
            "/tasks",
            {
                "goal": "nope",
                "images": [{"media_type": "image/svg+xml", "data_base64": PNG_B64}],
            },
        )
        assert status == 400, body
        assert "error" in body
        # No task was created (start was never reached).
        list_status, sessions = _get_json(host, port, "/tasks")
        assert list_status == 200
        assert sessions == []
    finally:
        shutdown()


def test_create_task_bad_base64_is_400(tmp_path: Path) -> None:
    room = _room(tmp_path)
    server, _url, shutdown = serve_backend(
        BackendConfig(host="127.0.0.1", port=0, workspace_dir=tmp_path),
        engine_room=room,
    )
    host, port = server.server_address[:2]
    try:
        status, body = _post(
            host,
            port,
            "/tasks",
            {
                "goal": "nope",
                "images": [{"media_type": "image/png", "data_base64": "@@bad@@"}],
            },
        )
        assert status == 400, body
        _, sessions = _get_json(host, port, "/tasks")
        assert sessions == []
    finally:
        shutdown()


def test_create_task_oversize_is_400(tmp_path: Path) -> None:
    room = _room(tmp_path)
    server, _url, shutdown = serve_backend(
        BackendConfig(host="127.0.0.1", port=0, workspace_dir=tmp_path),
        engine_room=room,
    )
    host, port = server.server_address[:2]
    try:
        oversize = base64.b64encode(b"\x00" * (MAX_IMAGE_BYTES + 1)).decode("ascii")
        status, body = _post(
            host,
            port,
            "/tasks",
            {
                "goal": "nope",
                "images": [{"media_type": "image/png", "data_base64": oversize}],
            },
        )
        assert status == 400, body
        _, sessions = _get_json(host, port, "/tasks")
        assert sessions == []
    finally:
        shutdown()


def test_send_goal_bad_image_does_not_advance(tmp_path: Path) -> None:
    room = _room(tmp_path)
    server, _url, shutdown = serve_backend(
        BackendConfig(host="127.0.0.1", port=0, workspace_dir=tmp_path),
        engine_room=room,
    )
    host, port = server.server_address[:2]
    try:
        status, body = _post(
            host,
            port,
            "/tasks",
            {"goal": "first turn", "permission_mode": "bypassPermissions"},
        )
        assert status == 202, body
        task_id = body["task_id"]
        before = len(room.events(task_id))

        status, _ = _post(
            host,
            port,
            f"/tasks/{task_id}/messages",
            {
                "goal": "bad image",
                "images": [{"media_type": "text/plain", "data_base64": PNG_B64}],
            },
        )
        assert status == 400
        # The conversation did not advance (no new turn appended).
        assert len(room.events(task_id)) == before
        assert _user_image_hashes(room, task_id) == []
    finally:
        shutdown()


# ---------------------------------------------------------------------------
# Regression — the no-images path is unchanged (text-only)
# ---------------------------------------------------------------------------


def test_create_task_without_images_is_text_only(tmp_path: Path) -> None:
    room = _room(tmp_path)
    server, _url, shutdown = serve_backend(
        BackendConfig(host="127.0.0.1", port=0, workspace_dir=tmp_path),
        engine_room=room,
    )
    host, port = server.server_address[:2]
    try:
        # Omitted images and an explicit empty list both behave as text-only.
        for payload in (
            {"goal": "hello there", "permission_mode": "bypassPermissions"},
            {"goal": "hello again", "permission_mode": "bypassPermissions", "images": []},
        ):
            status, body = _post(host, port, "/tasks", payload)
            assert status == 202, body
            task_id = body["task_id"]
            assert _user_image_hashes(room, task_id) == []
    finally:
        shutdown()
