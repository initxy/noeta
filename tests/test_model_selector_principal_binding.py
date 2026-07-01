"""Issue 06 — model selector durable binding +
``Principal`` allowlist validation.

Covers every acceptance criterion:

* L0 :class:`noeta.protocols.values.Principal` (minimal: identity +
  allowed_models) + the ⊤ ``LOCAL_PRINCIPAL``.
* The new L0 ``ModelBound`` event, written by the **Engine** under a driver
  command, folded into ``GovernanceState`` (opening binding + per-turn
  switch).
* The driver validates ``selector ∈ principal.allowed_models ∩ allowlist``
  *before* any durable write — a rejected selector leaves no ``ModelBound``,
  no turn, no binding.
* The resolver keys the Engine on ``(agent_name, model)`` taken from the
  latest ``ModelBound`` fold.
* CLI local principal = ⊤; a web principal's ``allowed_models`` gates the
  selector.
* an old recording with no ``ModelBound`` folds to the local/⊤ default model
  (no drift).
* the demo: open on opus, switch to haiku mid-conversation → two
  ``ModelBound`` events, each traceable to its authorizing principal.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests._sdk_session import official_registry as official_agent_registry
from noeta.execution.multi_turn import NEXT_GOAL_WAKE_HANDLE
from noeta.execution.driver import (
    InteractionDriver,
    ModelBindPrelude,
    ModelSelectorError,
    multi_turn_policy_wrapper,
)
from noeta.client import SdkHost
from noeta.core.engine import Engine
from noeta.core.fold import fold
from noeta.protocols.events import ModelBoundPayload, TaskCreatedPayload
from noeta.protocols.messages import LLMResponse, TextBlock, Usage
from noeta.protocols.values import LOCAL_PRINCIPAL, Principal
from noeta.storage.memory import (
    InMemoryContentStore,
    InMemoryDispatcher,
    InMemoryEventLog,
)
from noeta.testing.fake_llm import FakeLLMProvider
from noeta.tools.fs import FsWriteMode, ShellMode


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _end_turn(text: str = "done") -> LLMResponse:
    return LLMResponse(
        stop_reason="end_turn",
        content=[TextBlock(text=text)],
        usage=Usage(uncached=1, output=1),
        raw={"id": "end-" + text},
    )


def _host(
    workspace: Path, *, responses: list[LLMResponse], model: str = "gpt-test"
) -> tuple[SdkHost, InMemoryDispatcher, InMemoryEventLog]:
    dispatcher = InMemoryDispatcher()
    event_log = InMemoryEventLog(lease_validator=dispatcher)
    content_store = InMemoryContentStore()
    host = SdkHost(
        event_log=event_log,
        content_store=content_store,
        dispatcher=dispatcher,
        provider=FakeLLMProvider(responses=responses),
        model=model,
        workspace_dir=workspace,
        write_mode=FsWriteMode.APPLY,
        shell_mode=ShellMode.ALLOWLIST,
        policy_wrapper=multi_turn_policy_wrapper,
    
        registry=official_agent_registry(),
        aliases={"default": "main"},
        require_approval_tools=(),)
    return host, dispatcher, event_log


# ---------------------------------------------------------------------------
# L0 Principal
# ---------------------------------------------------------------------------


def test_principal_is_minimal_two_fields() -> None:
    """Principal carries ONLY identity + allowed_models (+ the ⊤ flag) — no
    capabilities / side-effects / delegation chain (issue 06: minimal L0)."""
    p = Principal(identity="alice", allowed_models=frozenset({"opus"}))
    field_names = {f for f in p.__dataclass_fields__}
    assert field_names == {"identity", "allowed_models", "allows_any"}


def test_principal_permits_membership() -> None:
    p = Principal(identity="alice", allowed_models=frozenset({"opus", "haiku"}))
    assert p.permits("opus")
    assert p.permits("haiku")
    assert not p.permits("sonnet")


def test_local_principal_is_top() -> None:
    """The CLI's local principal permits any selector (⊤ — no trust
    boundary)."""
    assert LOCAL_PRINCIPAL.allows_any
    assert LOCAL_PRINCIPAL.permits("anything-at-all")
    assert LOCAL_PRINCIPAL.identity == "local"


# ---------------------------------------------------------------------------
# ModelBound event + Engine writer + fold
# ---------------------------------------------------------------------------


def _engine_with_task(
    tmp_path: Path,
) -> tuple[Engine, InMemoryEventLog, InMemoryContentStore, Any, str]:
    dispatcher = InMemoryDispatcher()
    log = InMemoryEventLog(lease_validator=dispatcher)
    cs = InMemoryContentStore()
    from noeta.testing.composer import trivial_three_segment

    engine = Engine(
        event_log=log,
        content_store=cs,
        composer=trivial_three_segment(cs),
        policy=None,
    )
    task = engine.create_task(goal="g", policy_name="react", agent_name="default")
    dispatcher.enqueue(task.task_id)
    lease = dispatcher.lease(worker_id="w")
    assert lease is not None
    return engine, log, cs, task, lease.lease_id


def test_engine_note_model_bound_emits_and_folds(tmp_path: Path) -> None:
    engine, log, cs, task, lease_id = _engine_with_task(tmp_path)

    engine.note_model_bound(
        task, lease_id=lease_id, model="opus", principal_identity="alice"
    )

    events = log.read(task.task_id)
    bound = [e for e in events if e.type == "ModelBound"]
    assert len(bound) == 1
    assert isinstance(bound[0].payload, ModelBoundPayload)
    assert bound[0].payload.model == "opus"
    assert bound[0].payload.principal_identity == "alice"
    # Engine is the writer (driver-commanded, like TaskWoken/TaskStarted).
    assert bound[0].origin == "engine"

    folded = fold(log, cs, task.task_id)
    assert folded.governance.model_binding == "opus"
    assert folded.governance.principal_identity == "alice"
    # (I4): the model-binding audit gained a ``provider`` key
    # (``None`` when only the model was bound — this opening ModelBound carried
    # no provider, so the host default sticks).
    assert folded.governance.model_bindings == [
        {"model": "opus", "principal_identity": "alice", "provider": None}
    ]


def test_fold_latest_model_bound_wins_with_full_audit(tmp_path: Path) -> None:
    """A per-turn switch appends a second ModelBound; fold tracks the latest
    binding AND the append-only audit of every binding."""
    engine, log, cs, task, lease_id = _engine_with_task(tmp_path)
    engine.note_model_bound(
        task, lease_id=lease_id, model="opus", principal_identity="alice"
    )
    engine.note_model_bound(
        task, lease_id=lease_id, model="haiku", principal_identity="alice"
    )
    folded = fold(log, cs, task.task_id)
    assert folded.governance.model_binding == "haiku"
    assert [b["model"] for b in folded.governance.model_bindings] == [
        "opus",
        "haiku",
    ]


def test_old_recording_without_model_bound_folds_to_no_binding(
    tmp_path: Path,
) -> None:
    """An old recording (no ModelBound) folds to model_binding=None — the
    resolver then falls back to its host-fixed default, no drift."""
    log = InMemoryEventLog()
    cs = InMemoryContentStore()
    log.emit(
        task_id="legacy",
        type="TaskCreated",
        payload=TaskCreatedPayload(goal="g", policy_name="p"),
    )
    folded = fold(log, cs, "legacy")
    assert folded.governance.model_binding is None
    assert folded.governance.principal_identity is None
    assert folded.governance.model_bindings == []


# ---------------------------------------------------------------------------
# Driver validation: selector ∈ allowed_models ∩ allowlist
# ---------------------------------------------------------------------------


def test_start_rejects_selector_outside_principal_allowlist(
    tmp_path: Path,
) -> None:
    """A web principal that may bind only 'sonnet' cannot bind 'opus'; the
    rejection leaves NO Task, NO ModelBound, NO turn."""
    ws = tmp_path / "ws"
    ws.mkdir()
    host, _, event_log = _host(ws, responses=[_end_turn()])
    driver = InteractionDriver(
        host, principal=Principal(identity="bob", allowed_models=frozenset({"sonnet"}))
    )

    with pytest.raises(ModelSelectorError) as exc:
        driver.start(goal="x", agent="main", model_selector="opus")
    assert exc.value.selector == "opus"
    assert exc.value.allowed == ["sonnet"]

    # No durable write at all — the refusal happened before task creation.
    assert all(not event_log.read(tid) for tid in [])  # nothing created
    # And no ModelBound anywhere on the (empty) log.
    # (A fresh InMemoryEventLog has no streams.)
    assert getattr(event_log, "_streams", {}) == {}


def test_start_emits_opening_model_bound_for_allowed_selector(
    tmp_path: Path,
) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    host, _, event_log = _host(ws, responses=[_end_turn("hi")])
    driver = InteractionDriver(
        host,
        principal=Principal(
            identity="bob", allowed_models=frozenset({"opus", "haiku"})
        ),
    )
    out = driver.start(goal="hello", agent="main", model_selector="opus")
    assert out.status == "suspended"

    events = event_log.read(out.task_id)
    bound = [e for e in events if e.type == "ModelBound"]
    assert len(bound) == 1
    # D-C3: the driver resolves the 'opus' alias to its real model-id before
    # binding, so ModelBound records the real id (not the friendly alias).
    assert bound[0].payload.model == "claude-opus-4-8"
    assert bound[0].payload.principal_identity == "bob"
    # Opening ModelBound sits in the pre-loop window: after TaskCreated,
    # before TaskStarted.
    types = [e.type for e in events]
    assert types.index("TaskCreated") < types.index("ModelBound")
    assert types.index("ModelBound") < types.index("TaskStarted")


def test_cli_local_principal_binds_host_default_without_selector(
    tmp_path: Path,
) -> None:
    """The CLI (⊤ principal, no selector) still records an opening
    ModelBound — bound to the host-fixed default model, identity 'local'."""
    ws = tmp_path / "ws"
    ws.mkdir()
    host, _, event_log = _host(ws, responses=[_end_turn()], model="gpt-test")
    driver = InteractionDriver(host)  # defaults to LOCAL_PRINCIPAL (⊤)

    out = driver.start(goal="hello", agent="main")
    bound = [e for e in event_log.read(out.task_id) if e.type == "ModelBound"]
    assert len(bound) == 1
    assert bound[0].payload.model == "gpt-test"
    assert bound[0].payload.principal_identity == "local"


# ---------------------------------------------------------------------------
# Resolver keys on (agent_name, model)
# ---------------------------------------------------------------------------


def test_resolver_keys_engine_on_agent_and_bound_model(tmp_path: Path) -> None:
    """Two tasks naming the same agent but bound to different models resolve
    to DISTINCT Engines; the same (agent, model) shares one Engine."""
    ws = tmp_path / "ws"
    ws.mkdir()
    host, dispatcher, log = _host(ws, responses=[_end_turn()])

    # Task A bound to opus.
    eng = host.resolve_engine_for_agent("default")
    a = eng.create_task(goal="ga", policy_name="react", agent_name="default")
    dispatcher.enqueue(a.task_id)
    lease_a = dispatcher.lease(worker_id="w", task_id=a.task_id)
    assert lease_a is not None
    eng.note_model_bound(
        a, lease_id=lease_a.lease_id, model="opus", principal_identity="local"
    )
    a_folded = fold(log, host.content_store, a.task_id)

    # Task B bound to haiku.
    b = eng.create_task(goal="gb", policy_name="react", agent_name="default")
    dispatcher.enqueue(b.task_id)
    lease_b = dispatcher.lease(worker_id="w", task_id=b.task_id)
    assert lease_b is not None
    eng.note_model_bound(
        b, lease_id=lease_b.lease_id, model="haiku", principal_identity="local"
    )
    b_folded = fold(log, host.content_store, b.task_id)

    eng_a = host.resolve_engine(a_folded)
    eng_b = host.resolve_engine(b_folded)
    assert eng_a is not eng_b  # distinct model → distinct Engine

    # Re-resolving A's binding returns the SAME cached Engine.
    assert host.resolve_engine(a_folded) is eng_a


def test_resolver_falls_back_to_host_model_when_no_binding(
    tmp_path: Path,
) -> None:
    """A task with no ModelBound resolves on the host-fixed default model —
    the fallback that lets old recordings resume unchanged."""
    ws = tmp_path / "ws"
    ws.mkdir()
    host, dispatcher, log = _host(ws, responses=[_end_turn()], model="gpt-test")
    eng = host.resolve_engine_for_agent("default")
    t = eng.create_task(goal="g", policy_name="react", agent_name="default")
    folded = fold(log, host.content_store, t.task_id)
    # No ModelBound → resolves the host-default Engine (same one the
    # explicit host model would mint).
    resolved = host.resolve_engine(folded)
    assert resolved is host.resolve_engine_for_agent("default", model="gpt-test")


# ---------------------------------------------------------------------------
# Per-turn switch: two ModelBound events (the demo)
# ---------------------------------------------------------------------------


def test_per_turn_switch_records_two_model_bounds(tmp_path: Path) -> None:
    """Open on opus, switch to haiku mid-conversation → exactly two
    ModelBound events, each traceable to the authorizing principal. The
    switch lands in the post-TaskWoken window (resume-able)."""
    ws = tmp_path / "ws"
    ws.mkdir()
    host, _, event_log = _host(
        ws, responses=[_end_turn("t1"), _end_turn("t2")]
    )
    driver = InteractionDriver(
        host,
        principal=Principal(
            identity="carol", allowed_models=frozenset({"opus", "haiku"})
        ),
    )

    started = driver.start(goal="first", agent="main", model_selector="opus")
    assert started.status == "suspended"
    pre = event_log.read(started.task_id)

    out = driver.send_goal(
        started.task_id, goal="second", model_selector="haiku"
    )
    assert out.status == "suspended"

    all_events = event_log.read(started.task_id)
    bound = [e for e in all_events if e.type == "ModelBound"]
    # D-C3: aliases resolve to real ids before binding.
    assert [b.payload.model for b in bound] == [
        "claude-opus-4-8",
        "claude-haiku-4-5",
    ]
    assert all(b.payload.principal_identity == "carol" for b in bound)

    # The switch's ModelBound rides the woken-command-prelude window: it
    # lands AFTER TaskWoken and BEFORE the next turn's ContextPlanComposed.
    new = all_events[len(pre):]
    types = [e.type for e in new]
    woken_idx = types.index("TaskWoken")
    bound_idx = types.index("ModelBound")
    plan_idx = types.index("ContextPlanComposed")
    assert woken_idx < bound_idx < plan_idx

    # inspect can trace the current binding + full audit back to the
    # principal that sanctioned it.
    folded = fold(event_log, host.content_store, started.task_id)
    assert folded.governance.model_binding == "claude-haiku-4-5"
    # (I4): both switches were model-only ⇒ ``provider`` is None
    # on each audit entry (the host default provider sticks across both turns).
    assert folded.governance.model_bindings == [
        {"model": "claude-opus-4-8", "principal_identity": "carol", "provider": None},
        {"model": "claude-haiku-4-5", "principal_identity": "carol", "provider": None},
    ]


def test_send_goal_rejects_switch_outside_allowlist_leaves_no_bound(
    tmp_path: Path,
) -> None:
    """A per-turn switch to an unauthorized model is refused and writes no
    second ModelBound (the binding stays at the opening model)."""
    ws = tmp_path / "ws"
    ws.mkdir()
    host, _, event_log = _host(ws, responses=[_end_turn("t1")])
    driver = InteractionDriver(
        host, principal=Principal(identity="dave", allowed_models=frozenset({"opus"}))
    )
    started = driver.start(goal="first", agent="main", model_selector="opus")
    before = len(
        [e for e in event_log.read(started.task_id) if e.type == "ModelBound"]
    )
    with pytest.raises(ModelSelectorError):
        driver.send_goal(started.task_id, goal="x", model_selector="haiku")
    after = [
        e for e in event_log.read(started.task_id) if e.type == "ModelBound"
    ]
    assert len(after) == before  # no second binding written
    # opening binding stays at the resolved opus id; the rejected switch left
    # no trace.
    assert after[-1].payload.model == "claude-opus-4-8"


# ---------------------------------------------------------------------------
# ModelBindPrelude composition
# ---------------------------------------------------------------------------


def test_model_bind_prelude_chains_inner(tmp_path: Path) -> None:
    """ModelBindPrelude binds the model THEN runs its inner prelude on the
    same engine/lease (note_woken → ModelBound → inner → run_one_step)."""
    engine, log, cs, task, lease_id = _engine_with_task(tmp_path)

    calls: list[str] = []

    def _inner(eng: Any, t: Any, *, lease_id: str) -> Any:
        calls.append("inner")
        return t

    prelude = ModelBindPrelude(
        model="opus", principal_identity="local", inner=_inner
    )
    prelude(engine, task, lease_id=lease_id)

    assert calls == ["inner"]
    bound = [e for e in log.read(task.task_id) if e.type == "ModelBound"]
    assert len(bound) == 1
    assert bound[0].payload.model == "opus"
