"""Goal spill to the ContentStore (the ``goal_ref`` escape hatch).

A goal string is caller- or model-controlled, yet four payloads inline it into
an event envelope capped at ``EVENT_PAYLOAD_MAX_BYTES``: ``TaskCreated`` /
``SubtaskSpawned`` / ``SubtaskDenied`` / ``BackgroundSubagentStarted``. An
oversized goal rides in the ContentStore under a ``goal_ref`` instead, and both
halves of that trade are pinned here:

1. inline case: the canonical bytes carry no ``goal_ref`` key at all — a key
   appearing there would move the hash of every ordinary recording;
2. spill round-trip: ``spill_goal`` keeps the payload under the cap and
   ``goal_from_payload`` returns the full text;
3. seed path end to end (public ``Client.start``): an oversized goal is
   accepted, the genesis records the ref, and ``fold`` rebuilds
   ``state.goal`` in full;
4. child genesis: ``_emit_child_task_created`` spills, and the child's fold
   sees the full goal;
5. restore tolerance: a body without the key, a spilled body, and a body with
   a key this reader does not know all restore.
"""

from __future__ import annotations

from pathlib import Path

from noeta.client import Client, Options
from noeta.client.host_config import HostConfig
from noeta.core.engine import _emit_child_task_created
from noeta.core.fold import fold
from noeta.protocols.canonical import to_canonical_bytes
from noeta.protocols.events import (
    GOAL_INLINE_LIMIT,
    BackgroundSubagentStartedPayload,
    SubtaskDeniedPayload,
    SubtaskSpawnedPayload,
    TaskCreatedPayload,
    goal_from_payload,
    spill_goal,
)
from noeta.protocols.messages import LLMResponse, TextBlock, Usage
from noeta.protocols.values import EVENT_PAYLOAD_MAX_BYTES
from noeta.storage._payload_restore import _restore_payload
from noeta.storage.memory import (
    InMemoryContentStore,
    InMemoryDispatcher,
    InMemoryEventLog,
)
from noeta.testing.fake_llm import FakeLLMProvider

#: Canonical bytes of this goal exceed ``GOAL_INLINE_LIMIT`` (quotes push a
#: len == limit string over), so every emitter must spill it.
BIG_GOAL = "g" * (GOAL_INLINE_LIMIT + 1)


# ---------------------------------------------------------------------------
# 1 — the inline case carries no goal_ref key
# ---------------------------------------------------------------------------


def test_inline_goal_payloads_carry_no_goal_ref_key() -> None:
    payloads = (
        TaskCreatedPayload(goal="fix tests", policy_name="react", agent_name="main"),
        SubtaskSpawnedPayload(subtask_id="s1", agent_name="a", goal="g", inputs={}),
        SubtaskDeniedPayload(agent_name="a", goal="g", reason="nope"),
        BackgroundSubagentStartedPayload(
            subtask_id="s1", agent_name="a", goal="g", call_id="c1"
        ),
    )
    for payload in payloads:
        assert b"goal_ref" not in to_canonical_bytes(payload)


# ---------------------------------------------------------------------------
# 2 — spill round-trip
# ---------------------------------------------------------------------------


def test_spill_goal_roundtrip_and_payload_cap() -> None:
    store = InMemoryContentStore()
    assert spill_goal(store, "small") == ("small", None)

    inline, ref = spill_goal(store, BIG_GOAL)
    assert inline == ""
    assert ref is not None
    for payload in (
        TaskCreatedPayload(goal=inline, policy_name="react", goal_ref=ref),
        SubtaskSpawnedPayload(
            subtask_id="s1", agent_name="a", goal=inline, inputs={}, goal_ref=ref
        ),
        SubtaskDeniedPayload(
            agent_name="a", goal=inline, reason="nope", goal_ref=ref
        ),
        BackgroundSubagentStartedPayload(
            subtask_id="s1", agent_name="a", goal=inline, call_id="c1", goal_ref=ref
        ),
    ):
        assert goal_from_payload(payload, store) == BIG_GOAL
        assert len(to_canonical_bytes(payload)) <= EVENT_PAYLOAD_MAX_BYTES


