"""noeta.sdk task catalog — ``task_status`` / ``task_summaries``.

The read-only half of the command surface: a host has to answer "where does
this conversation rest?" without driving a verb, and has to be able to
reconcile its own index against the ledger. Both were reachable only through
``noeta.read_models`` / ``noeta.core.fold``, which a product host may not
import.
"""

from __future__ import annotations

from pathlib import Path

from noeta.protocols.messages import LLMResponse, TextBlock, Usage
from noeta.sdk import NEXT_GOAL_WAKE_HANDLE, Client, Options, TaskStatus
from noeta.testing.fake_llm import FakeLLMProvider


#: Exactly the row `list_task_summaries` promises. Pinned so a widening of the
#: read model shows up here rather than in a product's parser.
SUMMARY_KEYS = {
    "task_id",
    "status",
    "closed",
    "last_seq",
    "last_event_time",
    "created_event_time",
    "parent_task_id",
    "agent_name",
    "workspace_dir",
    "background_jobs",
}


def _provider(n: int = 6) -> FakeLLMProvider:
    return FakeLLMProvider(
        responses=[
            LLMResponse(
                stop_reason="end_turn",
                content=[TextBlock(text=f"reply-{i}")],
                usage=Usage(uncached=1, output=1),
            )
            for i in range(n)
        ]
    )


def _client(workspace: Path) -> Client:
    return Client(
        Options(
            system_prompt="finish",
            name="main",
            allowed_tools=(),
            permission_mode="bypassPermissions",
        ),
        provider=_provider(),
        workspace_dir=workspace,
        multi_turn=True,
    )


def test_task_status_reports_the_next_goal_suspend(tmp_path: Path) -> None:
    """A finished interactive turn rests on the next-goal handle.

    ``status`` alone cannot say that — it reads ``suspended`` for an approval
    wait too — so the handle is the field that tells a UI "type again" apart
    from "answer me first".
    """
    client = _client(tmp_path)
    try:
        task_id = client.start(goal="alpha").task_id

        status = client.task_status(task_id)
        assert isinstance(status, TaskStatus)
        assert status.task_id == task_id
        assert status.status == "suspended"
        assert status.wake_handle == NEXT_GOAL_WAKE_HANDLE
        assert status.closed is False
        assert status.parent_task_id is None
    finally:
        client.shutdown()


def test_task_status_is_none_for_an_unknown_task(tmp_path: Path) -> None:
    """Unknown is distinguishable from ``pending``.

    A fold answers "pending" for both a created-but-unstarted task and a stream
    that does not exist, so existence is asked separately — otherwise a typo'd
    id reads as a real, freshly-created conversation.
    """
    client = _client(tmp_path)
    try:
        assert client.task_status("no-such-task") is None
    finally:
        client.shutdown()


def test_closed_is_orthogonal_to_status(tmp_path: Path) -> None:
    """Closing archives the conversation; it does not change where it rests.

    ``close`` is advisory — ``send_goal`` reopens — so a closed task is still
    ``suspended``. A host that treated ``closed`` as a fifth status would hide
    a resumable conversation.
    """
    client = _client(tmp_path)
    try:
        task_id = client.start(goal="alpha").task_id
        client.close(task_id)

        status = client.task_status(task_id)
        assert status is not None
        assert status.closed is True
        assert status.status == "suspended"
    finally:
        client.shutdown()


def test_task_summaries_rows_match_the_pinned_shape(tmp_path: Path) -> None:
    client = _client(tmp_path)
    try:
        first = client.start(goal="alpha").task_id
        second = client.start(goal="beta").task_id

        rows = client.task_summaries()
        by_id = {row["task_id"]: row for row in rows}
        assert {first, second} <= set(by_id)
        assert set(by_id[first]) == SUMMARY_KEYS
        assert by_id[first]["parent_task_id"] is None
        assert by_id[first]["agent_name"] == "main"
    finally:
        client.shutdown()


def test_task_summaries_agrees_with_task_status(tmp_path: Path) -> None:
    """The two reads fold the same ledger, so they cannot disagree.

    They take different paths — one row-per-task over the whole log, one
    snapshot-accelerated single fold — and a host will mix them (list from one,
    detail from the other), so a divergence would surface as a UI that
    contradicts itself.
    """
    client = _client(tmp_path)
    try:
        task_id = client.start(goal="alpha").task_id
        client.close(task_id)

        row = next(r for r in client.task_summaries() if r["task_id"] == task_id)
        status = client.task_status(task_id)
        assert status is not None
        assert row["status"] == status.status
        assert row["closed"] == status.closed
        assert row["parent_task_id"] == status.parent_task_id
    finally:
        client.shutdown()
