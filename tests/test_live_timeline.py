"""Issue 04 — live event timeline (consume the fan-out).

Two layers:

* **unit**: :class:`LiveTimelineObserver` projects the four canonical tool
  lifecycle / assistant-text envelopes into terminal lines, reading only
  ``EventEnvelope`` (no private event type), and swallows render failures
  (Observer must never raise back into the EventLog writer).
* **integration**: an SDK host wired with a ``LiveTimelineObserver`` renders
  the tool-call lifecycle *live* as the turn runs, and the recording it writes
  is **identical** to the same run without the live observer wired — the
  observer is a pure read-side projection that appends nothing, so the EventLog
  a resume folds is untouched (issue 04 acceptance #4). TL6 removed
  the ``noeta code`` CLI; the production ``SdkHost`` + ``InteractionDriver``
  assembly is the reusable integration surface (the observer self-subscribes on
  ``host.event_log`` exactly like the shipping backend wires it).
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import pytest

from noeta.agent.observe.live_timeline import LiveTimelineObserver
from noeta.core._decision_handlers import put_messages
from noeta.protocols.messages import LLMResponse, Message, TextBlock, ToolUseBlock, Usage
from noeta.storage.memory import InMemoryContentStore, InMemoryEventLog
from noeta.testing.fake_llm import FakeLLMProvider


# ---------------------------------------------------------------------------
# unit — observer projects canonical envelopes only
# ---------------------------------------------------------------------------


def _wire(stream: io.StringIO) -> tuple[InMemoryEventLog, InMemoryContentStore, LiveTimelineObserver]:
    log = InMemoryEventLog()
    cs = InMemoryContentStore()
    obs = LiveTimelineObserver(event_log=log, content_store=cs, stream=stream)
    return log, cs, obs


def test_tool_call_lifecycle_renders_started_args_result_ok() -> None:
    """started → args → result → ok are all visible from canonical
    ToolCallStarted / ToolResultRecorded envelopes."""
    out = io.StringIO()
    log, cs, obs = _wire(out)
    log.system_emit(
        task_id="t-1",
        type="ToolCallStarted",
        payload=_started("c1", "edit", {"path": "x.py", "old": "a"}),
        actor="engine",
        origin="engine",
    )
    log.system_emit(
        task_id="t-1",
        type="ToolResultRecorded",
        payload=_result("c1", success=True, summary="edit: edited x.py", cs=cs),
        actor="tool",
        origin="tool",
    )
    obs.stop()
    text = out.getvalue()
    assert "→ edit(" in text
    assert 'path="x.py"' in text  # args visible
    assert "✔ edit [ok]" in text
    assert "edited x.py" in text  # result summary visible


def test_tool_call_failure_renders_fail_glyph() -> None:
    out = io.StringIO()
    log, cs, obs = _wire(out)
    log.system_emit(
        task_id="t-1",
        type="ToolCallStarted",
        payload=_started("c2", "shell_run", {"command": "pytest"}),
        actor="engine",
        origin="engine",
    )
    log.system_emit(
        task_id="t-1",
        type="ToolResultRecorded",
        payload=_result("c2", success=False, summary="shell_run: exit 1", cs=cs),
        actor="tool",
        origin="tool",
    )
    obs.stop()
    text = out.getvalue()
    assert "✗ shell_run [fail]" in text


def test_assistant_text_messages_appended_is_rendered() -> None:
    """MessagesAppended (assistant TextBlock) is dereferenced from
    ContentStore and printed as prose; user/tool messages are not."""
    out = io.StringIO()
    log, cs, obs = _wire(out)
    payload = put_messages(
        cs,
        [
            Message(role="user", content=[TextBlock(text="please fix it")]),
            Message(role="assistant", content=[TextBlock(text="On it — reading files.")]),
        ],
    )
    log.system_emit(
        task_id="t-1",
        type="MessagesAppended",
        payload=payload,
        actor="engine",
        origin="engine",
    )
    obs.stop()
    text = out.getvalue()
    assert "On it — reading files." in text
    assert "please fix it" not in text  # user prose is not the model's speech


def test_non_projected_event_types_are_ignored() -> None:
    """An envelope type the timeline does not project (e.g. TaskCreated)
    prints nothing — no CLI-private event type, no noise."""
    out = io.StringIO()
    log, cs, obs = _wire(out)
    log.system_emit(
        task_id="t-1",
        type="TaskStarted",
        payload=_TaskStartedLike(lease_id="L1"),
        actor="engine",
        origin="engine",
    )
    obs.stop()
    assert out.getvalue() == ""


def test_render_failure_is_swallowed_not_raised() -> None:
    """A broken stream must not raise back into the EventLog writer:
    the append still succeeds."""

    class _BoomStream:
        def write(self, _s: str) -> int:
            raise RuntimeError("boom")

        def flush(self) -> None:  # pragma: no cover - print() flush path
            raise RuntimeError("boom")

    log = InMemoryEventLog()
    cs = InMemoryContentStore()
    obs = LiveTimelineObserver(event_log=log, content_store=cs, stream=_BoomStream())  # type: ignore[arg-type]
    env = log.system_emit(
        task_id="t-1",
        type="ToolCallStarted",
        payload=_started("c9", "glob", {"pattern": "*"}),
        actor="engine",
        origin="engine",
    )
    # The write blew up inside the observer but the append committed.
    assert env.type == "ToolCallStarted"
    obs.stop()


# ---------------------------------------------------------------------------
# integration — a code run renders the timeline live + recording is identical
#
# TL6 removed the operator CLI (``noeta code``); the live timeline is a
# pure read-side projection of the production ``SdkHost`` + ``InteractionDriver``
# assembly — these integration tests construct a ``LiveTimelineObserver`` on the
# host event log (the same self-subscribing extension seam the backend uses)
# before driving. The CLI-only batch ``== noeta code summary ==`` stdout
# rendering went away with the command — the behavioural surface that survives
# (and matters) is the live tool-call lifecycle on the stream.
# ---------------------------------------------------------------------------


def _replace_then_finish() -> list[LLMResponse]:
    return [
        LLMResponse(
            stop_reason="tool_use",
            content=[
                ToolUseBlock(
                    call_id="rt-1",
                    tool_name="edit",
                    arguments={"path": "x.py", "old": "foo", "new": "bar"},
                )
            ],
            usage=Usage(uncached=1, output=1),
            raw={"id": "resp-1"},
        ),
        LLMResponse(
            stop_reason="end_turn",
            content=[TextBlock(text="all done — renamed foo to bar")],
            usage=Usage(uncached=1, output=1),
            raw={"id": "resp-2"},
        ),
    ]


def test_run_renders_live_timeline_on_stream(
    tmp_path: Path,
) -> None:
    """A code run wired with a ``LiveTimelineObserver`` prints the tool-call
    lifecycle + assistant text live, as the turn runs.

    Drives the production ``SdkHost`` + ``InteractionDriver`` assembly with the
    observer self-subscribed on the host event log, so the timeline appears the
    moment each canonical envelope commits."""
    from noeta.tools.fs import FsWriteMode, ShellMode

    from tests._sdk_session import (
        make_driver,
        make_host,
        make_registry,
        runner_main_spec,
    )

    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "x.py").write_text("foo\n")

    stream = io.StringIO()
    host = make_host(
        make_registry(runner_main_spec("main")),
        workspace_dir=ws,
        provider=FakeLLMProvider(responses=_replace_then_finish()),
        model="gpt-test",
        multi_turn=False,
        write_mode=FsWriteMode.APPLY,
        shell_mode=ShellMode.OFF,
        # The old runner applied edits without an approval pause; disable the
        # SDK host's default write-family gate to keep the one-shot apply.
        require_approval_tools=(),
    )
    obs = LiveTimelineObserver(
        event_log=host.event_log,
        content_store=host.content_store,
        stream=stream,
    )
    try:
        make_driver(host).start(goal="rename foo to bar", agent="main")
    finally:
        obs.stop()

    out = stream.getvalue()
    # Tool lifecycle visible live on the stream.
    assert "→ edit(" in out
    assert "✔ edit [ok]" in out
    # Assistant prose visible live on the stream.
    assert "all done — renamed foo to bar" in out


def test_live_observer_appends_nothing_to_eventlog(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The live observer is a pure read-side projection: a run with it
    wired records the **same event sequence** (same types in the same
    order, same count) as a run without it — it appends nothing, so the
    EventLog a resume folds is untouched (issue 04 acceptance #4).

    (Across two independent *live* runs, volatile ids — lease/call/llm
    uuids — differ by construction. Here we pin what the observer can
    actually affect: the stream's shape — and it must change nothing.)"""
    from noeta.tools.fs import FsWriteMode, ShellMode

    from tests._sdk_session import (
        make_driver,
        make_host,
        make_registry,
        runner_main_spec,
    )

    def _record(*, live_stream: Any) -> list[str]:
        ws = tmp_path / ("ws-live" if live_stream is not None else "ws-quiet")
        ws.mkdir()
        (ws / "x.py").write_text("foo\n")
        host = make_host(
            make_registry(runner_main_spec("main")),
            workspace_dir=ws,
            provider=FakeLLMProvider(responses=_replace_then_finish()),
            model="gpt-test",
            multi_turn=False,
            write_mode=FsWriteMode.DRY_RUN,
            shell_mode=ShellMode.OFF,
            require_approval_tools=(),
        )
        obs = (
            LiveTimelineObserver(
                event_log=host.event_log,
                content_store=host.content_store,
                stream=live_stream,
            )
            if live_stream is not None
            else None
        )
        try:
            out = make_driver(host).start(goal="rename foo to bar", agent="main")
            return [e.type for e in host.event_log.read(out.task_id)]
        finally:
            if obs is not None:
                obs.stop()

    with_observer = _record(live_stream=io.StringIO())
    without_observer = _record(live_stream=None)
    assert with_observer == without_observer


class _TaskStartedLike:
    """Minimal stand-in for a payload whose type the timeline ignores."""

    def __init__(self, *, lease_id: str) -> None:
        self.lease_id = lease_id


def _started(call_id: str, tool_name: str, arguments: dict[str, Any]) -> Any:
    from noeta.protocols.events import ToolCallStartedPayload

    return ToolCallStartedPayload(
        call_id=call_id, tool_name=tool_name, arguments=arguments
    )


def _result(
    call_id: str, *, success: bool, summary: str, cs: InMemoryContentStore
) -> Any:
    from noeta.protocols.events import ToolResultRecordedPayload

    ref = cs.put(b"null", media_type="application/json")
    return ToolResultRecordedPayload(
        call_id=call_id, success=success, output_ref=ref, summary=summary
    )
