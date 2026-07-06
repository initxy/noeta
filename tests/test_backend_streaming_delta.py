"""Token streaming slice 3 — the backend delta hub + SSE delta frames.

Deltas are an ephemeral projection (ADR ``token-streaming-projection.md``):
``EngineRoom`` owns one ``DeltaHub`` wired as the host-config ``delta_sink``,
and ``stream_frames`` projects published deltas as named ``event: delta`` SSE
frames WITHOUT an ``id:`` line — the resume cursor never moves for a delta, a
reconnect replays envelopes only, and a flooded connection drops deltas (never
envelopes). The final ``MessagesAppended`` envelope stays the only truth.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Optional

from noeta.agent.backend import EngineRoom
from noeta.agent.backend.delta_hub import DeltaHub
from noeta.agent.backend.stream import _DELTA_QUEUE_LIMIT, stream_frames
from noeta.protocols.messages import LLMResponse, StreamDelta, TextBlock, Usage
from noeta.protocols.step_context import StepContext
from noeta.sdk import HostConfig, Options
from noeta.testing.fake_llm import FakeStreamingLLMProvider

_HEARTBEAT = b": keep-alive\n\n"


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _end(text: str) -> LLMResponse:
    return LLMResponse(
        stop_reason="end_turn",
        content=[TextBlock(text=text)],
        usage=Usage(uncached=1, output=1),
    )


def _options() -> Options:
    return Options(
        system_prompt="finish each turn",
        name="main",
        allowed_tools=(),
        permission_mode="bypassPermissions",
    )


class _GatedStreamingProvider:
    """A :class:`FakeStreamingLLMProvider` behind a release gate.

    ``complete_streaming`` blocks until the test sets :attr:`gate`, so the SSE
    connection is provably subscribed BEFORE any delta fires — deltas are
    ephemeral, so a delta fired pre-subscription is silently lost and the
    test would race. Structural ``StreamingProvider`` match is preserved
    (the wrapper carries ``complete_streaming``).
    """

    def __init__(self, inner: FakeStreamingLLMProvider) -> None:
        self.inner = inner
        self.gate = threading.Event()

    def complete(self, request: Any) -> LLMResponse:
        return self.inner.complete(request)

    def complete_streaming(
        self,
        request: Any,
        on_delta: Any,
        request_headers: Optional[dict[str, str]] = None,
    ) -> LLMResponse:
        assert self.gate.wait(timeout=10.0), "test releases the gate"
        return self.inner.complete_streaming(request, on_delta, request_headers)


def _parse_sse(chunk: bytes) -> dict[str, Any]:
    """One yielded frame → its ``event`` / ``id`` / parsed ``data`` fields."""
    out: dict[str, Any] = {"event": None, "id": None, "data": None}
    for line in chunk.decode("utf-8").splitlines():
        if line.startswith("event: "):
            out["event"] = line[len("event: "):]
        elif line.startswith("id: "):
            out["id"] = line[len("id: "):]
        elif line.startswith("data: "):
            out["data"] = json.loads(line[len("data: "):])
    return out


def _drain_until_heartbeat(gen: Any, max_frames: int = 2000) -> list[bytes]:
    """Collect non-heartbeat frames until the first heartbeat (queue empty)."""
    frames: list[bytes] = []
    for _ in range(max_frames):
        chunk = next(gen)
        if chunk == _HEARTBEAT:
            return frames
        frames.append(chunk)
    raise AssertionError("stream never settled to a heartbeat")


def _drain_until_type(
    gen: Any, envelope_type: str, max_frames: int = 2000
) -> list[bytes]:
    """Collect frames (skipping heartbeats) until ``envelope_type`` arrives."""
    frames: list[bytes] = []
    for _ in range(max_frames):
        chunk = next(gen)
        if chunk == _HEARTBEAT:
            continue
        frames.append(chunk)
        parsed = _parse_sse(chunk)
        data = parsed["data"]
        if (
            parsed["event"] is None
            and isinstance(data, dict)
            and data.get("type") == envelope_type
        ):
            return frames
    raise AssertionError(f"stream never delivered a {envelope_type} envelope")


# ---------------------------------------------------------------------------
# DeltaHub unit
# ---------------------------------------------------------------------------


def test_hub_publish_subscribe_unsubscribe() -> None:
    hub = DeltaHub()
    delta = StreamDelta(kind="text", text="hi", index=0)
    hub.publish("t1", "c1", delta)  # no subscribers: a silent no-op

    got_a: list[tuple[str, str, Any]] = []
    got_b: list[tuple[str, str, Any]] = []
    unsub_a = hub.subscribe(lambda *args: got_a.append(args))
    unsub_b = hub.subscribe(lambda *args: got_b.append(args))

    hub.publish("t1", "c1", delta)
    assert got_a == [("t1", "c1", delta)]
    assert got_b == [("t1", "c1", delta)]

    unsub_a()
    hub.publish("t1", "c1", delta)
    assert len(got_a) == 1  # unsubscribed: no further deliveries
    assert len(got_b) == 2

    unsub_a()  # idempotent
    unsub_b()
    hub.publish("t1", "c1", delta)
    assert len(got_b) == 2


def test_hub_swallows_subscriber_exceptions() -> None:
    """A raising subscriber never reaches the publisher (the LLM drive
    thread) and never starves later subscribers on the same pass."""
    hub = DeltaHub()
    got: list[tuple[str, str, Any]] = []

    def _explodes(*args: Any) -> None:
        raise RuntimeError("subscriber bug")

    hub.subscribe(_explodes)
    hub.subscribe(lambda *args: got.append(args))

    delta = StreamDelta(kind="thinking", text="hmm", index=1)
    hub.publish("t1", "c1", delta)  # must not raise
    assert got == [("t1", "c1", delta)]


def test_hub_sink_extracts_task_identity() -> None:
    """``hub.sink`` is the host-config adapter: ``(ctx, call_id, delta)`` in,
    ``publish(ctx.task_id, ...)`` out."""
    hub = DeltaHub()
    got: list[tuple[str, str, Any]] = []
    hub.subscribe(lambda *args: got.append(args))

    ctx = StepContext(task_id="task-9", lease_id="lease-1", trace_id="trace-1")
    delta = StreamDelta(kind="text", text="tok", index=0)
    hub.sink(ctx, "call-7", delta)
    assert got == [("task-9", "call-7", delta)]


# ---------------------------------------------------------------------------
# EngineRoom wiring
# ---------------------------------------------------------------------------


def test_engine_room_wires_hub_into_host_config(tmp_path: Path) -> None:
    """A host_config WITHOUT a sink gets the room's hub injected
    (``dataclasses.replace``): subscribe_deltas observes a live turn's
    deltas, and an unsubscribe stops further deliveries."""
    provider = FakeStreamingLLMProvider(
        responses=[_end("one"), _end("two")],
        deltas=[
            [StreamDelta(kind="text", text="o", index=0)],
            [StreamDelta(kind="text", text="t", index=0)],
        ],
    )
    room = EngineRoom(
        _options(),
        provider=provider,
        workspace_dir=tmp_path,
        host_config=HostConfig(),  # the replace branch (no caller sink)
    )
    try:
        got: list[tuple[str, str, Any]] = []
        unsub = room.subscribe_deltas(lambda *args: got.append(args))
        task_id = room.start(goal="hi")  # synchronous drive
        assert got, "the hub sink should have fanned the turn's deltas out"
        assert all(tid == task_id for tid, _, _ in got)
        assert [d.text for _, _, d in got] == ["o"]
        call_ids = {cid for _, cid, _ in got}
        assert len(call_ids) == 1 and all(call_ids)

        unsub()
        room.send_goal(task_id, goal="again")
        assert len(got) == 1  # unsubscribed before the second turn
    finally:
        room.shutdown()


def test_caller_supplied_delta_sink_is_respected(tmp_path: Path) -> None:
    """A caller that already set ``HostConfig.delta_sink`` keeps it: the room
    must NOT overwrite it with its hub (the hub then simply never fires)."""
    seen: list[tuple[str, str, Any]] = []

    def _my_sink(ctx: StepContext, call_id: str, delta: Any) -> None:
        seen.append((ctx.task_id, call_id, delta))

    provider = FakeStreamingLLMProvider(
        responses=[_end("reply")],
        deltas=[[StreamDelta(kind="text", text="x", index=0)]],
    )
    room = EngineRoom(
        _options(),
        provider=provider,
        workspace_dir=tmp_path,
        host_config=HostConfig(delta_sink=_my_sink),
    )
    try:
        hub_got: list[Any] = []
        room.subscribe_deltas(lambda *args: hub_got.append(args))
        task_id = room.start(goal="hi")
        assert [tid for tid, _, _ in seen] == [task_id]
        assert seen[0][2].text == "x"
        assert hub_got == []  # the caller's sink won; the hub saw nothing
    finally:
        room.shutdown()


# ---------------------------------------------------------------------------
# stream_frames: delta frames on the wire
# ---------------------------------------------------------------------------


def test_stream_delivers_delta_frames_then_final_envelope(tmp_path: Path) -> None:
    """End-to-end: a driven turn's deltas ride the SSE stream as named
    ``event: delta`` frames (no ``id:``), and the durable ``MessagesAppended``
    envelope (which DOES carry an id) arrives after them."""
    scripted = [
        StreamDelta(kind="thinking", text="hmm", index=0),
        StreamDelta(kind="text", text="hel", index=1),
        StreamDelta(kind="text", text="lo", index=1),
    ]
    provider = _GatedStreamingProvider(
        FakeStreamingLLMProvider(
            responses=[_end("streamed reply")], deltas=[scripted]
        )
    )
    room = EngineRoom(
        _options(),
        provider=provider,
        workspace_dir=tmp_path,
        background_drive=True,
    )
    gen = None
    try:
        task_id = room.start(goal="hi")  # seed is durable; drive blocks on the gate
        gen = stream_frames(room, task_id, None, heartbeat_secs=0.2)
        _drain_until_heartbeat(gen)  # catch-up done ⇒ both subscriptions live
        provider.gate.set()
        live = _drain_until_type(gen, "MessagesAppended")

        parsed = [_parse_sse(chunk) for chunk in live]
        delta_frames = [p for p in parsed if p["event"] == "delta"]
        assert len(delta_frames) == len(scripted)
        # Delta frames: correct JSON payload, never an id (cursor untouched).
        for frame, delta in zip(delta_frames, scripted):
            assert frame["id"] is None
            data = frame["data"]
            assert set(data) == {"task_id", "call_id", "kind", "text", "index"}
            assert data["task_id"] == task_id
            assert data["kind"] == delta.kind
            assert data["text"] == delta.text
            assert data["index"] == delta.index
        # One trio ⇒ one stable call_id across every delta of the turn.
        assert len({f["data"]["call_id"] for f in delta_frames}) == 1
        assert all(f["data"]["call_id"] for f in delta_frames)
        # Envelope frames stay unnamed and carry the cursor id.
        envelope_frames = [p for p in parsed if p["event"] is None]
        assert envelope_frames, "the final envelope must ride the same stream"
        assert all(p["id"] for p in envelope_frames)
        # Ordering: the durable MessagesAppended lands after every delta.
        last_delta = max(i for i, p in enumerate(parsed) if p["event"] == "delta")
        first_appended = min(
            i
            for i, p in enumerate(parsed)
            if p["event"] is None and p["data"].get("type") == "MessagesAppended"
        )
        assert last_delta < first_appended
    finally:
        provider.gate.set()
        if gen is not None:
            gen.close()
        room.shutdown()


def test_resume_replays_envelopes_but_not_deltas(tmp_path: Path) -> None:
    """Reconnect with ``Last-Event-ID``: the cursor replays envelopes exactly
    (no dup / no loss) and replays ZERO deltas — they were ephemeral."""
    provider = FakeStreamingLLMProvider(
        responses=[_end("reply")],
        deltas=[
            [
                StreamDelta(kind="text", text="re", index=0),
                StreamDelta(kind="text", text="ply", index=0),
            ]
        ],
    )
    room = EngineRoom(_options(), provider=provider, workspace_dir=tmp_path)
    try:
        task_id = room.start(goal="hi")  # synchronous: deltas fired, nobody listened
        assert provider.streamed_calls == 1  # the streaming path really ran

        gen = stream_frames(room, task_id, None, heartbeat_secs=0.2)
        full = [_parse_sse(c) for c in _drain_until_heartbeat(gen)]
        gen.close()
        assert len(full) >= 3
        assert all(p["event"] is None and p["id"] for p in full)  # no deltas

        # Disconnect + resume from the cursor after the 2nd envelope.
        resume_cursor = full[1]["id"]
        gen = stream_frames(room, task_id, resume_cursor, heartbeat_secs=0.2)
        resumed = [_parse_sse(c) for c in _drain_until_heartbeat(gen)]
        gen.close()

        assert all(p["event"] is None for p in resumed)  # envelopes only
        replayed = {p["data"]["seq"] for p in resumed}
        assert replayed == {p["data"]["seq"] for p in full[2:]}  # no loss
        assert replayed.isdisjoint({p["data"]["seq"] for p in full[:2]})  # no dup
        assert any(p["data"]["type"] == "MessagesAppended" for p in resumed)
    finally:
        room.shutdown()


def test_flooded_connection_drops_deltas_never_envelopes(tmp_path: Path) -> None:
    """Backpressure: with the consumer stalled, delta enqueues stop once the
    pending queue passes the limit — but every envelope still lands."""
    flood = [
        StreamDelta(kind="text", text=f"tok{i}", index=0)
        for i in range(_DELTA_QUEUE_LIMIT + 100)
    ]
    provider = _GatedStreamingProvider(
        FakeStreamingLLMProvider(responses=[_end("done")], deltas=[flood])
    )
    room = EngineRoom(
        _options(),
        provider=provider,
        workspace_dir=tmp_path,
        background_drive=True,
    )
    gen = None
    try:
        task_id = room.start(goal="hi")
        gen = stream_frames(room, task_id, None, heartbeat_secs=0.2)
        _drain_until_heartbeat(gen)  # subscriptions live, queue empty
        # Stall the consumer (stop pulling), release the flood, let the whole
        # turn settle: every enqueue decision happens against a static queue.
        provider.gate.set()
        assert room.join_drives(timeout=10.0)

        frames = [_parse_sse(c) for c in _drain_until_type(gen, "TaskSuspended")]
        delta_count = sum(1 for p in frames if p["event"] == "delta")
        # Enqueued while qsize <= limit, dropped after: limit + 1 exactly.
        assert delta_count == _DELTA_QUEUE_LIMIT + 1
        # Envelopes are never dropped — even the ones committed while the
        # queue was already over the delta limit.
        envelope_types = [
            p["data"]["type"] for p in frames if p["event"] is None
        ]
        assert "MessagesAppended" in envelope_types
        assert "TaskSuspended" in envelope_types
    finally:
        provider.gate.set()
        if gen is not None:
            gen.close()
        room.shutdown()


def test_deltas_filtered_to_the_connection_tree(tmp_path: Path) -> None:
    """A stream on task A never carries task B's deltas (or envelopes):
    the delta callback filters on the same tree set the envelope path uses."""
    provider = FakeStreamingLLMProvider(
        responses=[_end("a"), _end("b")],
        deltas=[
            [],  # task A streams nothing
            [StreamDelta(kind="text", text="b-tok", index=0)],
        ],
    )
    room = EngineRoom(_options(), provider=provider, workspace_dir=tmp_path)
    gen = None
    try:
        task_a = room.start(goal="task a")
        gen = stream_frames(room, task_a, None, heartbeat_secs=0.2)
        _drain_until_heartbeat(gen)  # subscribed; A fully caught up
        # Drive an unrelated task while A's connection is live: B's deltas
        # fire on this thread through the hub and must be filtered out.
        task_b = room.start(goal="task b")
        assert task_b != task_a
        frames = [_parse_sse(c) for c in _drain_until_heartbeat(gen)]
        assert frames == [], "nothing from task B may reach task A's stream"
    finally:
        if gen is not None:
            gen.close()
        room.shutdown()


# ---------------------------------------------------------------------------
# Full-stack smoke: delta frames over a real HTTP socket
# ---------------------------------------------------------------------------


def test_delta_frames_over_real_http(tmp_path: Path) -> None:
    """serve_backend → real socket: named, id-less ``delta`` frames interleave
    with envelopes on the wire and precede the assistant's ``MessagesAppended``
    (the HTTP writer adds nothing and drops nothing on top of stream_frames)."""
    import http.client

    from noeta.agent.backend import BackendConfig, serve_backend

    provider = _GatedStreamingProvider(
        FakeStreamingLLMProvider(
            responses=[_end("hello world")],
            deltas=[
                [
                    StreamDelta(kind="text", text="hello ", index=0),
                    StreamDelta(kind="text", text="world", index=0),
                ]
            ],
        )
    )
    room = EngineRoom(
        _options(),
        provider=provider,
        workspace_dir=tmp_path,
        background_drive=True,
    )
    server, _url, shutdown = serve_backend(
        BackendConfig(host="127.0.0.1", port=0, workspace_dir=tmp_path),
        engine_room=room,
    )
    host, port = server.server_address[:2]
    conn = http.client.HTTPConnection(host, port, timeout=10)
    try:
        # Seeds synchronously; the background drive then blocks on the gate,
        # so the SSE connection below is provably subscribed before any
        # delta fires (deltas are ephemeral — a pre-subscription delta is
        # silently lost and would race the test).
        task_id = room.start(goal="stream over http")
        conn.request("GET", f"/stream?task={task_id}")
        resp = conn.getresponse()
        assert resp.status == 200
        assert resp.getheader("Content-Type", "").startswith("text/event-stream")
        provider.gate.set()

        frames: list[dict[str, Any]] = []
        raw: list[str] = []
        saw_final = False
        for _ in range(5000):
            line = resp.readline().decode("utf-8")
            raw.append(line)
            if line.strip() and line != "\n":
                continue
            frame = _parse_sse("".join(raw).encode("utf-8"))
            raw = []
            if frame["event"] is None and frame["data"] is None:
                continue  # heartbeat comment frame
            frames.append(frame)
            data = frame["data"]
            # A multi-turn conversation task rests at ``suspended`` between
            # turns (one session = one Task) — TaskSuspended is the turn's
            # terminal envelope on this stream, not TaskCompleted.
            if (
                frame["event"] is None
                and isinstance(data, dict)
                and data.get("type") == "TaskSuspended"
            ):
                saw_final = True
                break
        assert saw_final, "stream never delivered TaskSuspended"

        deltas = [f for f in frames if f["event"] == "delta"]
        envelopes = [f for f in frames if f["event"] is None]
        assert [d["data"]["text"] for d in deltas] == ["hello ", "world"]
        assert all(d["id"] is None for d in deltas), "delta frames carry no id"
        assert all(
            d["data"]["task_id"] == task_id and d["data"]["kind"] == "text"
            for d in deltas
        )
        assert all(e["id"] is not None for e in envelopes), "envelopes keep ids"
        # Deltas precede the assistant's MessagesAppended (the durable truth).
        last_appended = max(
            i
            for i, f in enumerate(frames)
            if f["event"] is None and f["data"].get("type") == "MessagesAppended"
        )
        first_delta = min(i for i, f in enumerate(frames) if f["event"] == "delta")
        assert first_delta < last_appended
    finally:
        conn.close()
        shutdown()
