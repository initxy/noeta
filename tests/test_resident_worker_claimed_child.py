"""A sub-agent child a resident worker claims ahead of the parent's delegation
drain must be opened and driven exactly as the drain would open it.

The race (``subtask_drain._ChildNotReady``): ``ChildLifecycleObserver``
enqueues every foreground child unreserved, so under ``num_workers >= 2`` an
idle worker's untargeted FIFO poll can lease a freshly-spawned child before the
parent's drain makes its targeted ``_descend_to_child`` lease. That claim goes
through ``run_leased_task`` → ``resolve_engine`` instead of the drain's
child-engine builder, and used to seed only the goal: the child ran on the
host-default model and reasoning effort, without its opening ``ModelBound``,
without its pre-loop residents (no workspace block), and with the leaf agent's
own delegation identity instead of the root's inherited spawn set.

These tests make the steal deterministic — an untargeted poll interposed at the
child's enqueue — and compare the stolen child's recording and first request
against the same script driven by the drain.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

from noeta.core.fold import fold
from noeta.policies.control_semantics import SPAWN_SUBAGENT_TOOL
from noeta.protocols.events import ModelBoundPayload, TaskCreatedPayload
from noeta.protocols.messages import (
    LLMResponse,
    TextBlock,
    ToolUseBlock,
    Usage,
)
from noeta.runtime.shell_policy import ShellMode
from noeta.runtime.worker import run_leased_task
from noeta.runtime.workspace import FsWriteMode
from noeta.testing.fake_llm import FakeLLMProvider

from tests._sdk_session import (
    default_coding_budget,
    make_driver,
    make_host,
    make_registry,
    preset_spec,
    runner_main_spec,
)

PARENT_GOAL = "parent-goal: delegate and report"
CHILD_GOAL = "child-goal: scout the tree and answer"
SESSION_MODEL = "session-model"
HOST_MODEL = "gpt-test"


def _spawn(agent: str) -> LLMResponse:
    return LLMResponse(
        stop_reason="tool_use",
        content=[
            ToolUseBlock(
                call_id="spawn-1",
                tool_name=SPAWN_SUBAGENT_TOOL,
                arguments={"agent": agent, "goal": CHILD_GOAL},
            )
        ],
        usage=Usage(uncached=1, output=1),
        raw={"id": "spawn-1"},
    )


def _end(text: str) -> LLMResponse:
    return LLMResponse(
        stop_reason="end_turn",
        content=[TextBlock(text=text)],
        usage=Usage(uncached=1, output=1),
        raw={"id": "end-" + text},
    )


def _host(ws: Path, provider: FakeLLMProvider, *child_specs: Any):
    """A resident host whose ``main`` may spawn each child spec by name."""
    names = tuple(spec.name for spec in child_specs)
    main = runner_main_spec("main", delegation=True, spawnable=names)
    host = make_host(
        make_registry(main, *child_specs),
        workspace_dir=ws,
        provider=provider,
        model=HOST_MODEL,
        multi_turn=True,
        write_mode=FsWriteMode.APPLY,
        shell_mode=ShellMode.OFF,
        budget=default_coding_budget(),
    )
    driver = make_driver(host, model_allowlist=frozenset({SESSION_MODEL}))
    return host, driver


def _steal_children_at_enqueue(host: Any, stolen: list[Any]) -> None:
    """Interpose an untargeted poll at the exact point a child is enqueued.

    A real idle resident worker hits this window on a millisecond race (the
    observer enqueues the child inside the parent's ``TaskCreated`` append;
    the drain's targeted lease comes only after the parent's step settles);
    polling from inside ``enqueue`` makes the same interleaving
    deterministic. Only children are stolen — the root's own enqueue is left
    to the driver.
    """
    dispatcher = host.dispatcher
    real_enqueue = dispatcher.enqueue

    def enqueue_then_poll(
        task_id: str,
        *,
        reserved: bool = False,
        queue: str | None = None,
        parent_task_id: str | None = None,
    ) -> None:
        real_enqueue(
            task_id,
            reserved=reserved,
            queue=queue,
            parent_task_id=parent_task_id,
        )
        if parent_task_id is None:
            return
        thief = dispatcher.lease(
            worker_id="thief", lease_seconds=30.0, task_id=None
        )
        if thief is not None:
            stolen.append(thief)

    dispatcher.enqueue = enqueue_then_poll  # type: ignore[method-assign]


def _spawned_child_id(host: Any, parent_id: str) -> str:
    return next(
        e.payload.subtask_id
        for e in host.event_log.read(parent_id)
        if e.type == "SubtaskSpawned"
    )


def _engine_model(engine: Any) -> str:
    """The model the Engine's (possibly wrapped) ReAct policy sends."""
    policy = engine._policy
    while not hasattr(policy, "_model"):
        policy = policy._inner
    return str(policy._model)


