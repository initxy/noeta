"""The core ``erased`` record: the person's messages at the given seqs, and
the ``inline`` text a plugin expanded them into, leave the conversation and
its history; nothing else does, and a snapshot carries the result."""

from __future__ import annotations

from typing import Any

from noeta.core import snapshots
from noeta.core.events import (
    ExtensionRecorded,
    InboxItem,
    InputTaken,
    Recorded,
    Say,
    TaskCreated,
)
from noeta.core.loop.state import fold
from noeta.core.records import CORE
from noeta.core.types import user


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
    assert [m.text for m in state.history] == ["from another agent"]
    assert state.message_seqs == [4]


def test_an_erased_conversation_survives_a_snapshot() -> None:
    records = _records()
    data = snapshots.encode(fold(records[:3], {}))
    assert data is not None
    resumed = fold(records[3:], {}, state=snapshots.decode(data))
    assert resumed.messages == fold(records, {}).messages
    again = snapshots.decode(snapshots.encode(resumed) or b"")
    assert [m.text for m in again.messages] == ["from another agent"]
