"""Composer image input — base64 attachments → ``ImageBlock`` on the user turn.

``POST /sessions/{id}/messages`` accepts ``images: [{media_type, data_base64}]``.
``noeta.agent.api.image_input.build_image_blocks`` validates + decodes each
entry, stores the bytes through ``AgentService.put_content`` (content-
addressed), and wraps the ref in an ``ImageBlock`` that rides the seeded user
turn. A bad attachment (non-whitelisted MIME / illegal base64 / over 5MB) is
the client's fault → HTTP 400, the turn is never seeded.

Covered:

* HTTP happy path: the image lands on the user turn — the ``user_message`` UI
  event exposes ``images: [{hash, media_type}]`` and ``GET /content/{hash}``
  returns the original decoded bytes with a sniffed image Content-Type;
* image-only messages (no text) are accepted; text-only without images keeps
  the 422 empty-content rejection;
* HTTP 400 mapping for bad MIME / bad base64 / over-5MB, with the session
  left idle (nothing seeded);
* ``build_image_blocks`` directly (stub service): decode/validate/store and
  each reject path raising ``ImageInputError``.
"""
from __future__ import annotations

import base64
import hashlib
from types import SimpleNamespace

import pytest

from noeta.agent.api.image_input import (
    ALLOWED_IMAGE_TYPES,
    MAX_IMAGE_BYTES,
    ImageInputError,
    build_image_blocks,
)
from tests.conftest import create_session, login, read_sse, wait_status

# A real 1x1 transparent PNG (valid magic bytes so the /content sniff agrees).
PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8z8BQDwAE"
    "hQGAhKmMIQAAAABJRU5ErkJggg=="
)
PNG_B64 = base64.b64encode(PNG_BYTES).decode("ascii")
PNG_HASH = hashlib.sha256(PNG_BYTES).hexdigest()


# ---------------------------------------------------------------------------
# HTTP — happy path
# ---------------------------------------------------------------------------


def test_message_with_image_reaches_user_turn_and_reads_back(client):
    login(client)
    sid = create_session(client)

    resp = client.post(
        f"/api/v1/sessions/{sid}/messages",
        json={
            "content": "What is in this screenshot?",
            "images": [{"media_type": "image/png", "data_base64": PNG_B64}],
        },
    )
    assert resp.status_code == 202, resp.text

    # The mock asks a clarifying question on the first turn → waiting.
    wait_status(client, sid, {"waiting", "idle"})
    events = read_sse(client, sid, stop_types=("question", "turn_finished"))
    user_msgs = [e for e in events if e["event"] == "user_message"]
    assert user_msgs, f"no user_message in {[e['event'] for e in events]}"
    data = user_msgs[0]["data"]
    assert data["content"] == "What is in this screenshot?"
    assert data["images"] == [{"hash": PNG_HASH, "media_type": "image/png"}]

    # Content-addressed read-back: the same bytes, with a sniffed image type.
    content = client.get(f"/api/v1/content/{PNG_HASH}")
    assert content.status_code == 200
    assert content.content == PNG_BYTES
    assert content.headers["content-type"].startswith("image/png")


def test_image_only_message_is_accepted(client):
    login(client)
    sid = create_session(client)

    resp = client.post(
        f"/api/v1/sessions/{sid}/messages",
        json={"images": [{"media_type": "image/png", "data_base64": PNG_B64}]},
    )
    assert resp.status_code == 202, resp.text
    wait_status(client, sid, {"waiting", "idle"})
    events = read_sse(client, sid, stop_types=("question", "turn_finished"))
    user_msgs = [e for e in events if e["event"] == "user_message"]
    assert user_msgs, f"no user_message in {[e['event'] for e in events]}"
    data = user_msgs[0]["data"]
    assert data["content"] == ""
    assert data["images"] == [{"hash": PNG_HASH, "media_type": "image/png"}]


def test_text_only_user_message_carries_no_images_field(client):
    """Regression guard: the pre-image event vocabulary stays byte-identical
    for text-only turns (omitted images / an explicit empty list)."""
    login(client)
    sid = create_session(client)

    resp = client.post(
        f"/api/v1/sessions/{sid}/messages",
        json={"content": "plain text", "images": []},
    )
    assert resp.status_code == 202, resp.text
    wait_status(client, sid, {"waiting", "idle"})
    events = read_sse(client, sid, stop_types=("question", "turn_finished"))
    user_msgs = [e for e in events if e["event"] == "user_message"]
    assert user_msgs
    assert user_msgs[0]["data"] == {"content": "plain text"}