def _tool_name(tool: dict[str, Any]) -> str:
    """A recorded tool schema's name — Anthropic-shaped (``name``) or
    OpenAI-function-shaped (``function.name``)."""
    return str(tool.get("name") or tool["function"]["name"])


def _child_shape(host: Any, provider: FakeLLMProvider, child_id: str) -> dict[str, Any]:
    """Everything about a child's opening that must not depend on who
    claimed it: its recorded event shape, its opening binding, the residents
    its packs recorded, and the model / effort / tool surface of its first
    request."""
    events = host.event_log.read(child_id)
    # [0] the parent's spawn turn, [1] the child's first (and only) turn.
    child_req = provider.received_requests[1]
    return {
        "types": [e.type for e in events],
        "model_bound": [
            (e.payload.model, e.payload.principal_identity, e.payload.provider)
            for e in events
            if e.type == "ModelBound"
        ],
        "resident_actors": sorted(
            {e.actor for e in events if e.type == "ContextContentRecorded"}
        ),
        "req_model": child_req.model,
        "req_effort": child_req.effort,
        "tools": sorted(_tool_name(t) for t in child_req.tools),
    }


def _run(tmp_path: Path, *, stolen: bool, **start_kwargs: Any) -> tuple[Any, dict[str, Any]]:
    """Drive the spawn script once — the child claimed by a thief's untargeted
    poll (``stolen=True``) or by the driver's own drain — and return the host
    plus the child's shape."""
    ws = tmp_path / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    provider = FakeLLMProvider(
        responses=[_spawn("explore"), _end("child-done"), _end("parent-done")]
    )
    host, driver = _host(ws, provider, preset_spec("explore"))
    thieves: list[Any] = []
    if stolen:
        _steal_children_at_enqueue(host, thieves)

    out = driver.start(goal=PARENT_GOAL, agent="main", **start_kwargs)
    assert out.status == "suspended"

    if stolen:
        assert len(thieves) == 1, "the interposed poll did not claim the child"
        child_id = thieves[0].task_id
        assert child_id == _spawned_child_id(host, out.task_id)
        # The drain found the child already leased and degraded; the thief
        # drives it through the resident-worker step primitive.
        assert run_leased_task(host, thieves[0]) == "drained"
    else:
        child_id = _spawned_child_id(host, out.task_id)

    child = fold(host.event_log, host.content_store, child_id)
    assert child.status == "terminal", [e.type for e in host.event_log.read(child_id)]
    return host, _child_shape(host, provider, child_id)


def test_worker_claimed_child_is_recorded_like_a_drain_driven_child(
    tmp_path: Path,
) -> None:
    """The stolen child's recording and first request are shape-identical to
    the drain's: same event sequence, same opening binding, same residents,
    same model / effort / tools."""
    _, drained = _run(
        tmp_path / "drain", stolen=False,
        model_selector=SESSION_MODEL, effort="xhigh",
    )
    _, stolen = _run(
        tmp_path / "stolen", stolen=True,
        model_selector=SESSION_MODEL, effort="xhigh",
    )
    assert stolen == drained
    # ... and that shape is the inherited one, not the host defaults.
    assert stolen["model_bound"] == [(SESSION_MODEL, "inherited", None)]
    assert stolen["req_model"] == SESSION_MODEL
    assert stolen["req_effort"] == "xhigh"
    # the pre-loop residents were activated (the drain's ``run_content_init``)
    assert stolen["resident_actors"], stolen["types"]
    assert "ContextContentRecorded" in stolen["types"]
    # the explore leaf activates no delegation of its own, so it carries no
    # spawn tool — under the worker exactly as under the drain
    assert SPAWN_SUBAGENT_TOOL not in stolen["tools"]


def test_worker_claimed_child_on_a_default_bound_root_stays_unbound(
    tmp_path: Path,
) -> None:
    """A root on the host-default model keeps its children unbound (no
    ``ModelBound``), on the worker path exactly as under the drain."""
    _, drained = _run(tmp_path / "drain", stolen=False)
    _, stolen = _run(tmp_path / "stolen", stolen=True)
    assert stolen == drained
    assert stolen["model_bound"] == []
    assert stolen["req_model"] == HOST_MODEL


