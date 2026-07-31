"""Task fork — branching a conversation off its event-sourced ledger.

``fork`` is ``rewind``'s twin: the same anchor (a user-goal
``MessagesAppended``), the same fold-through boundary, but the resulting
baseline lands on a NEW task's stream instead of re-basing the source. These
tests pin the two halves that make it a fork rather than a rewind — the source
is untouched, and the branch is independently drivable — plus the fold
invariant the ``TaskForked`` baseline exists to preserve.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests._sdk_session import official_registry as official_agent_registry

from noeta.client import SdkHost
from noeta.core.fold import fold, messages_from_appended
from noeta.execution.driver import (
    InteractionDriver,
    NotForkableError,
    multi_turn_policy_wrapper,
)
from noeta.execution.multi_turn import NEXT_GOAL_WAKE_HANDLE
from noeta.protocols.messages import LLMResponse, TextBlock, Usage
from noeta.runtime.shell_policy import ShellMode
from noeta.runtime.workspace import FsWriteMode
from noeta.storage.memory import (
    InMemoryContentStore,
    InMemoryDispatcher,
    InMemoryEventLog,
)
from noeta.testing.fake_llm import FakeLLMProvider


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _end_turn(text: str = "done") -> LLMResponse:
    return LLMResponse(
        stop_reason="end_turn",
        content=[TextBlock(text=text)],
        usage=Usage(uncached=1, output=1),
        raw={},
    )


def _host(
    workspace: Path, *, responses: list[LLMResponse]
) -> tuple[SdkHost, InMemoryDispatcher, InMemoryEventLog]:
    dispatcher = InMemoryDispatcher()
    event_log = InMemoryEventLog(lease_validator=dispatcher)
    content_store = InMemoryContentStore()
    host = SdkHost(
        event_log=event_log,
        content_store=content_store,
        dispatcher=dispatcher,
        provider=FakeLLMProvider(responses=responses),
        model="gpt-test",
        workspace_dir=workspace,
        write_mode=FsWriteMode.APPLY,
        shell_mode=ShellMode.ALLOWLIST,
        policy_wrapper=multi_turn_policy_wrapper,
        registry=official_agent_registry(),
        aliases={"default": "main"},
    )
    return host, dispatcher, event_log


def _user_goal_seqs(event_log: Any, content_store: Any, task_id: str) -> list[int]:
    """Seqs of the user-goal ``MessagesAppended`` events (the fork anchors)."""
    seqs: list[int] = []
    for env in event_log.read(task_id):
        if env.type != "MessagesAppended":
            continue
        msgs = messages_from_appended(env, content_store)
        if msgs and msgs[0].role == "user":
            seqs.append(env.seq)
    return seqs


def _texts(task: Any) -> list[str]:
    return [
        " ".join(getattr(b, "text", "") for b in getattr(m, "content", []))
        for m in task.runtime.messages
    ]


def _two_turn_conversation(
    tmp_path: Path, *, extra_responses: int = 0
) -> tuple[InteractionDriver, SdkHost, InMemoryEventLog, str]:
    """A conversation with two driven turns ("first", then "second")."""
    ws = tmp_path / "ws"
    ws.mkdir()
    responses = [_end_turn("turn1"), _end_turn("turn2")]
    responses += [_end_turn(f"extra{i}") for i in range(extra_responses)]
    host, _, event_log = _host(ws, responses=responses)
    driver = InteractionDriver(host)
    started = driver.start(goal="first", agent="main")
    driver.send_goal(started.task_id, goal="second")
    return driver, host, event_log, started.task_id


# ---------------------------------------------------------------------------
# The fork is a new task; the source is untouched
# ---------------------------------------------------------------------------


def test_fork_leaves_the_source_stream_byte_identical(tmp_path: Path) -> None:
    """The defining difference from rewind: branching writes NOTHING to the
    conversation it branched from."""
    driver, host, event_log, task_id = _two_turn_conversation(tmp_path)
    before = list(event_log.read(task_id))
    second_goal = _user_goal_seqs(event_log, host.content_store, task_id)[1]

    outcome = driver.fork(task_id, message_seq=second_goal)

    assert outcome.task_id != task_id
    assert list(event_log.read(task_id)) == before


def test_fork_inherits_history_through_the_anchor_boundary(tmp_path: Path) -> None:
    """The branch folds to the source's state as of the turn boundary BEFORE
    the anchor message — turn 1 survives, the anchored turn does not."""
    driver, host, event_log, task_id = _two_turn_conversation(tmp_path)
    second_goal = _user_goal_seqs(event_log, host.content_store, task_id)[1]

    forked = driver.fork(task_id, message_seq=second_goal).task_id
    branch = fold(event_log, host.content_store, forked)

    texts = _texts(branch)
    assert any("first" in t for t in texts)
    assert not any("second" in t for t in texts)
    # Identity is re-stamped: the inherited body must not fold back as its source.
    assert branch.task_id == forked
    # A fork is a sibling, never a subtask — nothing may wake a "parent".
    assert branch.parent_task_id is None
    # Landed on a clean turn boundary ⇒ immediately live.
    assert branch.status == "suspended"


def test_fork_genesis_records_its_lineage(tmp_path: Path) -> None:
    driver, host, event_log, task_id = _two_turn_conversation(tmp_path)
    second_goal = _user_goal_seqs(event_log, host.content_store, task_id)[1]

    forked = driver.fork(task_id, message_seq=second_goal).task_id

    stream = event_log.read(forked)
    assert stream[0].type == "TaskCreated"
    marker = next(e for e in stream if e.type == "TaskForked")
    assert marker.payload.source_task_id == task_id
    assert marker.payload.source_seq < second_goal
    # The source's agent identity carries over to the branch.
    assert stream[0].payload.agent_name == "main"


def test_fork_and_source_diverge_independently(tmp_path: Path) -> None:
    """Both branches drive their own next turn; neither sees the other's."""
    driver, host, event_log, task_id = _two_turn_conversation(
        tmp_path, extra_responses=2
    )
    second_goal = _user_goal_seqs(event_log, host.content_store, task_id)[1]
    forked = driver.fork(task_id, message_seq=second_goal).task_id

    branch_out = driver.send_goal(forked, goal="down-the-branch")
    assert branch_out.status == "suspended"
    assert branch_out.wake_handle == NEXT_GOAL_WAKE_HANDLE

    source_out = driver.send_goal(task_id, goal="down-the-source")
    assert source_out.status == "suspended"

    branch_texts = _texts(fold(event_log, host.content_store, forked))
    source_texts = _texts(fold(event_log, host.content_store, task_id))
    assert any("down-the-branch" in t for t in branch_texts)
    assert not any("down-the-source" in t for t in branch_texts)
    assert any("down-the-source" in t for t in source_texts)
    assert not any("down-the-branch" in t for t in source_texts)
    # The source kept the turn the branch dropped.
    assert any("second" in t for t in source_texts)


