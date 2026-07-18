"""Token streaming end to end (SDK 0.1.7+, token-streaming projection).

FakeLLMProvider (the everyday mock) does not implement StreamingProvider, so
the runtime seam does not stream and the mock path has no deltas — all other
tests keep their behavior. Here FakeStreamingLLMProvider triggers the
delta_sink path, verified through real uvicorn + httpx SSE:
  - SSE receives event: delta frames while the message is processed
    (token by token, no id line / seq=None)
  - the durable assistant_text / turn_finished arrive afterwards
  - a reconnect (since_seq replay) shows no delta replay (deltas never land in
    the EventLog)
"""
from __future__ import annotations

import threading
import time

import pytest

from noeta.protocols.messages import LLMResponse, StreamDelta, TextBlock, Usage
from noeta.testing.fake_llm import FakeStreamingLLMProvider

from tests.conftest import (
    create_session,
    login,
    personal_space_id,
    read_sse,
    wait_status,
)

_FULL_TEXT = "Hello, this is a streamed reply."


def _streaming_provider() -> FakeStreamingLLMProvider:
    """A streaming provider ending in end_turn: the first LLM call streams
    deltas along the way."""
    return FakeStreamingLLMProvider(
        # Extra responses in reserve so unexpected additional calls do not
        # exhaust the script
        responses=[
            LLMResponse(
                stop_reason="end_turn",
                content=[TextBlock(text=_FULL_TEXT)],
                usage=Usage(uncached=1, output=1),
            ),
            LLMResponse(
                stop_reason="end_turn",
                content=[TextBlock(text=_FULL_TEXT)],
                usage=Usage(uncached=1, output=1),
            ),
        ],
        deltas=[
            [
                StreamDelta(kind="text", text="Hello,", index=0),
                StreamDelta(kind="text", text=" this is", index=0),
                StreamDelta(kind="text", text=" a streamed reply.", index=0),
            ],
            [],
        ],
    )


@pytest.fixture
def streaming_client(make_client, monkeypatch):
    """Swap build_provider for FakeStreamingLLMProvider and start real
    uvicorn."""
    provider = _streaming_provider()
    monkeypatch.setattr(
        "noeta.agent.host.service.build_provider", lambda s: (provider, "mock")
    )
    return make_client(LLM_PROVIDER="mock")


def _events(client, sid: str, stop: tuple[str, ...] = ("turn_finished",)):
    return read_sse(client, sid, stop_types=stop, timeout=20)


def test_delta_frames_on_sse(streaming_client):
    """SSE receives delta frames while the message is processed, and the
    durable messages arrive afterwards.

    The SSE stream must be open before sending: deltas are an ephemeral
    projection pushed only to online subscribers of a running turn; connecting
    after the turn ends replays only durable events and never shows a delta.
    """
    client = streaming_client
    login(client, "alice")
    space = personal_space_id(client)
    sid = create_session(client, space)

    ev: list[dict] = []

    def consume() -> None:
        ev.extend(_events(client, sid))

    t = threading.Thread(target=consume)
    t.start()
    time.sleep(0.3)  # wait for the SSE connection (empty replay, then live subscription) so live deltas are received
    client.post(f"/api/v1/sessions/{sid}/messages", json={"content": "Hello"})
    t.join(timeout=25)
    assert not t.is_alive(), "the SSE consumer thread did not converge at turn_finished"
    types = [e["event"] for e in ev]

    deltas = [e for e in ev if e["event"] == "delta"]
    assert deltas, f"no delta frames received; event types: {types}"

    # Frame structure: seq=None (no id line), data carries call_id/kind/text/index
    d0 = deltas[0]
    assert d0["seq"] is None
    data = d0["data"]
    assert set(data) == {"call_id", "kind", "text", "index", "_task"}
    assert data["kind"] == "text"
    assert isinstance(data["call_id"], str) and data["call_id"]
    assert data["index"] == 0

    # The accumulated text of the same call_id + same index = the full text
    call_id = data["call_id"]
    assert all(d["data"]["call_id"] == call_id for d in deltas)
    assert all(d["data"]["index"] == 0 for d in deltas)
    assembled = "".join(d["data"]["text"] for d in deltas)
    assert assembled == _FULL_TEXT, f"accumulated delta text mismatch: {assembled!r}"

    # The durable truth arrives afterwards: assistant_text (full text) +
    # turn_finished
    assert any(e["event"] == "assistant_text" for e in ev), types
    at = next(e for e in ev if e["event"] == "assistant_text")
    assert at["data"]["text"] == _FULL_TEXT
    assert any(e["event"] == "turn_finished" for e in ev), types

    # Deltas appear before the durable assistant_text (the live projection
    # precedes the truth)
    delta_idx = types.index("delta")
    text_idx = types.index("assistant_text")
    assert delta_idx < text_idx


def test_replay_excludes_delta(streaming_client):
    """A reconnect (since_seq replay) shows no delta replay: deltas never land
    in the EventLog."""
    client = streaming_client
    login(client, "alice")
    space = personal_space_id(client)
    sid = create_session(client, space)

    client.post(f"/api/v1/sessions/{sid}/messages", json={"content": "Hello"})
    wait_status(client, sid, {"idle"}, timeout=20)

    # Consume the full event stream first to get the last seq
    live = _events(client, sid)
    last_seq = max(e["seq"] for e in live if e["seq"] is not None)

    # Reconnect: replay from last_seq (since_seq=last_seq → no new durable
    # events)
    replayed = read_sse(
        client, sid, since_seq=last_seq, stop_types=("replay_done",), timeout=20
    )
    rtypes = [e["event"] for e in replayed]
    # The replay path never contains deltas
    assert "delta" not in rtypes, f"delta replayed on reconnect: {rtypes}"
    # replay_done arrives (the replay endpoint terminates normally)
    assert "replay_done" in rtypes, rtypes
