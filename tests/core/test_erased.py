"""The core ``erased`` record: the person's messages at the given seqs, and
the ``inline`` text a plugin expanded them into, leave the conversation and
its history; nothing else does, and a snapshot carries the result."""

from __future__ import annotations

from typing import Any

from noeta.core import snapshots
from noeta.core.events import (
    Cancel,
    ExtensionRecorded,
    InboxItem,
    InputTaken,
    ModelReplied,
    Recorded,
    Say,
    TaskCreated,
    ToolStarted,
)
from noeta.core.loop.state import fold
from noeta.core.records import CORE, ERASED_KIND
from noeta.core.types import Message, ModelResponse, ToolUse, user


def _rec(seq: int, ev: Any) -> Recorded:
    return Recorded(seq=seq, at=0, event=ev)


def _records() -> list[Recorded]:
    return [
        _rec(1, TaskCreated(agent="a")),
        _rec(2, InputTaken(items=[InboxItem(seq=1, at=0, body=Say(message=user(
            "/deploy SECRET42", origin="human")))], through=1)),
        _rec(3, ExtensionRecorded(extension="commands", kind="ran",
                                  message=user("Deploy with key SECRET42 now.",
                                               origin="inline"))),
        _rec(4, ExtensionRecorded(extension="mail", kind="note",
                                  message=user("from another agent", origin="agent"))),
        _rec(5, ExtensionRecorded(extension=CORE, kind="erased", data={"seqs": [2, 3, 4]})),
    ]


def test_an_erase_takes_the_message_and_its_expansion_only() -> None:
    state = fold(_records(), {})
    assert [m.text for m in state.messages] == ["from another agent"]
    assert state.message_seqs == [4]


def test_an_erased_conversation_survives_a_snapshot() -> None:
    records = _records()
    data = snapshots.encode(fold(records[:3], {}))
    assert data is not None
    resumed = fold(records[3:], {}, state=snapshots.decode(data))
    assert resumed.messages == fold(records, {}).messages
    again = snapshots.decode(snapshots.encode(resumed) or b"")
    assert [m.text for m in again.messages] == ["from another agent"]


def test_every_plugin_with_state_folds_the_erase() -> None:
    seen: list[tuple[str, str, Any]] = []

    def reducer(name: str) -> Any:
        def reduce(state: Any, kind: str, data: Any) -> Any:
            seen.append((name, kind, data))
            return {"last": kind}
        return reduce

    state = fold(_records(), {"commands": reducer("commands"), "quiet": reducer("quiet")})
    assert ("commands", ERASED_KIND, {"seqs": [2, 3, 4]}) in seen
    # A plugin that never recorded anything is not woken by it.
    assert not any(name == "quiet" for name, _k, _d in seen)
    assert state.ext["commands"] == {"last": ERASED_KIND}


def test_an_erase_keeps_the_stop_marker_released_with_the_message() -> None:
    call = ToolUse(id="c1", name="Echo", input={})
    stop_marker = "[Request interrupted by user]"
    state = fold([
        _rec(1, TaskCreated(agent="a")),
        _rec(2, InputTaken(items=[InboxItem(seq=1, at=0, body=Say(message=user("go")))],
                           through=1)),
        _rec(3, ModelReplied(response=ModelResponse(
            message=Message(role="assistant", content=[call]), stop_reason="tool_use"))),
        _rec(4, ToolStarted(call=call)),
        _rec(5, InputTaken(items=[
            InboxItem(seq=2, at=0, body=Cancel(turn_only=True)),
            InboxItem(seq=3, at=0, body=Say(message=user("my SECRET is 42", origin="human"),
                                            when="now"))], through=3)),
        # The stop releases the held message at its own seq, after its marker.
        _rec(6, ExtensionRecorded(extension=CORE, kind="turn_stopped",
                                  data={"marker": stop_marker})),
        _rec(7, ExtensionRecorded(extension=CORE, kind="erased", data={"seqs": [6]})),
    ], {})
    texts = [m.text for m in state.messages]
    assert texts[-1] == stop_marker and not any("SECRET" in t for t in texts)
