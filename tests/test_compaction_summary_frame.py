"""The compacted prefix arrives labelled as a summary, not as a fresh request.

Without a frame the note is just another ``user`` turn: a summary that says
"the user asked for X" reads as the user asking for X again, and the model
re-does work it already finished. The frame is applied at COMPOSE time only —
the stored body never carries it — so an already-compacted task picks it up on
its next step, and a chain of compactions can never nest one frame inside
another.
"""

from __future__ import annotations

from noeta.context.composer import _SUMMARY_FRAME, ThreeSegmentComposer
from noeta.protocols.canonical import to_canonical_bytes
from noeta.protocols.messages import Message, TextBlock
from noeta.protocols.task import Task
from noeta.storage.memory import InMemoryContentStore


def _composer(store: InMemoryContentStore) -> ThreeSegmentComposer:
    return ThreeSegmentComposer(
        system_prompt="sys", tools={}, content_store=store
    )


def _task_with_summary(
    store: InMemoryContentStore, body: str, *, boundary: int = 2
) -> Task:
    task = Task(task_id="t-frame")
    task.runtime.messages = [
        Message(role="user", content=[TextBlock(text=f"m{i}")])
        for i in range(4)
    ]
    task.context.summary_ref = store.put(
        to_canonical_bytes(body), media_type="application/json"
    )
    task.context.summary_boundary = boundary
    return task


def test_summary_message_opens_with_the_frame() -> None:
    store = InMemoryContentStore()
    task = _task_with_summary(store, "The user asked for a refactor.")
    head = _composer(store)._apply_summary(task)[0]

    assert head.role == "user"
    # ``origin`` stays unset: the compaction Policy's note-shape gate requires
    # an origin-less single-TextBlock user turn to recognise its own note.
    assert head.origin is None
    assert len(head.content) == 1
    text = head.content[0].text  # type: ignore[union-attr]
    assert text.startswith(_SUMMARY_FRAME)
    assert text.endswith("The user asked for a refactor.")
    # It says what the note is, and that what it restates already happened.
    assert "stands in for the messages it replaced" in _SUMMARY_FRAME
    assert "not a new request" in _SUMMARY_FRAME


def test_frame_is_not_stored_so_recompaction_cannot_nest_it() -> None:
    """The body in the ContentStore is untouched, and a body that already
    opens with the frame (the model echoed it back into its own note) is not
    framed twice."""
    store = InMemoryContentStore()
    body = "note one"
    task = _task_with_summary(store, body)
    composer = _composer(store)

    assert composer._decode_summary(task.context.summary_ref) == body
    for _ in range(3):  # composing again never stacks frames
        text = composer._apply_summary(task)[0].content[0].text  # type: ignore[union-attr]
        assert text.count(_SUMMARY_FRAME) == 1

    echoed = _task_with_summary(store, f"{_SUMMARY_FRAME}\n\nnote two")
    text = composer._apply_summary(echoed)[0].content[0].text  # type: ignore[union-attr]
    assert text.count(_SUMMARY_FRAME) == 1


def test_frame_does_not_read_as_a_safety_constraint() -> None:
    """The frame rides the same message the constraint detector scans before
    every re-compaction; a trigger word in it would invent a constraint that
    then outlives every future summary."""
    from noeta.builtins.react.impl import extract_safety_constraints

    assert extract_safety_constraints(
        [Message(role="user", content=[TextBlock(text=_SUMMARY_FRAME)])]
    ) == []


def test_constraints_in_a_framed_note_are_still_detected() -> None:
    """The frame adds one line; the detector works line by line, so a
    constraint carried forward inside a framed note keeps binding."""
    from noeta.builtins.react.impl import extract_safety_constraints

    store = InMemoryContentStore()
    task = _task_with_summary(
        store, "Progress so far.\n- Do not touch config/secrets.yaml."
    )
    head = _composer(store)._apply_summary(task)[0]

    assert extract_safety_constraints([head]) == [
        "Do not touch config/secrets.yaml."
    ]
