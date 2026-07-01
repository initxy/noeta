"""noeta.sdk ``Client.delete_task`` — hard-delete a session (task + subtask tree).

The conversation IS the task (D6), so deletion purges each task's event stream +
dispatcher state, cascaded across the whole subtask tree (a subtask rides its
root). Hash-addressed content blobs are shared and left for offline GC.
"""

from __future__ import annotations

from pathlib import Path

from noeta.protocols.messages import LLMResponse, TextBlock, Usage
from noeta.sdk import Client, Options
from noeta.testing.fake_llm import FakeLLMProvider


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


def test_delete_task_purges_and_unknown_not_found(tmp_path: Path) -> None:
    client = _client(tmp_path)
    try:
        t1 = client.start(goal="alpha").task_id
        t2 = client.start(goal="beta").task_id
        assert client.events(t1)

        result = client.delete_task(t1)
        assert result["ok"] is True
        assert result["deleted"] == [t1]
        assert client.events(t1) == []  # purged
        assert client.events(t2)  # sibling untouched
        ids = {s.task_id for s in client.task_streams()}
        assert t1 not in ids and t2 in ids

        gone = client.delete_task("no-such-task")
        assert gone["ok"] is False and gone["reason"] == "not_found"
    finally:
        client.shutdown()


def test_delete_task_cascades_subtree(tmp_path: Path) -> None:
    client = _client(tmp_path)
    try:
        root = client.start(goal="root").task_id
        child = client.start(goal="child").task_id
        # A real subtask is engine-internal; fabricate the parent link the
        # cascade folds from each genesis TaskCreated.parent_task_id so the BFS
        # tree-walk + multi-task purge are exercised deterministically.
        original = client._genesis_parent
        client._genesis_parent = (  # type: ignore[method-assign]
            lambda tid: root if tid == child else original(tid)
        )
        result = client.delete_task(root)
        assert result["ok"] is True
        assert set(result["deleted"]) == {root, child}
        assert client.events(root) == [] and client.events(child) == []
    finally:
        client.shutdown()


def test_delete_task_running_guard(tmp_path: Path) -> None:
    client = _client(tmp_path)
    try:
        t1 = client.start(goal="alpha").task_id
        client._host.dispatcher.has_active_lease = (  # type: ignore[attr-defined]
            lambda tid: tid == t1
        )
        result = client.delete_task(t1)
        assert result["ok"] is False and result["reason"] == "running"
        assert client.events(t1)  # not purged
    finally:
        client.shutdown()