# ---------------------------------------------------------------------------
# 3 — seed path end to end through the public Client
# ---------------------------------------------------------------------------


def test_client_start_accepts_oversized_goal(tmp_path: Path) -> None:
    dispatcher = InMemoryDispatcher()
    content_store = InMemoryContentStore()
    event_log = InMemoryEventLog(lease_validator=dispatcher)
    ws = tmp_path / "ws"
    ws.mkdir()
    provider = FakeLLMProvider(
        responses=[
            LLMResponse(
                stop_reason="end_turn",
                content=[TextBlock(text="done")],
                usage=Usage(uncached=1, output=1),
                raw={"id": "r1"},
            )
        ]
    )
    client = Client(
        Options(
            system_prompt="You are a test agent.",
            name="main",
            allowed_tools=(),
            permission_mode="bypassPermissions",
        ),
        provider=provider,
        workspace_dir=ws,
        model="stub-model",
        host_config=HostConfig(
            event_log=event_log,
            content_store=content_store,
            dispatcher=dispatcher,
            write_mode="apply",
        ),
    )
    outcome = client.start(goal=BIG_GOAL)
    task_id = outcome.task_id

    genesis = event_log.read(task_id)[0]
    assert genesis.type == "TaskCreated"
    assert genesis.payload.goal == ""
    assert genesis.payload.goal_ref is not None
    assert goal_from_payload(genesis.payload, content_store) == BIG_GOAL

    # fold's genesis bootstrap derefs the ref — resume sees the full goal.
    task = fold(event_log, content_store, task_id)
    assert task.state.goal == BIG_GOAL


# ---------------------------------------------------------------------------
# 4 — child genesis spills too (spawn paths reuse the same write point)
# ---------------------------------------------------------------------------


def test_child_task_created_spills_and_folds() -> None:
    dispatcher = InMemoryDispatcher()
    content_store = InMemoryContentStore()
    event_log = InMemoryEventLog(lease_validator=dispatcher)
    env = _emit_child_task_created(
        event_log,
        content_store,
        "engine",
        "scripted",
        child_task_id="child-1",
        parent_task_id="parent-1",
        agent_name="explore",
        goal=BIG_GOAL,
        inputs={},
        trace_id="trace-1",
        subtask_depth=1,
    )
    assert env.payload.goal == ""
    assert env.payload.goal_ref is not None

    child = fold(event_log, content_store, "child-1")
    assert child.state.goal == BIG_GOAL
    assert child.parent_task_id == "parent-1"


# ---------------------------------------------------------------------------
# 5 — restore tolerance (no ref / spilled / unknown extra key)
# ---------------------------------------------------------------------------


def test_restore_tolerates_all_goal_shapes() -> None:
    store = InMemoryContentStore()
    _, ref = spill_goal(store, BIG_GOAL)

    old_shape = {"goal": "fix", "policy_name": "react", "agent_name": "main"}
    restored = _restore_payload("TaskCreated", dict(old_shape))
    assert restored.goal == "fix"
    assert restored.goal_ref is None

    spilled_shape = {**old_shape, "goal": "", "goal_ref": ref}
    restored = _restore_payload("TaskCreated", dict(spilled_shape))
    assert restored.goal_ref == ref
    assert goal_from_payload(restored, store) == BIG_GOAL

    # A NEXT additive key from a newer writer must not crash this reader.
    forward_shape = {**old_shape, "not_yet_invented": True}
    restored = _restore_payload("TaskCreated", dict(forward_shape))
    assert restored.goal == "fix"

    for type_name, body in (
        ("SubtaskSpawned", {"subtask_id": "s1", "agent_name": "a", "goal": "g"}),
        ("SubtaskDenied", {"agent_name": "a", "goal": "g", "reason": "r"}),
        (
            "BackgroundSubagentStarted",
            {"subtask_id": "s1", "agent_name": "a", "goal": "g", "call_id": "c1"},
        ),
    ):
        restored = _restore_payload(type_name, {**body, "not_yet_invented": True})
        assert restored.goal == "g"
        assert restored.goal_ref is None
