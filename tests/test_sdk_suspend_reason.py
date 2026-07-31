"""noeta.sdk ``Client.suspend_reason`` + the parsed ``SuspendReason`` view.

``status="suspended"`` collapses three very different rests into one word: the
ordinary wait for the human, a turn a human stopped, and a turn that failed and
was parked instead of terminating. A host renders those three differently, so
it needs the reason — split into ``(kind, detail)``, with the kind *spelled
once*, not hard-coded in a product's translator.

The producers of the ``interrupted`` / ``turn_failed:`` tags are pinned at the
driver level (``test_turn_interrupt.py``, ``test_code_multi_turn.py``); what is
pinned here is the public accessor, the parse convention, and that both
producers bind the very constants ``noeta.sdk`` exports.
"""

from __future__ import annotations

from pathlib import Path

from noeta.protocols.messages import LLMResponse, TextBlock, Usage
from noeta.sdk import (
    SUSPEND_REASON_INTERRUPTED,
    SUSPEND_REASON_TURN_FAILED,
    SUSPEND_REASON_WAITING_HUMAN,
    Client,
    Options,
    SuspendReason,
    parse_suspend_reason,
)
from noeta.testing.fake_llm import FakeLLMProvider


def _client(workspace: Path) -> Client:
    return Client(
        Options(
            system_prompt="finish",
            name="main",
            allowed_tools=(),
            permission_mode="bypassPermissions",
        ),
        provider=FakeLLMProvider(
            responses=[
                LLMResponse(
                    stop_reason="end_turn",
                    content=[TextBlock(text=f"reply-{i}")],
                    usage=Usage(uncached=1, output=1),
                )
                for i in range(4)
            ]
        ),
        workspace_dir=workspace,
        multi_turn=True,
    )


def test_an_ordinary_turn_rests_on_waiting_human(tmp_path: Path) -> None:
    client = _client(tmp_path)
    try:
        task_id = client.start(goal="alpha").task_id
        assert client.suspend_reason(task_id) == SuspendReason(
            kind=SUSPEND_REASON_WAITING_HUMAN, detail=None
        )
    finally:
        client.shutdown()


def test_suspend_reason_reads_the_most_recent_suspend(tmp_path: Path) -> None:
    """Per-turn, not per-conversation — the scan runs backwards.

    A conversation accumulates one suspend per turn, so a forward scan would
    pin the UI to whatever happened first and never notice a later stop.
    """
    client = _client(tmp_path)
    try:
        task_id = client.start(goal="alpha").task_id
        client.send_goal(task_id, goal="beta")

        suspends = [
            env
            for env in client.events(task_id)
            if env.type == "TaskSuspended"
        ]
        assert len(suspends) >= 2, "each turn should park once"
        assert client.suspend_reason(task_id) == parse_suspend_reason(
            suspends[-1].payload.reason
        )
    finally:
        client.shutdown()


def test_suspend_reason_is_none_when_a_task_never_suspended(tmp_path: Path) -> None:
    client = _client(tmp_path)
    try:
        assert client.suspend_reason("no-such-task") is None
    finally:
        client.shutdown()


def test_the_producers_bind_the_exported_constants() -> None:
    """One definition per tag.

    The tags are written deep in the runtime (a policy wrapper, a worker) and
    compared far away in a product's UI layer. Re-spelling the literal in either
    place is how the two silently drift, so the producers import the very
    objects ``noeta.sdk`` exports — identity, not equality.
    """
    from noeta.execution.multi_turn import TURN_FAILED_SUSPEND_TAG
    from noeta.runtime.worker import STOP_INTERRUPTED_SUSPEND_REASON

    assert TURN_FAILED_SUSPEND_TAG is SUSPEND_REASON_TURN_FAILED
    assert STOP_INTERRUPTED_SUSPEND_REASON is SUSPEND_REASON_INTERRUPTED


def test_parse_splits_kind_from_detail() -> None:
    """The recorded tag is ``turn_failed: <policy reason>``; the parse undoes it.

    Pinned as the two halves of one convention: the producer joins with
    ``": "``, ``parse_suspend_reason`` splits on the first ``": "`` — so a
    detail that itself contains ``": "`` survives intact, and a host compares
    ``kind`` with plain ``==`` instead of substring-matching the raw tag.
    """
    recorded = f"{SUSPEND_REASON_TURN_FAILED}: provider 503"
    assert parse_suspend_reason(recorded) == SuspendReason(
        kind=SUSPEND_REASON_TURN_FAILED, detail="provider 503"
    )
    # A bare tag has no detail half.
    assert parse_suspend_reason(SUSPEND_REASON_INTERRUPTED) == SuspendReason(
        kind=SUSPEND_REASON_INTERRUPTED, detail=None
    )
    # Only the FIRST ": " is the seam.
    nested = f"{SUSPEND_REASON_TURN_FAILED}: provider: 503"
    assert parse_suspend_reason(nested).detail == "provider: 503"
