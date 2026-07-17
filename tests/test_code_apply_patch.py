"""M1 — `apply_patch` end-to-end through a coding session: batch apply
and one-approval HITL.
"""

from __future__ import annotations

from pathlib import Path

from tests._read_models.result import _collect_files_changed
from noeta.protocols.messages import LLMResponse, TextBlock, ToolUseBlock, Usage
from noeta.testing.fake_llm import FakeLLMProvider
from noeta.tools.fs import FsWriteMode, ShellMode

from tests._sdk_session import make_driver, make_host, make_registry, runner_main_spec


def _ws(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    (ws / "x.py").write_text("foo\n", encoding="utf-8")
    return ws


def _patch_call() -> LLMResponse:
    return LLMResponse(
        stop_reason="tool_use",
        content=[
            ToolUseBlock(
                call_id="p1",
                tool_name="apply_patch",
                arguments={
                    "edits": [
                        {"op": "replace", "path": "x.py", "old": "foo", "new": "bar"},
                        {"op": "create", "path": "y.py", "content": "new\n"},
                    ]
                },
            )
        ],
        usage=Usage(uncached=1, output=1),
        raw={"id": "p1"},
    )


def _end() -> LLMResponse:
    return LLMResponse(
        stop_reason="end_turn",
        content=[TextBlock(text="done")],
        usage=Usage(uncached=1, output=1),
        raw={"id": "end"},
    )


def _session(
    tmp_path: Path,
    *,
    require_approval: bool = False,
):
    """A one-shot SDK host + driver for the apply_patch batch.

    ``require_approval_tools=("apply_patch",)`` is the host-level gate (mirroring
    the old ``CodeSessionConfig`` knob); ``()`` keeps the batch applying without
    approval (the SDK host's default permission_mode would otherwise gate the
    write family)."""
    host = make_host(
        make_registry(runner_main_spec("main")),
        workspace_dir=_ws(tmp_path),
        provider=FakeLLMProvider(responses=[_patch_call(), _end()]),
        model="gpt-test",
        multi_turn=False,
        write_mode=FsWriteMode.APPLY,
        shell_mode=ShellMode.OFF,
        require_approval_tools=("apply_patch",) if require_approval else (),
    )
    return host, make_driver(host)


def test_apply_patch_through_session(tmp_path: Path) -> None:
    host, driver = _session(tmp_path)
    out = driver.start(goal="apply a 2-file patch", agent="main")
    events = host.event_log.read(out.task_id)
    assert out.status == "terminal"
    starts = [
        e for e in events
        if e.type == "ToolCallStarted" and e.payload.tool_name == "apply_patch"
    ]
    assert len(starts) == 1
    ws = tmp_path / "ws"
    assert (ws / "x.py").read_text() == "bar\n"
    assert (ws / "y.py").read_text() == "new\n"
    # the per-file rows surfaced into the session summary
    paths = {
        row["path"]
        for row in _collect_files_changed(events, host.content_store)
    }
    assert {"x.py", "y.py"} <= paths


def test_apply_patch_one_approval_for_batch(tmp_path: Path) -> None:
    host, driver = _session(tmp_path, require_approval=True)
    out = driver.start(goal="apply a 2-file patch", agent="main")
    assert out.status == "suspended"
    assert out.wake_handle == "approval-p1"  # one approval, whole batch
    result = driver.approve(out.task_id, call_id="p1")
    assert result.status == "terminal"
    ws = tmp_path / "ws"
    assert (ws / "x.py").read_text() == "bar\n"
    assert (ws / "y.py").read_text() == "new\n"


def test_apply_patch_deny_writes_nothing(tmp_path: Path) -> None:
    host, driver = _session(tmp_path, require_approval=True)
    out = driver.start(goal="apply a 2-file patch", agent="main")
    driver.deny(out.task_id, call_id="p1")
    ws = tmp_path / "ws"
    assert (ws / "x.py").read_text() == "foo\n"  # denied → not applied
    assert not (ws / "y.py").exists()