def test_forked_task_folds_the_same_from_scratch(tmp_path: Path) -> None:
    """The invariant ``TaskForked`` is a real baseline FOR: a forked task's
    inherited history is not derivable from its own genesis, so the
    from-scratch path must honour the marker exactly like the accelerated one."""
    driver, host, event_log, task_id = _two_turn_conversation(
        tmp_path, extra_responses=1
    )
    second_goal = _user_goal_seqs(event_log, host.content_store, task_id)[1]
    forked = driver.fork(task_id, message_seq=second_goal).task_id
    driver.send_goal(forked, goal="down-the-branch")

    accelerated = fold(event_log, host.content_store, forked)
    scratch = fold(event_log, host.content_store, forked, ignore_snapshots=True)
    assert scratch.state_dict() == accelerated.state_dict()
    assert any("first" in t for t in _texts(scratch))


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


def test_fork_refuses_a_seq_that_is_not_a_user_message(tmp_path: Path) -> None:
    driver, host, event_log, task_id = _two_turn_conversation(tmp_path)
    plan_seq = next(
        e.seq for e in event_log.read(task_id) if e.type == "ContextPlanComposed"
    )
    with pytest.raises(NotForkableError) as excinfo:
        driver.fork(task_id, message_seq=plan_seq)
    assert excinfo.value.code == "not_forkable"
    # No branch was minted.
    assert len(event_log.list_task_streams()) == 1


def test_fork_refuses_the_opening_message(tmp_path: Path) -> None:
    """Branching at the first goal would inherit nothing and land on a
    ``pending`` baseline no ``send_goal`` could resume — that is a new task,
    not a fork."""
    driver, host, event_log, task_id = _two_turn_conversation(tmp_path)
    first_goal = _user_goal_seqs(event_log, host.content_store, task_id)[0]
    with pytest.raises(NotForkableError) as excinfo:
        driver.fork(task_id, message_seq=first_goal)
    assert "no prior turn" in excinfo.value.reason
    assert len(event_log.list_task_streams()) == 1


def test_fork_refuses_an_unknown_task(tmp_path: Path) -> None:
    driver, _, _, _ = _two_turn_conversation(tmp_path)
    with pytest.raises(NotForkableError) as excinfo:
        driver.fork("task-nonexistent", message_seq=1)
    assert excinfo.value.reason == "no events"


def test_fork_refuses_a_subtask(tmp_path: Path) -> None:
    """A fork inherits ``parent_task_id`` verbatim, so branching a child would
    mint a phantom sibling its parent never spawned."""
    driver, host, event_log, task_id = _two_turn_conversation(tmp_path)
    engine = host.resolve_engine(fold(event_log, host.content_store, task_id))
    child = engine.create_task(
        goal="child work", policy_name="react", parent_task_id=task_id
    )
    with pytest.raises(NotForkableError) as excinfo:
        driver.fork(child.task_id, message_seq=1)
    assert excinfo.value.reason == "not a root task"