def test_empty_message_without_images_is_422(client):
    login(client)
    sid = create_session(client)
    resp = client.post(f"/api/v1/sessions/{sid}/messages", json={"content": ""})
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# HTTP — 400 mapping (bad attachment → nothing seeded)
# ---------------------------------------------------------------------------


def _assert_rejected_and_idle(client, sid: str, images: list) -> str:
    resp = client.post(
        f"/api/v1/sessions/{sid}/messages",
        json={"content": "nope", "images": images},
    )
    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    # The turn was never seeded: the session did not flip to running.
    status = client.get(f"/api/v1/sessions/{sid}").json()["session"]["status"]
    assert status == "idle"
    return detail


def test_bad_media_type_is_400(client):
    login(client)
    sid = create_session(client)
    detail = _assert_rejected_and_idle(
        client, sid, [{"media_type": "image/svg+xml", "data_base64": PNG_B64}]
    )
    assert "image/svg+xml" in detail


def test_bad_base64_is_400(client):
    login(client)
    sid = create_session(client)
    detail = _assert_rejected_and_idle(
        client, sid, [{"media_type": "image/png", "data_base64": "@@bad@@"}]
    )
    assert "base64" in detail


def test_oversize_image_is_400(client):
    login(client)
    sid = create_session(client)
    oversize = base64.b64encode(b"\x00" * (MAX_IMAGE_BYTES + 1)).decode("ascii")
    detail = _assert_rejected_and_idle(
        client, sid, [{"media_type": "image/png", "data_base64": oversize}]
    )
    assert "5MB" in detail


# ---------------------------------------------------------------------------
# build_image_blocks directly (stub service)
# ---------------------------------------------------------------------------


class _StubService:
    """Records put_content calls and hands back a content-addressed ref."""

    def __init__(self) -> None:
        self.puts: list[tuple[bytes, str]] = []

    def put_content(self, body: bytes, *, media_type: str):
        self.puts.append((body, media_type))
        return SimpleNamespace(
            hash=hashlib.sha256(body).hexdigest(),
            size=len(body),
            media_type=media_type,
        )


def test_build_image_blocks_decodes_stores_and_wraps():
    service = _StubService()
    blocks = build_image_blocks(
        service,
        [
            {"media_type": "image/png", "data_base64": PNG_B64},
            # Normalization: mixed case / surrounding whitespace still passes.
            {"media_type": "  IMAGE/JPEG ", "data_base64": PNG_B64},
        ],
    )
    assert [b.source.hash for b in blocks] == [PNG_HASH, PNG_HASH]
    assert service.puts == [(PNG_BYTES, "image/png"), (PNG_BYTES, "image/jpeg")]


def test_build_image_blocks_empty_input_is_noop():
    service = _StubService()
    assert build_image_blocks(service, None) == []
    assert build_image_blocks(service, []) == []
    assert service.puts == []


@pytest.mark.parametrize(
    "raw",
    [
        "not-a-list",
        ["not-a-dict"],
        [{"data_base64": PNG_B64}],  # missing media_type
        [{"media_type": "text/plain", "data_base64": PNG_B64}],  # not whitelisted
        [{"media_type": "image/png"}],  # missing data
        [{"media_type": "image/png", "data_base64": "@@bad@@"}],  # illegal base64
        [
            {
                "media_type": "image/png",
                "data_base64": base64.b64encode(
                    b"\x00" * (MAX_IMAGE_BYTES + 1)
                ).decode("ascii"),
            }
        ],
    ],
)
def test_build_image_blocks_rejects(raw):
    service = _StubService()
    with pytest.raises(ImageInputError):
        build_image_blocks(service, raw)
    # Whitelist rejection happens before any store write; the oversize case
    # decodes first but must not store either.
    assert service.puts == []


def test_whitelist_matches_frontend():
    assert ALLOWED_IMAGE_TYPES == {
        "image/png", "image/jpeg", "image/gif", "image/webp"
    }
    assert MAX_IMAGE_BYTES == 5 * 1024 * 1024
