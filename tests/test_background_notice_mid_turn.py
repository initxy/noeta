"""A background notice reaches a running turn between tool calls.

A background command that ends — or a monitor that emits — while the model is
mid-turn is queued as a pending injection the moment it happens, and the
running turn's top-of-loop drain appends it after the tool results, so the
model sees it on its very next request, as Claude Code delivers a queued
``<task-notification>`` between tool calls. The notice is recorded where it is
taken in, so a fresh fold re-derives the same transcript; it never splits a
``tool_use`` from its ``tool_result``; and a session that parks before taking
it in is driven a turn for it instead.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from tests._sdk_session import official_registry as official_agent_registry
from noeta.client import SdkHost
from noeta.core.fold import fold
from noeta.core.wiring import wire_default_observers
from noeta.execution.background_delivery import BackgroundDelivery
from noeta.execution.driver import InteractionDriver, multi_turn_policy_wrapper
from noeta.protocols.messages import (
    LLMRequest,
    LLMResponse,
    Message,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
)
from noeta.runtime.shell_policy import ShellMode
from noeta.runtime.workspace import FsWriteMode
from noeta.storage.memory import (
    InMemoryContentStore,
    InMemoryDispatcher,
    InMemoryEventLog,
)
from noeta.testing.fake_llm import FakeLLMProvider


def _tool(call_id: str, name: str, arguments: dict[str, Any]) -> LLMResponse:
    return LLMResponse(
        stop_reason="tool_use",
        content=[ToolUseBlock(call_id=call_id, tool_name=name, arguments=arguments)],
        usage=Usage(uncached=1, output=1),
        raw={"id": call_id},
    )


def _end_turn(text: str) -> LLMResponse:
    return LLMResponse(
        stop_reason="end_turn",
        content=[TextBlock(text=text)],
        usage=Usage(uncached=1, output=1),
        raw={"id": "end-" + text},
    )


def _host(
    workspace: Path, provider: FakeLLMProvider
) -> tuple[SdkHost, InMemoryEventLog, InMemoryContentStore]:
    dispatcher = InMemoryDispatcher()
    event_log = InMemoryEventLog(lease_validator=dispatcher)
    content_store = InMemoryContentStore()
    host = SdkHost(
        event_log=event_log,
        content_store=content_store,
        dispatcher=dispatcher,
        provider=provider,
        model="gpt-test",
        workspace_dir=workspace,
        write_mode=FsWriteMode.APPLY,
        shell_mode=ShellMode.ARBITRARY,
        permission_mode="bypassPermissions",
        policy_wrapper=multi_turn_policy_wrapper,
        registry=official_agent_registry(),
        aliases={"default": "main"},
    )
    return host, event_log, content_store


def _text(message: Message) -> str:
    return "".join(b.text for b in message.content if isinstance(b, TextBlock))


def _notices(messages: list[Message]) -> list[int]:
    return [
        i
        for i, m in enumerate(messages)
        if m.origin == "system" and _text(m).startswith("<task-notification>")
    ]


def _run_mid_turn(tmp_path: Path) -> tuple[
    FakeLLMProvider, InMemoryEventLog, InMemoryContentStore, str
]:
    """One turn: start a background command, wait in the foreground while it
    ends, then answer. Returns the provider (its requests) and the session."""
    ws = tmp_path / "ws"
    ws.mkdir()
    provider = FakeLLMProvider(
        responses=[
            _tool(
                "c1",
                "Bash",
                {
                    "command": "sleep 0.3; echo hello-from-bg",
                    "description": "Say hello later",
                    "run_in_background": True,
                },
            ),
            _tool("c2", "Bash", {"command": "sleep 1.5"}),
            _end_turn("saw it"),
        ]
    )
    host, event_log, content_store = _host(ws, provider)
    driver = InteractionDriver(host)
    host.set_background_notifier(driver)
    outcome = driver.start(goal="start it and wait", agent="main")
    assert outcome.status == "suspended"
    return provider, event_log, content_store, outcome.task_id


def test_a_notice_reaches_the_next_request_of_the_running_turn(
    tmp_path: Path,
) -> None:
    provider, event_log, content_store, task_id = _run_mid_turn(tmp_path)

    # One turn, three requests: no extra turn was driven for the notice.
    assert len(provider.received_requests) == 3
    third: LLMRequest = provider.received_requests[2]
    at = _notices(list(third.messages))
    assert len(at) == 1, "the notice rides the request after the foreground wait"
    notice = third.messages[at[0]]
    text = _text(notice)
    assert "<status>completed</status>" in text
    assert (
        '<summary>Background command "Say hello later" completed (exit code 0)'
        "</summary>"
    ) in text
    assert "<tool-use-id>c1</tool-use-id>" in text
    assert text.endswith("Last output:\nhello-from-bg")
    # The second request had none: the command was still running.
    assert _notices(list(provider.received_requests[1].messages)) == []
    # Nothing is left pending once the turn has taken it in.
    task = fold(event_log, content_store, task_id)
    assert task.governance.pending_injections == {}


def test_the_notice_follows_the_tool_results_never_splitting_a_pair(
    tmp_path: Path,
) -> None:
    provider, _event_log, _content_store, _task_id = _run_mid_turn(tmp_path)
    messages = list(provider.received_requests[2].messages)
    at = _notices(messages)[0]
    before = messages[at - 1]
    # Right after the tool result of the call that was running …
    assert before.role == "tool"
    assert [b.call_id for b in before.content if isinstance(b, ToolResultBlock)] == [
        "c2"
    ]
    # … whose tool_use is the assistant turn just before it.
    uses = [b for b in messages[at - 2].content if isinstance(b, ToolUseBlock)]
    assert [u.call_id for u in uses] == ["c2"]
    # Every tool_use in the request is answered by the very next message.
    for i, m in enumerate(messages):
        ids = [b.call_id for b in m.content if isinstance(b, ToolUseBlock)]
        if ids:
            nxt = messages[i + 1]
            assert [
                b.call_id for b in nxt.content if isinstance(b, ToolResultBlock)
            ] == ids


def test_the_notice_merges_into_the_tool_result_turn_on_the_wire(
    tmp_path: Path,
) -> None:
    """Anthropic: the notice renders as a ``<system-reminder>`` text block in
    the same user turn as the tool results, after them."""
    from noeta.builtins.providers.impl.anthropic import AnthropicProvider

    provider, _event_log, _content_store, _task_id = _run_mid_turn(tmp_path)
    request = provider.received_requests[2]
    body = AnthropicProvider(  # noqa: SLF001
        api_key="k", default_max_tokens=1024
    )._build_request_body(
        LLMRequest(
            model="claude-sonnet-4-5",
            system=request.system,
            messages=request.messages,
            tools=request.tools,
        )
    )
    turns = body["messages"]
    last = turns[-1]
    assert last["role"] == "user"
    kinds = [block["type"] for block in last["content"]]
    assert kinds[0] == "tool_result"
    notice = next(
        block for block in last["content"]
        if block["type"] == "text" and "<task-notification>" in block["text"]
    )
    assert notice["text"].startswith("<system-reminder>")
    assert last["content"].index(notice) > kinds.index("tool_result")


def test_a_fresh_fold_re_derives_the_transcript_the_model_saw(
    tmp_path: Path,
) -> None:
    """The notice is recorded where it was taken in, so folding the log from
    scratch gives exactly the history of the request that carried it."""
    provider, event_log, content_store, task_id = _run_mid_turn(tmp_path)
    sent = list(provider.received_requests[2].messages)
    folded = fold(event_log, content_store, task_id).runtime.messages
    # The request opens with the session's composed context; from the goal on
    # it is the recorded conversation, which the fold reproduces exactly, plus
    # the final answer.
    start = next(i for i, m in enumerate(sent) if _text(m) == "start it and wait")
    history = sent[start:]
    assert [(m.role, m.origin, m.content) for m in folded[:-1]] == [
        (m.role, m.origin, m.content) for m in history
    ]
    assert _text(folded[-1]) == "saw it"
    types = [e.type for e in event_log.read(task_id)]
    assert types.count("InjectionRequested") == 1
    consumed = [
        e for e in event_log.read(task_id)
        if e.type == "MessagesAppended"
        and getattr(e.payload, "consumes_injection", None)
    ]
    assert len(consumed) == 1


def test_a_notice_to_a_finished_or_closed_session_is_dropped(
    tmp_path: Path,
) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    provider = FakeLLMProvider(responses=[_end_turn("hi"), _end_turn("hi")])
    host, event_log, content_store = _host(ws, provider)
    driver = InteractionDriver(host)
    delivery = BackgroundDelivery(event_log=event_log, content_store=content_store)
    delivery.set_notifier(driver)

    closed = driver.start(goal="one", agent="main").task_id
    driver.close(closed)
    cancelled = driver.start(goal="two", agent="main").task_id
    driver.cancel(cancelled)
    for task_id in (closed, cancelled):
        delivery.post_notice(task_id=task_id, notice="<task-notification/>")
        types = [e.type for e in event_log.read(task_id)]
        assert "InjectionRequested" not in types


def test_notices_posted_while_parked_keep_their_order(tmp_path: Path) -> None:
    """Several notices to a parked session land in one driven turn, in the
    order they were posted."""
    ws = tmp_path / "ws"
    ws.mkdir()
    provider = FakeLLMProvider(responses=[_end_turn("hi"), _end_turn("ack")])
    host, event_log, content_store = _host(ws, provider)
    driver = InteractionDriver(host)
    task_id = driver.start(goal="go", agent="main").task_id
    delivery = BackgroundDelivery(event_log=event_log, content_store=content_store)
    # Queue all three before a notifier can drive anything.
    for n in range(3):
        driver.inject_notice(task_id, notice=f"<task-notification>{n}")
    delivery.set_notifier(driver)
    delivery.post_notice(task_id=task_id, notice="<task-notification>3")
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and len(provider.received_requests) < 2:
        time.sleep(0.02)
    while time.monotonic() < deadline and fold(
        event_log, content_store, task_id
    ).status != "suspended":
        time.sleep(0.02)
    messages = fold(event_log, content_store, task_id).runtime.messages
    texts = [_text(messages[i]) for i in _notices(messages)]
    assert texts == [f"<task-notification>{n}" for n in range(4)]
    assert len(provider.received_requests) == 2


def test_a_monitor_event_reaches_the_running_turn(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    provider = FakeLLMProvider(
        responses=[
            _tool(
                "m1",
                "Monitor",
                {
                    "command": "sleep 0.3; echo READY on 8080; sleep 30",
                    "description": "server ready line",
                    "timeout_ms": 60000,
                },
            ),
            _tool("c2", "Bash", {"command": "sleep 1.5"}),
            _end_turn("done"),
        ]
    )
    host, event_log, content_store = _host(ws, provider)
    driver = InteractionDriver(host)
    host.set_background_notifier(driver)
    driver.start(goal="watch it", agent="main")
    third = list(provider.received_requests[2].messages)
    events = [_text(third[i]) for i in _notices(third)]
    assert len(events) == 1
    assert '<summary>Monitor event: "server ready line"</summary>' in events[0]
    assert "<event>READY on 8080</event>" in events[0]
    host.shutdown_background()


def _host_with_subtasks(
    workspace: Path, provider: FakeLLMProvider
) -> tuple[SdkHost, InMemoryEventLog, InMemoryContentStore]:
    host, event_log, content_store = _host(workspace, provider)
    # Spawned subtasks are enqueued by the default lifecycle observers.
    wire_default_observers(event_log, host.dispatcher)
    return host, event_log, content_store


def _delegate(goal: str) -> LLMResponse:
    return _tool(
        "t1",
        "Task",
        {"description": "child job", "prompt": goal, "subagent_type": "general-purpose"},
    )


def test_a_subtask_job_notifies_the_subtask_while_it_runs(tmp_path: Path) -> None:
    """A background command a subtask started notifies that subtask, mid-turn,
    not the root waiting on it."""
    ws = tmp_path / "ws"
    ws.mkdir()
    provider = FakeLLMProvider(
        responses=[
            _delegate("run it"),
            _tool(
                "k1",
                "Bash",
                {
                    "command": "sleep 0.3; echo CHILD-OUT",
                    "description": "child job",
                    "run_in_background": True,
                },
            ),
            _tool("k2", "Bash", {"command": "sleep 1.5"}),
            _end_turn("child done"),
            _end_turn("root done"),
        ]
    )
    host, event_log, content_store = _host_with_subtasks(ws, provider)
    driver = InteractionDriver(host)
    host.set_background_notifier(driver)
    root = driver.start(goal="go", agent="main")

    child_last = list(provider.received_requests[3].messages)
    notices = [_text(child_last[i]) for i in _notices(child_last)]
    assert len(notices) == 1 and "CHILD-OUT" in notices[0]
    root_last = list(provider.received_requests[4].messages)
    assert _notices(root_last) == []
    root_task = fold(event_log, content_store, root.task_id)
    assert root_task.governance.pending_injections == {}
    host.shutdown_background()


def test_a_subtask_job_ending_after_the_subtask_notifies_the_root(
    tmp_path: Path,
) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    provider = FakeLLMProvider(
        responses=[
            _delegate("start it"),
            _tool(
                "k1",
                "Bash",
                {
                    "command": "sleep 0.5; echo LATE-OUT",
                    "description": "late job",
                    "run_in_background": True,
                },
            ),
            _end_turn("child done"),
            _end_turn("root done"),
            _end_turn("saw the late job"),
        ]
    )
    host, event_log, content_store = _host_with_subtasks(ws, provider)
    driver = InteractionDriver(host)
    host.set_background_notifier(driver)
    root = driver.start(goal="go", agent="main")
    assert root.status == "suspended"

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and len(provider.received_requests) < 5:
        time.sleep(0.02)
    assert len(provider.received_requests) == 5
    woke = list(provider.received_requests[4].messages)
    notices = [_text(woke[i]) for i in _notices(woke)]
    assert len(notices) == 1 and "LATE-OUT" in notices[0]
    host.shutdown_background()


def test_a_notice_the_subtask_never_took_in_moves_to_the_root(
    tmp_path: Path,
) -> None:
    """The notice reaches the subtask during its last model call, so the
    subtask ends with it still pending; it is handed to the root instead of
    being lost."""
    ws = tmp_path / "ws"
    ws.mkdir()
    script = [
        _delegate("start it"),
        _tool(
            "k1",
            "Bash",
            {
                "command": "sleep 0.2; echo RACE-OUT",
                "description": "racing job",
                "run_in_background": True,
            },
        ),
        _end_turn("child done"),
    ]

    def respond(request: LLMRequest) -> LLMResponse:
        index = len(provider.received_requests) - 1
        if index == 2:
            time.sleep(1.0)  # the job ends while the subtask's last call runs
        return script[index] if index < len(script) else _end_turn("root done")

    provider = FakeLLMProvider(responder=respond)
    host, event_log, content_store = _host_with_subtasks(ws, provider)
    driver = InteractionDriver(host)
    host.set_background_notifier(driver)
    root = driver.start(goal="go", agent="main")

    def root_notices() -> list[str]:
        out: list[str] = []
        for request in provider.received_requests[3:]:
            messages = list(request.messages)
            out.extend(_text(messages[i]) for i in _notices(messages))
        return out

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not any(
        "RACE-OUT" in n for n in root_notices()
    ):
        time.sleep(0.05)
    assert any("RACE-OUT" in n for n in root_notices())
    child_id = next(
        e.payload.subtask_id
        for e in event_log.read(root.task_id)
        if e.type == "SubtaskSpawned"
    )
    child = fold(event_log, content_store, child_id)
    assert child.status == "terminal"
    assert child.governance.pending_injections  # never taken in by the subtask
    host.shutdown_background()
