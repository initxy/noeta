"""Full API chain in mock mode: login → session → message → clarifying
question → answer → replay.

Sandbox off by default: pure conversation mode (no file tools), the file
surface APIs return empty. The chain with file output lives in the
sandbox-gated e2e tests.
"""
from __future__ import annotations

import concurrent.futures

from tests.conftest import create_session, login, read_sse, types, wait_status


def test_full_report_flow(client, tmp_path):
    login(client)
    sid = create_session(client)

    goal = "Write a report about the data platform"
    resp = client.post(f"/api/v1/sessions/{sid}/messages", json={"content": goal})
    assert resp.status_code == 202

    # The mock always asks a clarifying question on the first turn → session
    # enters waiting
    wait_status(client, sid, {"waiting"})
    events = read_sse(client, sid, stop_types=("question",))
    seen = types(events)
    assert "turn_started" in seen and "user_message" in seen and "question" in seen
    question = next(e for e in events if e["event"] == "question")
    q = question["data"]
    assert q["question_id"] and q["questions"]
    assert q["questions"][0]["choices"], "the clarifying question should carry choices"
    assert q["questions"][0]["allow_freeform"] is True

    # Sending another message while the turn is running → 409
    resp = client.post(f"/api/v1/sessions/{sid}/messages", json={"content": "more"})
    assert resp.status_code == 409

    # Answer the question → drives on until the turn finishes
    qid = q["questions"][0]["id"]
    resp = client.post(
        f"/api/v1/sessions/{sid}/answer",
        json={"question_id": q["question_id"], "answers": {qid: {"choice_id": "eng"}}},
    )
    assert resp.status_code == 202
    wait_status(client, sid, {"idle"})

    # A pending question produces no turn_finished, so the first turn_finished
    # is the end of this turn
    full = read_sse(client, sid, stop_types=("turn_finished",), timeout=10)
    seen = types(full)
    # Pure conversation mode has no execution-class tool events
    # (tool_call/tool_result): skill activation goes through the engine
    # capability (skill_activated); the file-output chain lives in the
    # sandbox-gated e2e tests
    for expected in (
        "user_message", "question", "question_answered",
        "skill_activated", "assistant_text", "turn_finished",
    ):
        assert expected in seen, f"missing event {expected}: {seen}"
    assert next(e for e in full if e["event"] == "skill_activated")["data"]["skill"] == "demo-skill"
    finish = next(e for e in full if e["event"] == "turn_finished")
    assert finish["data"]["status"] == "awaiting_input"

    # Pure conversation mode: host file tools are not registered, and there
    # are no sandbox tools either (sandbox not enabled)
    tool_names = {e["data"]["tool_name"] for e in full if e["event"] == "tool_call"}
    assert not tool_names & {
        "read", "write", "edit", "shell_run",
        "sandbox_exec", "sandbox_file_read", "sandbox_file_write",
    }, tool_names

    # The file surface is the sandbox: when it is not enabled the list is
    # empty and content 404s — no errors
    assert client.get(f"/api/v1/sessions/{sid}/files").json() == {"files": []}
    assert client.get(
        f"/api/v1/sessions/{sid}/files/content", params={"path": "report.md"}
    ).status_code == 404

    # since_seq replay matches the full stream (as a suffix)
    mid_seq = full[len(full) // 2]["seq"]
    tail = read_sse(client, sid, since_seq=mid_seq, stop_types=("turn_finished",))
    expected_tail = [e for e in full if e.get("seq") is not None and e["seq"] > mid_seq]
    assert [e["seq"] for e in tail] == [e["seq"] for e in expected_tail]
    assert types(tail) == types(expected_tail)


def test_second_turn_live_stream(client):
    """The second turn (a quick reply) rides the live push: open the stream
    first, then send the message."""
    login(client)
    sid = create_session(client)
    client.post(f"/api/v1/sessions/{sid}/messages", json={"content": "Write a report"})
    wait_status(client, sid, {"waiting"})
    q = next(
        e for e in read_sse(client, sid, stop_types=("question",))
        if e["event"] == "question"
    )["data"]
    client.post(
        f"/api/v1/sessions/{sid}/answer",
        json={"question_id": q["question_id"],
              "answers": {q["questions"][0]["id"]: {"text": "your call"}}},
    )
    wait_status(client, sid, {"idle"})
    last_seq = read_sse(client, sid, stop_types=("turn_finished",))[-1]["seq"]

    with concurrent.futures.ThreadPoolExecutor(1) as pool:
        fut = pool.submit(
            read_sse, client, sid, last_seq, ("turn_finished",), 20.0
        )
        import time
        time.sleep(0.3)  # let the stream establish first (subscribe before replay never drops events)
        resp = client.post(
            f"/api/v1/sessions/{sid}/messages", json={"content": "thanks, summarize it"}
        )
        assert resp.status_code == 202
        live = fut.result(timeout=25)

    seen = types(live)
    assert "user_message" in seen and "assistant_text" in seen and "turn_finished" in seen
    assert all(e.get("seq", 10**9) > last_seq for e in live if e.get("seq") is not None)


def test_cancel_then_continue(client):
    """Cancel while the question is pending → turn_finished{cancelled};
    sending again resumes via a fresh task."""
    login(client)
    sid = create_session(client)
    client.post(f"/api/v1/sessions/{sid}/messages", json={"content": "Write a report"})
    wait_status(client, sid, {"waiting"})

    resp = client.post(f"/api/v1/sessions/{sid}/cancel")
    assert resp.status_code == 200
    wait_status(client, sid, {"idle"})
    events = read_sse(client, sid, stop_types=("turn_finished",))
    finish = [e for e in events if e["event"] == "turn_finished"]
    assert finish and finish[-1]["data"]["status"] == "cancelled"

    # Continue the conversation after cancel: NotResumable → a new task in the
    # same workspace (the mock asks the clarifying question again on a new
    # task's first turn)
    resp = client.post(f"/api/v1/sessions/{sid}/messages", json={"content": "Rewrite it"})
    assert resp.status_code == 202
    wait_status(client, sid, {"waiting"})
    events = read_sse(client, sid, stop_types=("question",))
    assert "question" in types(events)


# The tool-failure → synthesized tool_result / turn-continues guarantee moved
# (with the standard noeta tools + per-session containers) to the docker-gated
# e2e: tests/test_sandbox_e2e.py::test_sandbox_e2e_turn. Pure-Python crash
# synthesis is the noeta engine's responsibility and is covered on the noeta
# side.


def test_raw_events(make_client):
    """Trace data source (now under the admin console): the raw envelope list,
    with cursor-based increments.

    raw-events is consolidated under /admin and only admins may read it;
    non-admins always get 404.
    """
    import json as _json

    client = make_client(ADMIN_USERS="alice")
    login(client, "alice")
    sid = create_session(client)

    # Empty while no task is bound
    assert (
        client.get(f"/api/v1/admin/sessions/{sid}/raw-events").json()["events"] == []
    )

    client.post(f"/api/v1/sessions/{sid}/messages", json={"content": "Write a report"})
    wait_status(client, sid, {"waiting"})

    body = client.get(f"/api/v1/admin/sessions/{sid}/raw-events").json()
    events, cursor = body["events"], body["cursor"]
    assert events, "raw events should exist after driving"
    seqs = [e["seq"] for e in events]
    assert seqs == sorted(seqs)
    event_types = {e["type"] for e in events}
    # The raw stream contains envelopes outside the translator vocabulary
    # (TaskCreated never appears in SSE)
    assert "TaskCreated" in event_types and "UserQuestionRequested" in event_types
    for e in events:
        for field in ("id", "task_id", "seq", "type", "occurred_at", "actor",
                      "origin", "payload"):
            assert field in e, f"envelope is missing field {field}"

    # The cursor is {task_id: last_seq}; passing it back yields an increment
    # without the already-delivered events. Do not assert it is exactly empty:
    # after suspend the engine may still write asynchronously (e.g.
    # TaskSnapshot).
    root = events[0]["task_id"]
    assert cursor[root] == seqs[-1]
    again = client.get(
        f"/api/v1/admin/sessions/{sid}/raw-events",
        params={"cursor": _json.dumps(cursor)},
    ).json()
    assert all(e["seq"] > cursor.get(e["task_id"], -1) for e in again["events"])
    assert again["cursor"][root] >= cursor[root]

    # A partial cursor's increment starts with a strict suffix of the full
    # stream (newly written events are allowed after it)
    mid = seqs[len(seqs) // 2]
    tail = client.get(
        f"/api/v1/admin/sessions/{sid}/raw-events",
        params={"cursor": _json.dumps({root: mid})},
    ).json()["events"]
    tail_seqs = [e["seq"] for e in tail if e["task_id"] == root]
    expected = [s for s in seqs if s > mid]
    assert tail_seqs[: len(expected)] == expected

    # Invalid cursor → 422
    assert (
        client.get(
            f"/api/v1/admin/sessions/{sid}/raw-events", params={"cursor": "not-json"}
        ).status_code
        == 422
    )

    # Invisible to non-admins (switch to non-allowlisted user bob) → 404
    resp = client.post("/api/v1/auth/dev-login", json={"username": "bob"})
    assert resp.status_code == 200
    assert (
        client.get(f"/api/v1/admin/sessions/{sid}/raw-events").status_code == 404
    )


def test_raw_events_subtask_subtree(tmp_path, monkeypatch):
    """raw_events returns root + each subtask stream (the data source for the
    Trace page's subagent drill-down).

    Defect regression: it once read only the root stream, so every subtask
    event was missing → clicking a subagent on the Trace page showed nothing.
    A stub client verifies the fan-out and the {task_id: last_seq} cursor
    semantics (each stream counts seq independently; on increments the subtask
    ids = those known to the cursor ∪ markers newly appearing in the root
    increment).
    """
    import asyncio

    from noeta.protocols.events import (
        EventEnvelope,
        SubtaskCompletedPayload,
        SubtaskSpawnedPayload,
        TaskCreatedPayload,
    )
    from noeta.protocols.wake import SubtaskResult

    from noeta.agent.config import Settings
    from noeta.agent.host.service import AgentService
    from noeta.agent.store.sessions import Session, SessionStore

    def ev(task_id: str, seq: int, etype: str, payload) -> EventEnvelope:
        return EventEnvelope.build(task_id=task_id, type=etype, payload=payload).with_seq(seq)

    streams: dict[str, list[EventEnvelope]] = {
        "task-root": [
            ev("task-root", 0, "TaskCreated",
               TaskCreatedPayload(goal="main task", policy_name="p")),
            ev("task-root", 1, "SubtaskSpawned",
               SubtaskSpawnedPayload(subtask_id="task-sub1", agent_name="explorer", goal="search")),
            ev("task-root", 2, "SubtaskCompleted",
               SubtaskCompletedPayload(subtask_id="task-sub1",
                                       result=SubtaskResult(status="completed"))),
        ],
        "task-sub1": [
            ev("task-sub1", 0, "TaskCreated",
               TaskCreatedPayload(goal="search", policy_name="p", parent_task_id="task-root")),
            ev("task-sub1", 1, "TaskCompleted", {"answer": "done"}),
        ],
    }

    class StubClient:
        def events_after(self, task_id, after_seq=None):
            floor = -1 if after_seq is None else after_seq
            return [e for e in streams.get(task_id, []) if e.seq > floor]

        def get_content(self, content_hash):  # pragma: no cover - must not be called
            raise AssertionError("raw_events must not deref")

    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    settings = Settings(llm_provider="mock", dev_login_enabled=True,
                        session_secret="s")
    settings.data_path.mkdir(parents=True, exist_ok=True)
    store = SessionStore(settings.app_db_path)
    service = AgentService(settings, store)
    service._client = StubClient()
    session = Session(id="s-1", user="u", title="t", model="m",
                      task_id="task-root", status="idle",
                      created_at=0.0, updated_at=0.0)

    try:
        # Full read: the root stream first, then the subtask streams
        # discovered via markers
        full = asyncio.run(service.raw_events(session, None))
        got = [(e["task_id"], e["seq"]) for e in full["events"]]
        assert got == [("task-root", 0), ("task-root", 1), ("task-root", 2),
                       ("task-sub1", 0), ("task-sub1", 1)]
        assert full["cursor"] == {"task-root": 2, "task-sub1": 1}

        # Passing the cursor back → empty increment
        empty = asyncio.run(service.raw_events(session, full["cursor"]))
        assert empty["events"] == [] and empty["cursor"] == full["cursor"]

        # Append one event to each stream (the subtask id exists only in the
        # cursor; the root increment has no marker)
        streams["task-root"].append(ev("task-root", 3, "TaskWoken", {"wake_event": None}))
        streams["task-sub1"].append(ev("task-sub1", 2, "TaskSnapshot", {"state_ref": None}))
        delta = asyncio.run(service.raw_events(session, full["cursor"]))
        got = [(e["task_id"], e["seq"]) for e in delta["events"]]
        assert got == [("task-root", 3), ("task-sub1", 2)]
        assert delta["cursor"] == {"task-root": 3, "task-sub1": 2}
    finally:
        store.close()


def _collect_ref_hashes(obj) -> list[str]:
    """Recursively collect the hashes of ContentRefs
    (__canonical_tag__=content_ref) inside a payload."""
    found: list[str] = []
    if isinstance(obj, dict):
        if obj.get("__canonical_tag__") == "content_ref" and "hash" in obj:
            found.append(obj["hash"])
        for v in obj.values():
            found.extend(_collect_ref_hashes(v))
    elif isinstance(obj, list):
        for v in obj:
            found.extend(_collect_ref_hashes(v))
    return found


def test_content_endpoint(make_client):
    """Trace deref data source (consolidated under admin): fetch raw bytes by
    ContentRef.hash."""
    client = make_client(ADMIN_USERS="alice")
    login(client, "alice")
    sid = create_session(client)
    client.post(f"/api/v1/sessions/{sid}/messages", json={"content": "Write a report"})
    wait_status(client, sid, {"waiting"})

    events = client.get(f"/api/v1/admin/sessions/{sid}/raw-events").json()["events"]
    hashes = _collect_ref_hashes([e["payload"] for e in events])
    assert hashes, "payloads should contain ContentRefs after driving"
    llm_started = [e for e in events if e["type"] == "LLMRequestStarted"]
    assert llm_started, "the mock provider should also emit LLM events (Trace turn grouping depends on them)"

    resp = client.get(f"/api/v1/content/{hashes[0]}")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/octet-stream")
    assert len(resp.content) > 0

    # Missing / invalid hash → 404
    assert client.get("/api/v1/content/" + "0" * 64).status_code == 404
    assert client.get("/api/v1/content/not-a-hash").status_code == 404

    # Non-admin fetching content → 404 (the content endpoint is admin-gated)
    client.post("/api/v1/auth/dev-login", json={"username": "bob"})
    assert client.get(f"/api/v1/content/{hashes[0]}").status_code == 404


def test_content_read_not_blocked_by_worker(tmp_path, monkeypatch):
    """Regression: content reads do not go through the drive serial queue.

    An active turn can hold the worker thread for a long time; if
    get_content_by_hash queued via _submit, Trace-page derefs would hang until
    the turn ends. Fill the queue with a sleep job and assert the content read
    still returns immediately.
    """
    import asyncio
    import time as _time

    for key, value in {
        "DATA_DIR": str(tmp_path / "data"),
        "LLM_PROVIDER": "mock",
        "DEV_LOGIN_ENABLED": "true",
        "SESSION_SECRET": "test-secret",
    }.items():
        monkeypatch.setenv(key, value)

    from noeta.agent.config import Settings
    from noeta.agent.host.service import AgentService
    from noeta.agent.store.sessions import SessionStore

    settings = Settings()
    settings.data_path.mkdir(parents=True, exist_ok=True)
    store = SessionStore(settings.app_db_path)
    service = AgentService(settings, store)

    async def scenario() -> None:
        await service.startup()
        try:
            service._submit_nowait(lambda: _time.sleep(3))  # simulate an active turn filling the worker
            body = await asyncio.wait_for(
                service.get_content_by_hash("0" * 64), timeout=1.5
            )
            assert body is None  # nonexistent hash; the point is not waiting for an idle worker
        finally:
            await service.shutdown()

    asyncio.run(scenario())
    store.close()


def test_replay_not_blocked_by_worker(tmp_path, monkeypatch):
    """Regression: replay reads do not go through the drive serial queue.

    An active turn may hold the worker for minutes (LLM 429 retries, 120s
    sandbox command timeouts); if replay / raw_events queued via _submit, the
    SSE replay of **every** session (including finished ones) would never see
    replay_done and the whole frontend would hang on "loading". Fill the queue
    with a sleep job and assert both still return immediately (instead of
    waiting for an idle worker).
    """
    import asyncio
    import time as _time

    for key, value in {
        "DATA_DIR": str(tmp_path / "data"),
        "LLM_PROVIDER": "mock",
        "DEV_LOGIN_ENABLED": "true",
        "SESSION_SECRET": "test-secret",
    }.items():
        monkeypatch.setenv(key, value)

    from noeta.agent.config import Settings
    from noeta.agent.host.service import AgentService
    from noeta.agent.store.sessions import Session, SessionStore

    settings = Settings()
    settings.data_path.mkdir(parents=True, exist_ok=True)
    store = SessionStore(settings.app_db_path)
    service = AgentService(settings, store)

    # A non-flow session with a task_id: replay goes through _replay_single
    # (a pure event_log read). The task does not exist → empty replay; what
    # this test wants is "not queued behind the sleep job".
    session = Session(
        id="s-x", user="alice", title="t", model="m",
        task_id="task-does-not-exist", status="idle",
        created_at=0.0, updated_at=0.0,
    )

    async def scenario() -> None:
        await service.startup()
        try:
            service._submit_nowait(lambda: _time.sleep(3))  # simulate an active turn filling the worker
            events = await asyncio.wait_for(service.replay(session, None), timeout=1.5)
            assert events == []  # the point is not waiting for an idle worker
            raw = await asyncio.wait_for(service.raw_events(session, None), timeout=1.5)
            assert raw == {"events": [], "cursor": {}}
        finally:
            await service.shutdown()

    asyncio.run(scenario())
    store.close()


def test_worker_survives_cancelled_future(tmp_path, monkeypatch):
    """Regression: a client disconnecting midway must not kill the worker
    thread.

    Chain: the awaitable from _submit gets cancelled → the underlying
    concurrent Future enters CANCELLED (the job never went through
    set_running, so it is always cancellable) → when the worker finishes the
    job, set_result / set_exception raises InvalidStateError. The old
    implementation had no guard → the worker thread died → nobody consumed
    any later job (send_goal and friends all hung).
    """
    import asyncio
    import threading

    for key, value in {
        "DATA_DIR": str(tmp_path / "data"),
        "LLM_PROVIDER": "mock",
        "DEV_LOGIN_ENABLED": "true",
        "SESSION_SECRET": "test-secret",
    }.items():
        monkeypatch.setenv(key, value)

    from noeta.agent.config import Settings
    from noeta.agent.host.service import AgentService
    from noeta.agent.store.sessions import SessionStore

    settings = Settings()
    settings.data_path.mkdir(parents=True, exist_ok=True)
    store = SessionStore(settings.app_db_path)
    service = AgentService(settings, store)

    async def scenario() -> None:
        await service.startup()
        try:
            # Pin the timing with an Event: the job blocks on the worker →
            # cancel the waiter → release the job → the worker delivers a
            # result to the already-CANCELLED future
            release = threading.Event()
            fut = service._submit(release.wait)
            await asyncio.sleep(0.1)  # the worker has picked up the job and is blocked
            fut.cancel()
            await asyncio.sleep(0.1)  # let the loop forward the cancel to the underlying future
            release.set()
            await asyncio.sleep(0.3)  # the worker delivers (the old implementation died here)

            assert service._thread is not None and service._thread.is_alive(), (
                "worker thread killed by a cancelled future"
            )
            # Later jobs still complete normally (the old implementation never
            # returned here → wait_for timed out)
            result = await asyncio.wait_for(service._submit(lambda: 42), timeout=2.0)
            assert result == 42
        finally:
            try:
                # With a dead worker the shutdown job is also never consumed
                # in the old implementation; do not let the test hang
                await asyncio.wait_for(service.shutdown(), timeout=10.0)
            except asyncio.TimeoutError:
                pass

    asyncio.run(scenario())
    store.close()


def test_models_and_health(client):
    login(client)
    data = client.get("/api/v1/models").json()
    assert data["provider"] == "mock" and data["models"]
    assert any(m["default"] for m in data["models"])
    # models.json-driven: label / efforts / default_effort present, GPT 5.5 is
    # the default
    default = next(m for m in data["models"] if m["default"])
    assert default["id"] == "gpt-5.5-2026-04-24"
    assert default["label"] == "GPT 5.5"
    assert default["efforts"] == ["low", "medium", "high", "xhigh", "max"]
    assert default["default_effort"] == "high"
    assert any(m["id"] == "gpt-5.4-2026-03-05" for m in data["models"])
    assert client.get("/api/v1/health").json()["ok"] is True


def test_effort_flows_to_provider(make_client, monkeypatch):
    """The effort parameter flows from POST messages into the noeta
    LLMRequest.effort."""
    from noeta.protocols.messages import LLMResponse, TextBlock, Usage
    from noeta.testing.fake_llm import FakeLLMProvider

    seen_efforts: list[str | None] = []

    def responder(request):
        seen_efforts.append(request.effort)
        return LLMResponse(
            stop_reason="end_turn",
            content=[TextBlock(text="(mock) Received.")],
            usage=Usage(uncached=1, output=1),
        )

    monkeypatch.setattr(
        "noeta.agent.host.service.build_provider",
        lambda settings: (FakeLLMProvider(responder=responder), "mock"),
    )
    client = make_client()
    login(client)
    sid = create_session(client)

    # effort=high → 202, and it flows through to the provider
    resp = client.post(
        f"/api/v1/sessions/{sid}/messages",
        json={"content": "Hello", "effort": "high"},
    )
    assert resp.status_code == 202
    wait_status(client, sid, {"idle"})
    assert seen_efforts, "provider was never called"
    assert seen_efforts[0] == "high"

    # Unsupported effort → 422 (never reaches the provider)
    resp = client.post(
        f"/api/v1/sessions/{sid}/messages",
        json={"content": "again", "effort": "ultra"},
    )
    assert resp.status_code == 422
    assert "ultra" in resp.json()["detail"]


def test_models_config_fallback(make_client, tmp_path):
    """When models.json is missing, degrade to the single model GPT 5.5; the
    backend must not crash."""
    client = make_client(MODELS_CONFIG=str(tmp_path / "missing.json"))
    login(client)
    data = client.get("/api/v1/models").json()
    assert [m["id"] for m in data["models"]] == ["gpt-5.5-2026-04-24"]
    assert data["models"][0]["default"] is True
    # The degraded model can still create sessions
    sid = create_session(client)
    assert sid


def test_background_subagent_flow(client):
    """Full delegation chain: spawn explorer (background) → subtask tool
    events routed to the parent session (synthetic frames + subtask_id) → the
    result notice wakes the parent turn → subtask_finished."""
    import time

    login(client)
    sid = create_session(client)

    # Subtask tool events are synthetic frames (not replayed); the stream must
    # be open before the message is sent
    with concurrent.futures.ThreadPoolExecutor(1) as pool:
        fut = pool.submit(
            read_sse, client, sid, None, ("subtask_finished",), 30.0
        )
        time.sleep(0.3)  # let the stream establish first (subscribe before replay never drops events)
        resp = client.post(
            f"/api/v1/sessions/{sid}/messages",
            json={"content": "parallel search the report-related code of this platform"},
        )
        assert resp.status_code == 202
        events = fut.result(timeout=35)

    seen = types(events)
    assert "subtask_started" in seen and "subtask_finished" in seen

    started = next(e for e in events if e["event"] == "subtask_started")
    assert started["data"]["agent_name"] == "explorer"
    sub_id = started["data"]["subtask_id"]

    # The subtask's tool activity: carries subtask_id, synthetic frames (seq
    # None, excluded from replay dedup)
    sub_calls = [
        e for e in events
        if e["event"] == "tool_call" and e["data"].get("subtask_id")
    ]
    assert sub_calls and all(e["seq"] is None for e in sub_calls)
    assert all(e["data"]["subtask_id"] == sub_id for e in sub_calls)

    fin = next(e for e in events if e["event"] == "subtask_finished")
    assert fin["data"]["subtask_id"] == sub_id
    assert fin["data"]["status"] == "completed"

    # The result notice (origin=system) must not masquerade as a user message
    user_msgs = [e for e in events if e["event"] == "user_message"]
    assert all(
        "<background-subagent" not in e["data"]["content"] for e in user_msgs
    )

    # Session status tracks only the root task: back to idle when it all ends
    wait_status(client, sid, {"idle"})

    # Replay rebuilds the subtask summary card: the parent stream's
    # started/finished are replayable events with seq
    replayed = read_sse(client, sid, stop_types=("replay_done",))
    rseen = types(replayed)
    assert "subtask_started" in rseen and "subtask_finished" in rseen
    # Subtask steps are also rebuilt on full replay (synthetic frames appended
    # after the root events); a reconnect (since_seq > 0) does not resend
    # them, preventing duplicated steps in the frontend
    replayed_sub = [
        e for e in replayed
        if e["event"] == "tool_call" and e["data"].get("subtask_id")
    ]
    assert replayed_sub and all(e["seq"] is None for e in replayed_sub)
    # since_seq=0 is also a full replay (the frontend's real first-connect
    # path) and likewise delivers the subtask synthetic frames
    replayed_zero = read_sse(client, sid, since_seq=0, stop_types=("replay_done",))
    assert any(
        e["event"] == "tool_call" and e["data"].get("subtask_id")
        for e in replayed_zero
    )
    last_seq = max(e["seq"] for e in replayed if e["seq"] is not None)
    reconnected = read_sse(
        client, sid, since_seq=last_seq, stop_types=("replay_done",)
    )
    assert not any(
        e["event"] == "tool_call" and e["data"].get("subtask_id")
        for e in reconnected
    )


def test_cancel_cascades_to_background_subagent(client):
    """cancel on the root task → the noeta cancellation registry cascades to
    the background subtask (which abandons driving at a step boundary and
    writes its own TaskCancelled, no Delivered) → SSE receives the
    subtask_finished{cancelled} closing card."""
    import time

    login(client)
    sid = create_session(client)

    with concurrent.futures.ThreadPoolExecutor(1) as pool:
        fut = pool.submit(
            read_sse, client, sid, None, ("subtask_finished",), 30.0
        )
        time.sleep(0.3)
        resp = client.post(
            f"/api/v1/sessions/{sid}/messages",
            json={"content": "parallel search slow demo"},
        )
        assert resp.status_code == 202
        # Wait for the spawn turn to end (parent idle); the child sleeps 1s
        # per step (the mock slow path), so it is still running now and the
        # cancel cascade lands at its next step boundary
        wait_status(client, sid, {"idle"})
        assert client.post(f"/api/v1/sessions/{sid}/cancel").status_code == 200
        events = fut.result(timeout=35)

    seen = types(events)
    assert "subtask_started" in seen
    # The root's TaskCancelled arrives first (turn_finished{cancelled}), the
    # subtask cascade follows
    finish = [e for e in events if e["event"] == "turn_finished"]
    assert finish and finish[-1]["data"]["status"] == "cancelled"
    fin = [e for e in events if e["event"] == "subtask_finished"]
    assert fin and fin[-1]["data"]["status"] == "cancelled"


def test_capabilities_endpoint(client):
    # Unauthenticated → 401
    assert client.get("/api/v1/capabilities").status_code == 401
    login(client)
    caps = client.get("/api/v1/capabilities").json()["capabilities"]
    assert caps["todo_write"] is True and caps["delegation"] is True
    assert caps["ask_user_question"] is True and caps["skill_invocation"] is True
    # memory is on (isolated per space, noeta 0.2.4 resolver seam, see the
    # service.py comment)
    assert caps["memory"] is True and caps["mcp"] is False


def test_delete_session(client, tmp_path):
    login(client)
    sid = create_session(client)
    client.post(f"/api/v1/sessions/{sid}/messages", json={"content": "Write a report"})
    wait_status(client, sid, {"waiting"})
    assert client.delete(f"/api/v1/sessions/{sid}").status_code == 200
    assert client.get(f"/api/v1/sessions/{sid}").status_code == 404
    assert not (tmp_path / "data" / "workspaces" / sid).exists()
