"""``send_goal`` / ``inject_goal`` with an ``idempotency_key``: a repeat with
the same key on the same task appends nothing and returns the outcome — from
the same Client, from another Client over the same store, and while the first
call is still in flight. Unkeyed calls are unchanged."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from noeta.core.fold import fold
from noeta.protocols.canonical import to_canonical_bytes
from noeta.protocols.messages import ToolUseBlock
from noeta.sdk import Client, HostConfig, LLMResponse, Options, TextBlock, Usage
from noeta.testing.fake_llm import FakeLLMProvider


def _text(n: int) -> LLMResponse:
    return LLMResponse(
        stop_reason="end_turn",
        content=[TextBlock(text=f"done{n}")],
        usage=Usage(uncached=1, output=1),
        raw={"id": str(n)},
    )


def _goal_appends(client: Client, task_id: str, goal: str) -> int:
    host = client._host  # noqa: SLF001 - the folded transcript is the point
    count = 0
    for message in fold(host.event_log, host.content_store, task_id).runtime.messages:
        if message.role != "user":
            continue
        if any(getattr(b, "text", None) == goal for b in message.content):
            count += 1
    return count


def _client(tmp_path: Path, respond: Any, *, queue: str = "default") -> Client:
    return Client(
        Options(system_prompt="x", permission_mode="bypassPermissions"),
        provider=FakeLLMProvider(responder=respond),
        workspace_dir=tmp_path,
        model="sonnet",
        host_config=HostConfig(
            storage_path=str(tmp_path / "store.sqlite"), queue=queue
        ),
    )


def _counting() -> tuple[list[int], Any]:
    calls: list[int] = []
    lock = threading.Lock()

    def respond(_req: Any) -> LLMResponse:
        with lock:
            calls.append(1)
            n = len(calls)
        return _text(n)

    return calls, respond


def test_repeat_send_goal_appends_once(tmp_path: Path) -> None:
    calls, respond = _counting()
    client = _client(tmp_path, respond)
    try:
        tid = client.start(goal="hi").task_id
        first = client.send_goal(tid, goal="once", idempotency_key="k1")
        seen = len(calls)
        again = client.send_goal(tid, goal="once", idempotency_key="k1")
        assert len(calls) == seen
        assert _goal_appends(client, tid, "once") == 1
        assert again.status == first.status == "suspended"
        # A different key is a different request.
        client.send_goal(tid, goal="once", idempotency_key="k2")
        assert _goal_appends(client, tid, "once") == 2
    finally:
        client.shutdown()


def test_repeat_from_another_client_appends_nothing(tmp_path: Path) -> None:
    calls, respond = _counting()
    a = _client(tmp_path, respond, queue="a")
    b = _client(tmp_path, respond, queue="b")
    try:
        tid = a.start(goal="hi").task_id
        a.send_goal(tid, goal="once", idempotency_key="k")
        seen = len(calls)
        out = b.send_goal(tid, goal="once", idempotency_key="k")
        assert out.status == "suspended"
        assert len(calls) == seen
        assert _goal_appends(b, tid, "once") == 1
    finally:
        a.shutdown()
        b.shutdown()


def test_repeat_while_the_first_is_in_flight(tmp_path: Path) -> None:
    entered = threading.Event()
    release = threading.Event()
    calls: list[int] = []

    def respond(_req: Any) -> LLMResponse:
        calls.append(1)
        if len(calls) == 2:
            entered.set()
            release.wait(20)
        return _text(len(calls))

    a = _client(tmp_path, respond, queue="a")
    b = _client(tmp_path, respond, queue="b")
    try:
        tid = a.start(goal="hi").task_id
        t = threading.Thread(
            target=lambda: a.send_goal(tid, goal="once", idempotency_key="k")
        )
        t.start()
        assert entered.wait(10)
        out = b.send_goal(tid, goal="once", idempotency_key="k")
        assert out.status == "running"
        release.set()
        t.join(20)
        assert _goal_appends(a, tid, "once") == 1
        assert len(calls) == 2
    finally:
        release.set()
        a.shutdown()
        b.shutdown()


def test_concurrent_duplicates_seed_one_turn(tmp_path: Path) -> None:
    calls, respond = _counting()
    clients = [_client(tmp_path, respond, queue=f"q{i}") for i in range(4)]
    try:
        tid = clients[0].start(goal="hi").task_id
        barrier = threading.Barrier(len(clients))
        errors: list[BaseException] = []

        def go(c: Client) -> None:
            barrier.wait()
            try:
                c.send_goal(tid, goal="once", idempotency_key="k")
            except BaseException as exc:  # noqa: BLE001 - surfaced below
                errors.append(exc)

        threads = [threading.Thread(target=go, args=(c,)) for c in clients]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        assert errors == []
        assert _goal_appends(clients[0], tid, "once") == 1
        claims = [e for e in clients[0].events(tid) if e.type == "IdempotencyKeyClaimed"]
        assert len(claims) == 1
    finally:
        for c in clients:
            c.shutdown()


def test_repeat_inject_goal_mid_turn_delivers_once(tmp_path: Path) -> None:
    entered = threading.Event()
    release = threading.Event()
    calls: list[int] = []

    def respond(_req: Any) -> LLMResponse:
        calls.append(1)
        n = len(calls)
        if n == 2:
            entered.set()
            release.wait(20)
            return LLMResponse(
                stop_reason="tool_use",
                content=[
                    ToolUseBlock(
                        call_id="c1", tool_name="Bash", arguments={"command": "true"}
                    )
                ],
                usage=Usage(uncached=1, output=1),
                raw={"id": str(n)},
            )
        return _text(n)

    client = _client(tmp_path, respond)
    try:
        tid = client.start(goal="hi").task_id
        t = threading.Thread(target=lambda: client.send_goal(tid, goal="work"))
        t.start()
        assert entered.wait(10)
        client.inject_goal(tid, goal="also this", idempotency_key="inj")
        client.inject_goal(tid, goal="also this", idempotency_key="inj")
        release.set()
        t.join(20)
        assert _goal_appends(client, tid, "also this") == 1
        requests = [e for e in client.events(tid) if e.type == "InjectionRequested"]
        assert len(requests) == 1
        # The same key on a parked task is still a repeat: no new turn.
        seen = len(calls)
        client.inject_goal(tid, goal="also this", idempotency_key="inj")
        assert len(calls) == seen
    finally:
        release.set()
        client.shutdown()


def test_unkeyed_calls_are_unchanged(tmp_path: Path) -> None:
    calls, respond = _counting()
    client = _client(tmp_path, respond)
    try:
        tid = client.start(goal="hi").task_id
        client.send_goal(tid, goal="same")
        client.send_goal(tid, goal="same")
        assert _goal_appends(client, tid, "same") == 2
        events = client.events(tid)
        assert not any(e.type == "IdempotencyKeyClaimed" for e in events)
        for e in events:
            if e.type == "MessagesAppended":
                assert b"idempotency_key" not in to_canonical_bytes(e.payload)
    finally:
        client.shutdown()
