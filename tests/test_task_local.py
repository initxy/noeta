"""Task-local runtime state and the turn's Engine.

The Engine is a per-turn value; what a task needs across turns lives in the
host's ``TaskLocalRegistry`` (``noeta.runtime.task_local``): the files it has
read (the edit tools' read-first precondition), the built-ins' slots, and —
for the turn's own span — the Engine itself.

* ``Read`` in one turn, ``Edit`` in the next: applied. ``Read``, then an
  ``Edit`` that waits for approval, then the approval: applied. Two tasks
  never share a read record.
* One Engine per turn: the approval resume finds the Engine the turn opened
  with; the next goal builds afresh; a parked conversation holds none; the
  conversation's end forgets everything.
* Each task's WebFetch page cache is its own, and it spans the task's turns.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from noeta.core.fold import fold
from noeta.protocols.messages import LLMResponse, TextBlock, ToolUseBlock, Usage
from noeta.runtime.shell_policy import ShellMode
from noeta.runtime.task_local import TaskLocalRegistry
from noeta.runtime.workspace import FsWriteMode
from noeta.testing.fake_llm import FakeLLMProvider

from tests._sdk_session import make_driver, make_host, make_registry, runner_main_spec


# ---------------------------------------------------------------------------
# the registry
# ---------------------------------------------------------------------------


def test_registry_keeps_one_record_per_task() -> None:
    reg = TaskLocalRegistry()
    reads = reg.read_registry("t1")
    assert reg.read_registry("t1") is reads
    assert reg.read_registry("t2") is not reads
    counter = {"n": 0}

    def make() -> dict[str, int]:
        counter["n"] += 1
        return {}

    slot = reg.slot("t1", "x", make)
    assert reg.slot("t1", "x", make) is slot and counter["n"] == 1
    assert reg.peek("t1", "x") is slot
    assert reg.peek("t1", "y") is None and reg.peek("t9", "x") is None
    assert "t9" not in reg  # peek never creates
    bound = reg.bind_slot("t1")
    assert bound("x", make) is slot
    assert reg.held_engine("t1") is None
    reg.hold_engine("t1", "engine")
    assert reg.held_engine("t1") == "engine"
    reg.drop_engine("t1")
    assert reg.held_engine("t1") is None and reg.peek("t1", "x") is slot
    reg.forget("t1")
    assert "t1" not in reg and reg.peek("t1", "x") is None
    reg.forget("t1")  # idempotent


def test_registry_evicts_the_least_recently_touched_task() -> None:
    reg = TaskLocalRegistry(max_tasks=2)
    reg.read_registry("a")
    reg.read_registry("b")
    reg.read_registry("a")  # a is fresher than b
    reg.read_registry("c")
    assert "a" in reg and "c" in reg and "b" not in reg


# ---------------------------------------------------------------------------
# read-first spans turns and approvals
# ---------------------------------------------------------------------------


def _end(text: str = "done") -> LLMResponse:
    return LLMResponse(
        stop_reason="end_turn",
        content=[TextBlock(text=text)],
        usage=Usage(uncached=1, output=1),
        raw={"id": text},
    )


def _call(call_id: str, name: str, args: dict[str, Any]) -> LLMResponse:
    return LLMResponse(
        stop_reason="tool_use",
        content=[ToolUseBlock(call_id=call_id, tool_name=name, arguments=args)],
        usage=Usage(uncached=1, output=1),
        raw={"id": call_id},
    )


def _read(call_id: str, path: str = "a.py") -> LLMResponse:
    return _call(call_id, "Read", {"file_path": path})


def _edit(call_id: str, path: str = "a.py") -> LLMResponse:
    return _call(
        call_id, "Edit", {"file_path": path, "old_string": "one", "new_string": "ONE"}
    )


def _fs_host(tmp_path: Path, responses: list[LLMResponse], **knobs: Any):
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    (ws / "a.py").write_text("one\ntwo\n")
    knobs.setdefault("require_approval_tools", ())
    host = make_host(
        make_registry(runner_main_spec("main")),
        workspace_dir=ws,
        provider=FakeLLMProvider(responses=responses),
        model="stub-model",
        multi_turn=True,
        write_mode=FsWriteMode.APPLY,
        shell_mode=ShellMode.OFF,
        **knobs,
    )
    return host, make_driver(host), ws


def _tool_results(host: Any, task_id: str) -> list[Any]:
    return [
        e.payload
        for e in host.event_log.read(task_id)
        if e.type == "ToolResultRecorded"
    ]


def test_read_in_one_turn_edit_in_the_next(tmp_path: Path) -> None:
    host, driver, ws = _fs_host(
        tmp_path, [_read("r1"), _end("read it"), _edit("e1"), _end("edited")]
    )
    started = driver.start(goal="read a.py", agent="main")
    assert started.status == "suspended"
    driver.send_goal(started.task_id, goal="now change it")
    results = _tool_results(host, started.task_id)
    assert [r.success for r in results] == [True, True], [r.summary for r in results]
    assert (ws / "a.py").read_text() == "ONE\ntwo\n"


def test_read_then_edit_across_an_approval(tmp_path: Path) -> None:
    host, driver, ws = _fs_host(
        tmp_path,
        [_read("r1"), _edit("e1"), _end("edited")],
        require_approval_tools=("Edit",),
    )
    started = driver.start(goal="change a.py", agent="main")
    assert started.wake_handle == "approval-e1"
    assert (ws / "a.py").read_text() == "one\ntwo\n"
    out = driver.approve(started.task_id, call_id="e1", resolver="host")
    assert out.status == "suspended"
    results = _tool_results(host, started.task_id)
    assert [r.success for r in results] == [True, True], [r.summary for r in results]
    assert (ws / "a.py").read_text() == "ONE\ntwo\n"


def test_one_tasks_read_does_not_let_another_edit(tmp_path: Path) -> None:
    host, driver, ws = _fs_host(
        tmp_path, [_read("r1"), _end("read it"), _edit("e1"), _end("tried")]
    )
    reader = driver.start(goal="read a.py", agent="main")
    editor = driver.start(goal="change a.py", agent="main")
    assert _tool_results(host, reader.task_id)[0].success
    (refused,) = _tool_results(host, editor.task_id)
    assert not refused.success and "has not been read yet" in refused.summary
    assert (ws / "a.py").read_text() == "one\ntwo\n"


# ---------------------------------------------------------------------------
# one Engine per turn
# ---------------------------------------------------------------------------


def test_the_turn_keeps_its_engine_and_the_next_turn_builds_afresh(
    tmp_path: Path,
) -> None:
    host, driver, _ = _fs_host(
        tmp_path,
        [_read("r1"), _edit("e1"), _end("edited"), _end("second")],
        require_approval_tools=("Edit",),
    )
    seen: list[Any] = []
    build = host._build_engine

    def spy(*args: Any, **kwargs: Any) -> Any:
        engine = build(*args, **kwargs)
        seen.append(engine)
        return engine

    host._build_engine = spy  # type: ignore[method-assign]
    started = driver.start(goal="change a.py", agent="main")
    tid = started.task_id
    # The seed's by-name build, its resolve, and the drive's build: the
    # drive's is the turn's, held while the turn waits on the approval.
    held = host._task_locals.held_engine(tid)
    assert held is seen[-1]
    task = fold(host.event_log, host.content_store, tid)
    assert host.resolve_engine(task) is held
    builds_before = len(seen)
    driver.approve(tid, call_id="e1", resolver="host")
    # The approval resumed on the turn's Engine — no build — and the turn's
    # end (parked on the next-goal handle) let it go.
    assert len(seen) == builds_before
    assert host._task_locals.held_engine(tid) is None
    driver.send_goal(tid, goal="again")
    assert len(seen) == builds_before + 1
    assert host._task_locals.held_engine(tid) is None
    # The conversation's end forgets the task's local state entirely.
    task = fold(host.event_log, host.content_store, tid)
    host.resolve_engine(task)
    assert tid in host._task_locals
    driver.close(tid)
    assert tid not in host._task_locals


def test_each_task_has_its_own_page_cache_across_turns(tmp_path: Path) -> None:
    host, driver, _ = _fs_host(tmp_path, [_end("a1"), _end("b1"), _end("a2")])
    a = driver.start(goal="a", agent="main")
    b = driver.start(goal="b", agent="main")
    driver.send_goal(a.task_id, goal="a again")

    def cache_of(task_id: str) -> Any:
        task = fold(host.event_log, host.content_store, task_id)
        engine = host.resolve_engine(task)
        host.forget_turn_engine(task_id)
        return engine._tools["WebFetch"].cache

    cache_a = cache_of(a.task_id)
    assert cache_of(a.task_id) is cache_a  # the next build gets the same one
    assert cache_of(b.task_id) is not cache_a
