"""⑤ Background sub-agent — ``spawn_subagent(background=true)``.

docs/adr/background-subagent.md.

A parent calls ``spawn_subagent(agent, goal, background=true)``. Unlike a
foreground spawn (suspend on a ``SubtaskCompleted`` barrier), the parent gets a
"started" tool_result and KEEPS its turn; the sub-agent runs concurrently on the
shared executor and its result is delivered at the parent's next turn boundary
via Mechanism C (a ``BackgroundSubagentDelivered`` anchor + an ``origin="system"``
notice). The child is invisible to the ``ChildLifecycleObserver`` (its genesis
is ``background=True``).

Two test styles:

* **deterministic** (a recording launcher stub, no executor) locks the
  engine-side contract: Started event / child genesis / started receipt / parent
  continues / launch seam called / governance audit / cap rejection / observer
  invisibility.
* **integration** (the real registry + executor + Mechanism-C delivery, a
  content ``responder`` so the concurrent child does not race the cursor) proves
  the end-to-end: the background child runs, finishes, and proactively wakes the
  idle parent with its result.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Optional

from noeta.core.fold import fold
from noeta.policies.react import SPAWN_SUBAGENT_TOOL
from noeta.protocols.canonical import to_canonical_bytes
from noeta.protocols.messages import (
    LLMRequest,
    LLMResponse,
    Message,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
)
from noeta.protocols.wake import HumanResponseReceived, SubtaskCompleted
from noeta.testing.fake_llm import FakeLLMProvider
from noeta.tools.fs import FsWriteMode, ShellMode

from tests._sdk_session import (
    default_coding_budget,
    make_driver,
    make_host,
    make_registry,
    preset_spec,
    runner_main_spec,
)


PARENT_GOAL = "kick off the background research and keep chatting"
CHILD_GOAL = "research-topic-omega thoroughly and report"
CHILD_RESULT = "omega finding: it is well supported"
SPAWN_CALL_ID = "bg-spawn-1"
STARTED_MARKER = "runs concurrently while you keep working"
NOTICE_TAG = "<background-subagent "


# ---------------------------------------------------------------------------
# scripted responses
# ---------------------------------------------------------------------------


def _spawn_bg(
    agent: str = "explore", goal: str = CHILD_GOAL, call_id: str = SPAWN_CALL_ID
) -> LLMResponse:
    return LLMResponse(
        stop_reason="tool_use",
        content=[
            ToolUseBlock(
                call_id=call_id,
                tool_name=SPAWN_SUBAGENT_TOOL,
                arguments={"agent": agent, "goal": goal, "background": True},
            )
        ],
        usage=Usage(uncached=1, output=1),
        raw={"id": call_id},
    )


def _end(text: str) -> LLMResponse:
    return LLMResponse(
        stop_reason="end_turn",
        content=[TextBlock(text=text)],
        usage=Usage(uncached=1, output=1),
        raw={"id": "end"},
    )


def _make_ws(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir(parents=True)
    return ws


def _host(ws: Path, provider: FakeLLMProvider, **knobs: Any):
    main = runner_main_spec("main", delegation=True, spawnable=("explore",))
    children = [preset_spec(n) for n in ("explore", "general-purpose", "plan")]
    host = make_host(
        make_registry(main, *children),
        workspace_dir=ws,
        provider=provider,
        model="gpt-test",
        multi_turn=True,  # interactive: a finished turn suspends on next-goal
        write_mode=FsWriteMode.APPLY,
        shell_mode=ShellMode.OFF,
        budget=default_coding_budget(),
        **knobs,
    )
    driver = make_driver(host)
    # The Client wires this in production; a bare host+driver test must too, or
    # Mechanism-C delivery is a no-op (the durable record stands either way).
    host.set_background_notifier(driver)
    return host, driver


class _RecordingLauncher:
    """A stub background-sub-agent launcher: records launches, never drives.

    Lets the deterministic tests assert the ENGINE-side contract (events, child
    genesis, started receipt, parent continuation) without the executor /
    delivery timing. ``capacity`` returns ``reject`` to exercise the cap path."""

    def __init__(self, reject: Optional[str] = None) -> None:
        self.launched: list[tuple[str, str]] = []
        self.capacity_calls: list[str] = []
        self._reject = reject

    def capacity(self, parent_task_id: str) -> Optional[str]:
        self.capacity_calls.append(parent_task_id)
        return self._reject

    def launch(self, *, parent_task_id: str, child_task_id: str) -> None:
        self.launched.append((parent_task_id, child_task_id))


def _install_stub(host: Any, stub: _RecordingLauncher) -> None:
    # Swap the real registry for the recorder BEFORE the first engine is built
    # (engines are built lazily on the first resolve).
    object.__setattr__(host, "_background_subagents", stub)


def _events(host: Any, task_id: str) -> list[str]:
    return [e.type for e in host.event_log.read(task_id)]


def _started_payload(host: Any, parent_id: str):
    for e in host.event_log.read(parent_id):
        if e.type == "BackgroundSubagentStarted":
            return e.payload
    return None


# ---------------------------------------------------------------------------
# deterministic engine-side contract (stub launcher, no executor)
# ---------------------------------------------------------------------------


def test_background_spawn_continues_turn_and_launches(tmp_path: Path) -> None:
    provider = FakeLLMProvider(responses=[_spawn_bg(), _end("started; chatting")])
    host, driver = _host(_make_ws(tmp_path), provider)
    stub = _RecordingLauncher()
    _install_stub(host, stub)

    out = driver.start(goal=PARENT_GOAL, agent="main")

    # Parent did NOT suspend on a subtask barrier — it finished its turn and
    # rests on the interactive next-goal suspend.
    assert out.status == "suspended"
    parent = fold(host.event_log, host.content_store, out.task_id)
    assert isinstance(parent.wake_on, HumanResponseReceived)
    assert not isinstance(parent.wake_on, SubtaskCompleted)

    # The durable record is BackgroundSubagentStarted (not SubtaskSpawned +
    # TaskSuspended-on-barrier).
    types = _events(host, out.task_id)
    assert "BackgroundSubagentStarted" in types
    assert "SubtaskSpawned" not in types
    started = _started_payload(host, out.task_id)
    assert started.agent_name == "explore"
    assert started.goal == CHILD_GOAL
    assert started.call_id == SPAWN_CALL_ID

    # The parent got a SUCCESS "started" tool_result paired to the spawn call.
    paired = [
        b
        for m in parent.runtime.messages
        if m.role == "tool"
        for b in m.content
        if isinstance(b, ToolResultBlock) and b.call_id == SPAWN_CALL_ID
    ]
    assert paired and paired[0].success is True
    assert STARTED_MARKER in paired[0].output

    # The launch seam fired with (parent, child); governance audits it running.
    assert len(stub.launched) == 1
    parent_id, child_id = stub.launched[0]
    assert parent_id == out.task_id
    audit = parent.governance.background_subagents
    assert len(audit) == 1
    assert audit[0]["subtask_id"] == child_id
    assert audit[0]["status"] == "running"
    assert audit[0]["agent_name"] == "explore"


def test_background_child_genesis_is_marked_and_observer_skips_it(
    tmp_path: Path,
) -> None:
    provider = FakeLLMProvider(responses=[_spawn_bg(), _end("ok")])
    host, driver = _host(_make_ws(tmp_path), provider)
    stub = _RecordingLauncher()
    _install_stub(host, stub)

    out = driver.start(goal=PARENT_GOAL, agent="main")
    _, child_id = stub.launched[0]

    # The child's genesis carries background=True...
    child_created = [
        e for e in host.event_log.read(child_id) if e.type == "TaskCreated"
    ]
    assert child_created and child_created[0].payload.background is True
    assert child_created[0].payload.parent_task_id == out.task_id

    # ...so the ChildLifecycleObserver never recorded a phantom completion /
    # wake against the (un-barriered) parent.
    assert "SubtaskCompleted" not in _events(host, out.task_id)


def test_foreground_child_genesis_omits_background_key(tmp_path: Path) -> None:
    """A normal (foreground) spawn's TaskCreated folds background→None and its
    canonical bytes never carry the key (byte-equal to pre-feature recordings)."""
    from noeta.protocols.events import TaskCreatedPayload

    fg = TaskCreatedPayload(goal="g", policy_name="scripted", agent_name="explore")
    assert fg.background is None
    assert b"background" not in to_canonical_bytes(fg)
    bg = TaskCreatedPayload(
        goal="g", policy_name="scripted", agent_name="explore", background=True
    )
    assert b"background" in to_canonical_bytes(bg)


def test_background_spawn_over_cap_is_rejected_without_durable_trace(
    tmp_path: Path,
) -> None:
    provider = FakeLLMProvider(responses=[_spawn_bg(), _end("acknowledged")])
    host, driver = _host(_make_ws(tmp_path), provider)
    stub = _RecordingLauncher(reject="too many background sub-agents (8/8)")
    _install_stub(host, stub)

    out = driver.start(goal=PARENT_GOAL, agent="main")
    parent = fold(host.event_log, host.content_store, out.task_id)

    # Reject = no durable sub-agent: no Started event, no child, no launch.
    assert "BackgroundSubagentStarted" not in _events(host, out.task_id)
    assert stub.launched == []
    assert parent.governance.background_subagents == []
    # ...but the model got a clear "not started" tool_result and kept its turn.
    paired = [
        b
        for m in parent.runtime.messages
        if m.role == "tool"
        for b in m.content
        if isinstance(b, ToolResultBlock) and b.call_id == SPAWN_CALL_ID
    ]
    assert paired and paired[0].success is False
    assert "not started" in paired[0].output.lower() or (
        "not started" in (paired[0].error or "").lower()
    )


def test_resume_reproduces_background_audit(tmp_path: Path) -> None:
    """fold is the single writer: re-folding the parent stream rebuilds the
    same ``background_subagents`` audit (deterministic, EventLog-reconstructable)."""
    provider = FakeLLMProvider(responses=[_spawn_bg(), _end("ok")])
    host, driver = _host(_make_ws(tmp_path), provider)
    stub = _RecordingLauncher()
    _install_stub(host, stub)
    out = driver.start(goal=PARENT_GOAL, agent="main")

    a = fold(host.event_log, host.content_store, out.task_id)
    b = fold(host.event_log, host.content_store, out.task_id)
    assert a.governance.background_subagents == b.governance.background_subagents
    assert len(a.governance.background_subagents) == 1


# ---------------------------------------------------------------------------
# integration: real registry + executor + Mechanism-C delivery
# ---------------------------------------------------------------------------


def _req_text(req: LLMRequest) -> str:
    parts: list[str] = []
    sys = req.system
    if isinstance(sys, Message):
        parts.extend(b.text for b in sys.content if isinstance(b, TextBlock))
    elif sys is not None:
        parts.append(str(sys))
    for m in req.messages:
        for b in m.content:
            if isinstance(b, TextBlock):
                parts.append(b.text)
            elif isinstance(b, ToolResultBlock) and isinstance(b.output, str):
                parts.append(b.output)
    return "\n".join(parts)


def _bg_responder(req: LLMRequest) -> LLMResponse:
    text = _req_text(req)
    # the isolated child sees only its own goal.
    if CHILD_GOAL in text and PARENT_GOAL not in text:
        return _end(CHILD_RESULT)
    # parent: notice turn (Mechanism C) → finish; started receipt present →
    # finish the spawning turn; fresh goal → spawn in the background.
    if NOTICE_TAG in text:
        return _end("Background research came back; wrapping up.")
    if STARTED_MARKER in text:
        return _end("Launched it in the background; carrying on.")
    return _spawn_bg()


def _wait_for(predicate, *, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_background_subagent_end_to_end_delivers_result(tmp_path: Path) -> None:
    provider = FakeLLMProvider(responder=_bg_responder)
    host, driver = _host(_make_ws(tmp_path), provider)

    out = driver.start(goal=PARENT_GOAL, agent="main")
    assert out.status == "suspended"  # parent idle after its spawning turn

    # The background child + Mechanism-C delivery happen asynchronously on the
    # executor / a daemon drive thread. The delivery anchor
    # (``BackgroundSubagentDelivered``) is written BEFORE the notice turn that
    # appends the parent-visible notice message (driver.seed_notify_background_
    # subagent_exit: anchor first, THEN the seeded turn's MessagesAppended), so
    # waiting on the anchor alone races that later write. Wait for the notice
    # itself — the real post-condition, which implies the anchor landed too.
    def _parent_notice() -> list:
        parent = fold(host.event_log, host.content_store, out.task_id)
        return [
            m
            for m in parent.runtime.messages
            if any(
                isinstance(b, TextBlock) and NOTICE_TAG in b.text
                for b in m.content
            )
        ]

    assert _wait_for(lambda: bool(_parent_notice())), (
        "no background-subagent completion notice in the parent view"
    )
    assert "BackgroundSubagentDelivered" in _events(host, out.task_id), (
        "background sub-agent result was never delivered"
    )

    parent = fold(host.event_log, host.content_store, out.task_id)
    audit = parent.governance.background_subagents
    assert len(audit) == 1
    entry = audit[0]
    assert entry["status"] == "completed"
    child_id = entry["subtask_id"]

    # The child really ran to terminal with its scripted result.
    child = fold(host.event_log, host.content_store, child_id)
    assert child.status == "terminal"

    # The result was delivered as an origin="system" notice the parent saw, and
    # the full result is derefable from the recorded ref.
    notice = [
        m
        for m in parent.runtime.messages
        if any(
            isinstance(b, TextBlock) and NOTICE_TAG in b.text for b in m.content
        )
    ]
    assert notice, "no background-subagent completion notice in the parent view"
    result_bytes = host.content_store.get(entry["result_ref"])
    assert CHILD_RESULT in result_bytes.decode("utf-8")


def test_background_delivery_anchor_is_exactly_once(tmp_path: Path) -> None:
    """The ``BackgroundSubagentDelivered`` anchor is written once; a re-fold
    never re-injects, and the audit flips running→completed exactly once."""
    provider = FakeLLMProvider(responder=_bg_responder)
    host, driver = _host(_make_ws(tmp_path), provider)
    out = driver.start(goal=PARENT_GOAL, agent="main")
    assert _wait_for(
        lambda: "BackgroundSubagentDelivered" in _events(host, out.task_id)
    )
    # exactly one Started and one Delivered on the parent stream.
    types = _events(host, out.task_id)
    assert types.count("BackgroundSubagentStarted") == 1
    assert types.count("BackgroundSubagentDelivered") == 1


def test_recover_redrives_undelivered_background_subagent(tmp_path: Path) -> None:
    """Crash recovery: a Started-without-Delivered child whose stream is
    non-terminal (the host died mid-flight) is re-enqueued + re-driven from its
    own EventLog at startup, then delivered."""
    from noeta.execution.background_subagent import BackgroundSubagentRegistry

    ws = _make_ws(tmp_path)
    provider = FakeLLMProvider(responder=_bg_responder)
    host, driver = _host(ws, provider)
    # Phase 1 — a launcher stub creates the child (background genesis) but never
    # drives it: exactly the durable state a crash leaves (Started, no Delivered,
    # child non-terminal).
    stub = _RecordingLauncher()
    _install_stub(host, stub)
    out = driver.start(goal=PARENT_GOAL, agent="main")
    _, child_id = stub.launched[0]
    assert "BackgroundSubagentDelivered" not in _events(host, out.task_id)
    child = fold(host.event_log, host.content_store, child_id)
    assert child.status != "terminal"

    # Phase 2 — "restart": swap the stub for the REAL registry and recover.
    real = BackgroundSubagentRegistry(
        event_log=host.event_log,
        content_store=host.content_store,
        dispatcher=host.dispatcher,
        build_host=host._drain_host_for_id,
        deliver=host._on_background_subagent_exit,
    )
    object.__setattr__(host, "_background_subagents", real)
    recovered = host.recover_background_subagents()
    assert child_id in recovered

    # The child is re-driven to terminal from its own EventLog and delivered.
    assert _wait_for(
        lambda: "BackgroundSubagentDelivered" in _events(host, out.task_id)
    )
    child = fold(host.event_log, host.content_store, child_id)
    assert child.status == "terminal"


def test_cancelled_background_subagent_is_marked_terminal(tmp_path: Path) -> None:
    """A background child whose drive is aborted by the session cancel/close
    cascade (``cancel_check`` → ``TaskCancellationRequested``) is marked
    ``TaskCancelled`` on its OWN stream — not left a non-terminal orphan — and
    its result is NOT delivered.

    White-box on the executor done-callback: the cancel cascade makes
    ``_drive_member_to_terminal`` raise ``TaskCancellationRequested``, which the
    executor captures on the future and hands to ``_on_done``. We reproduce that
    exact hand-off deterministically (the real cancel→drive race is not
    reproducible under the serial FakeLLM cursor). Without the fix the child
    stays non-terminal, so a later crash-recovery scan (``_child_is_terminal`` →
    False) would re-drive a cancelled child to completion."""
    from concurrent.futures import Future

    from noeta.execution.background_subagent import BackgroundSubagentRegistry
    from noeta.protocols.errors import TaskCancellationRequested

    ws = _make_ws(tmp_path)
    provider = FakeLLMProvider(responses=[_spawn_bg(), _end("chatting")])
    host, driver = _host(ws, provider)
    # Spawn via the recorder so the child gets genesis (Started + child
    # TaskCreated) but is never driven — the durable state present when the
    # cancel cascade aborts an in-flight drive.
    stub = _RecordingLauncher()
    _install_stub(host, stub)
    out = driver.start(goal=PARENT_GOAL, agent="main")
    _, child_id = stub.launched[0]
    assert fold(host.event_log, host.content_store, child_id).status != "terminal"

    delivered: list[tuple[str, str]] = []
    registry = BackgroundSubagentRegistry(
        event_log=host.event_log,
        content_store=host.content_store,
        dispatcher=host.dispatcher,
        build_host=host._drain_host_for_id,
        deliver=lambda parent, child: delivered.append((parent, child)),
    )
    future: "Future[None]" = Future()
    future.set_exception(TaskCancellationRequested(child_id))
    registry._on_done(future, out.task_id, child_id)

    # The child is now terminal via its OWN TaskCancelled...
    assert "TaskCancelled" in _events(host, child_id)
    assert fold(host.event_log, host.content_store, child_id).status == "terminal"
    # ...so a recovery scan classifies it terminal and never re-drives it.
    assert registry._child_is_terminal(child_id) is True
    # ...and a cancelled child is NOT delivered (session is being torn down).
    assert delivered == []
    assert "BackgroundSubagentDelivered" not in _events(host, out.task_id)


def test_delivery_gives_up_when_parent_never_settles(tmp_path: Path) -> None:
    """The bounded retry-until-idle give-up branch: if the parent never settles
    to a next-goal suspend before the deadline, delivery returns WITHOUT writing
    a ``BackgroundSubagentDelivered`` anchor (the documented v1 drop — no
    duplicate, may lose). Exercised with a notifier that always raises (a parent
    perpetually mid-turn) + a clamped deadline."""
    from noeta.protocols.events import TaskCreatedPayload

    ws = _make_ws(tmp_path)
    # A parent that just rests idle: suspended on next-goal, NON-terminal, so the
    # delivery loop keeps retrying rather than dropping on a terminal parent.
    provider = FakeLLMProvider(responses=[_end("idle")])
    host, driver = _host(ws, provider)
    out = driver.start(goal="just chat", agent="main")
    assert out.status == "suspended"
    parent_id = out.task_id
    trace_id = next(iter(host.event_log.read(parent_id))).trace_id

    # A background child with genesis but no terminal projects to a non-None
    # ("stuck") result, so delivery actually enters its retry loop.
    child_id = "bg-child-never-settles"
    host.event_log.system_emit(
        task_id=child_id,
        type="TaskCreated",
        payload=TaskCreatedPayload(
            goal=CHILD_GOAL,
            policy_name="scripted",
            agent_name="explore",
            parent_task_id=parent_id,
            inputs={},
            subtask_depth=1,
            background=True,
        ),
        actor="engine",
        origin="engine",
        trace_id=trace_id,
    )

    class _AlwaysMidTurn:
        def notify_background_subagent_exit(self, *args: Any, **kw: Any) -> None:
            raise RuntimeError("parent still mid-turn")

    # Clamp the deadline so the give-up is fast. These are class-level constants
    # (not frozen dataclass fields), so set them on the type and restore after.
    cls = type(host)
    setattr(cls, "_BG_SUBAGENT_DELIVER_TIMEOUT_S", 0.1)
    setattr(cls, "_BG_SUBAGENT_DELIVER_POLL_S", 0.02)
    try:
        started = time.monotonic()
        host._drive_background_subagent_exit(_AlwaysMidTurn(), parent_id, child_id)
        elapsed = time.monotonic() - started
    finally:
        setattr(cls, "_BG_SUBAGENT_DELIVER_TIMEOUT_S", 30.0)
        setattr(cls, "_BG_SUBAGENT_DELIVER_POLL_S", 0.05)

    assert elapsed < 5.0  # gave up at the deadline, did not hang
    assert "BackgroundSubagentDelivered" not in _events(host, parent_id)