def test_worker_claimed_child_declared_default_beats_the_inherited_binding(
    tmp_path: Path,
) -> None:
    """A child agent's own declared default model wins over the root's
    session binding on the worker path too (identity ``"agent-default"``)."""
    ws = tmp_path / "ws"
    ws.mkdir()
    scout = dataclasses.replace(
        preset_spec("explore"), name="scout", default_model="haiku-test"
    )
    provider = FakeLLMProvider(
        responses=[_spawn("scout"), _end("child-done"), _end("parent-done")]
    )
    host, driver = _host(ws, provider, scout)
    thieves: list[Any] = []
    _steal_children_at_enqueue(host, thieves)

    out = driver.start(
        goal=PARENT_GOAL, agent="main", model_selector=SESSION_MODEL
    )
    assert out.status == "suspended"
    assert len(thieves) == 1
    assert run_leased_task(host, thieves[0]) == "drained"

    shape = _child_shape(host, provider, thieves[0].task_id)
    assert shape["model_bound"] == [("haiku-test", "agent-default", None)]
    assert shape["req_model"] == "haiku-test"


def test_root_cancel_abandons_a_worker_claimed_child(tmp_path: Path) -> None:
    """A cancel on the ROOT reaches a child a resident worker already claimed.

    ``cancel`` marks the tree's root in the process-local registry and nothing
    walks it downward, so a predicate bound to the claimed child's own id never
    trips: the stolen child would run its whole turn out — spending an LLM
    round and applying whatever its tools do — and only then hand a result to
    an already-terminal parent that drops it. Bound to the root (what the
    in-request drain has always done), it abandons at its first step boundary.
    """
    ws = tmp_path / "ws"
    ws.mkdir()
    provider = FakeLLMProvider(
        responses=[_spawn("explore"), _end("child-done"), _end("parent-done")]
    )
    host, driver = _host(ws, provider, preset_spec("explore"))
    thieves: list[Any] = []
    _steal_children_at_enqueue(host, thieves)

    out = driver.start(goal=PARENT_GOAL, agent="main")
    assert out.status == "suspended"
    assert len(thieves) == 1, "the interposed poll did not claim the child"
    child_id = thieves[0].task_id
    assert child_id == _spawned_child_id(host, out.task_id)

    # The human cancels the conversation while the pool holds the child's
    # lease. Only the root is marked — the child is not, and never will be.
    driver.cancel(out.task_id)
    assert host.is_cancelled(out.task_id) is True
    assert host.is_cancelled(child_id) is False

    assert run_leased_task(host, thieves[0]) == "cancelled"
    child_types = [e.type for e in host.event_log.read(child_id)]
    assert "TaskCompleted" not in child_types
    assert "LLMRequestStarted" not in child_types  # no round was spent
    # Only the parent's own spawn turn ever reached the provider.
    assert len(provider.received_requests) == 1


def test_resolve_engine_inherits_from_the_root_through_an_unbound_middle_child(
    tmp_path: Path,
) -> None:
    """Inheritance reads the delegation tree's ROOT, not the direct parent: a
    depth-2 child whose parent is itself an (unbound) subtask still resolves on
    the root's bound model. Recordings emitted by hand, the way a resident
    worker finds them when it claims the grandchild."""
    ws = tmp_path / "ws"
    ws.mkdir()
    host, _driver = _host(ws, FakeLLMProvider(responses=[]), preset_spec("explore"))
    host.event_log.emit(
        task_id="root",
        type="TaskCreated",
        payload=TaskCreatedPayload(goal="root", policy_name="react", agent_name="main"),
    )
    host.event_log.emit(
        task_id="root",
        type="ModelBound",
        payload=ModelBoundPayload(model=SESSION_MODEL, principal_identity="local"),
    )
    host.event_log.emit(
        task_id="mid",
        type="TaskCreated",
        payload=TaskCreatedPayload(
            goal="mid", policy_name="react", agent_name="explore",
            parent_task_id="root", subtask_depth=1,
        ),
    )
    host.event_log.emit(
        task_id="leaf",
        type="TaskCreated",
        payload=TaskCreatedPayload(
            goal="leaf", policy_name="react", agent_name="explore",
            parent_task_id="mid", subtask_depth=2,
        ),
    )

    engine = host.resolve_engine(fold(host.event_log, host.content_store, "leaf"))

    # The built Engine's policy runs on the root's bound model.
    assert _engine_model(engine) == SESSION_MODEL
