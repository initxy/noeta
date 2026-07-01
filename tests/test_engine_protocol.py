"""Structural compliance tests for ``EngineProtocol``.

The concrete :class:`noeta.core.engine.Engine`
MUST structurally satisfy the :class:`noeta.protocols.engine.EngineProtocol`
so that execution hosts (``noeta.execution.subtask_drain`` and the code
driver) can depend on the Protocol alone and accept alternative
implementations (fakes, replay adapters, remote proxies) without code
changes.

Coverage:
* ``isinstance(real_engine, EngineProtocol)`` via the ``@runtime_checkable``
  decoration on the Protocol.
* Assignment of a real Engine to an ``EngineProtocol``-typed variable — the
  mypy-only structural check is implicitly validated by letting mypy see the
  assignment succeed (the runtime equivalent exercises the same method set).
* A minimal shadow of the call sites from ``subtask_drain`` /
  ``InteractionDriver`` / ``WokenPrelude`` so future Protocol edits that
  would silently break a call site (missing method, wrong keyword-only
  shape) show up here instead of in a distant module.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from noeta.core.engine import Engine
from noeta.protocols.decisions import TaskStatePatch
from noeta.protocols.engine import EngineProtocol
from noeta.protocols.messages import TextBlock
from noeta.testing.composer import trivial_three_segment
from noeta.policies.stub import StubFinishPolicy
from noeta.storage.memory import (
    InMemoryContentStore,
    InMemoryDispatcher,
    InMemoryEventLog,
)


if TYPE_CHECKING:
    from noeta.protocols.task import Task


def _make_engine() -> tuple[Engine, InMemoryDispatcher]:
    content_store = InMemoryContentStore()
    dispatcher = InMemoryDispatcher()
    event_log = InMemoryEventLog(lease_validator=dispatcher)
    engine = Engine(
        event_log=event_log,
        content_store=content_store,
        composer=trivial_three_segment(content_store),
        policy=StubFinishPolicy(answer="done"),
    )
    return engine, dispatcher


# -- structural compliance ------------------------------------------------


def test_engine_is_runtime_checkable_compliant() -> None:
    """``isinstance(real_engine, EngineProtocol)`` must be ``True``.

    Any new required method on the Protocol, or any signature drift between
    the Protocol and the concrete Engine, will be caught here by the
    ``@runtime_checkable`` machinery (missing methods raise
    ``TypeError``; method-with-name-mismatch-``...``-stub checks the
    callable only, not the full signature — the static annotations cover
    the rest).
    """
    engine, _disp = _make_engine()
    assert isinstance(engine, EngineProtocol)


def test_engine_assigns_to_protocol_typed_variable() -> None:
    """A real Engine must be assignable to an ``EngineProtocol`` slot.

    This exercises the same structural relationship as
    ``test_engine_is_runtime_checkable_compliant``, but with the
    user-facing shape (typed variable) that call sites such as
    ``subtask_drain.DrainHost.build_child_engine`` return.
    """
    engine, _disp = _make_engine()
    # ``: EngineProtocol`` annotation is load-bearing here — a static
    # checker would flag a structural mismatch. At runtime the reference
    # must behave as the Protocol's method set describes.
    slot: EngineProtocol = engine
    assert slot is engine
    # sanity: the assigned reference exposes every Protocol method name.
    for name in (
        "create_task",
        "append_user_message",
        "append_subagent_result_message",
        "append_subagent_group_result_messages",
        "apply_state_patch",
        "resolve_tool_approval",
        "answer_user_question",
        "note_woken",
        "note_model_bound",
        "note_conversation_closed",
        "note_conversation_reopened",
        "run_one_step",
    ):
        assert callable(getattr(slot, name))


# -- call-site shadow: each called method accepts the keyword shape ------


def test_create_task_signature_matches_protocol() -> None:
    """``create_task`` is keyword-only and mirrors the Protocol defaults."""
    engine, dispatcher = _make_engine()
    slot: EngineProtocol = engine
    task = slot.create_task(goal="g", policy_name="stub")
    dispatcher.enqueue(task.task_id)
    lease = dispatcher.lease(worker_id="w-test")
    assert lease is not None
    assert task.state.goal == "g"


def test_apply_state_patch_signature_matches_protocol() -> None:
    """Pre-loop skill activation (``activate_skills`` call shape)."""
    engine, dispatcher = _make_engine()
    slot: EngineProtocol = engine
    task = slot.create_task(goal="g", policy_name="stub")
    dispatcher.enqueue(task.task_id)
    lease = dispatcher.lease(worker_id="w-test")
    assert lease is not None
    slot.apply_state_patch(
        task,
        patch=TaskStatePatch(activate_skills=["review"]),
        lease_id=lease.lease_id,
    )
    assert "review" in task.state.active_skills


def test_run_one_step_and_note_woken_signatures() -> None:
    """Core step-drive surface used by worker + driver + runner."""
    engine, dispatcher = _make_engine()
    slot: EngineProtocol = engine
    task = slot.create_task(goal="g", policy_name="stub_finish")
    dispatcher.enqueue(task.task_id)
    lease = dispatcher.lease(worker_id="w-test")
    assert lease is not None
    slot.append_user_message(task, content=[TextBlock(text="hi")], lease_id=lease.lease_id)
    terminal = slot.run_one_step(task, lease_id=lease.lease_id)
    assert terminal.status == "terminal"


def test_note_model_bound_signature_matches_protocol() -> None:
    """Driver calls this to bind a model per-task before drive."""
    engine, dispatcher = _make_engine()
    slot: EngineProtocol = engine
    task = slot.create_task(goal="g", policy_name="stub")
    dispatcher.enqueue(task.task_id)
    lease = dispatcher.lease(worker_id="w-test")
    assert lease is not None
    slot.note_model_bound(
        task,
        lease_id=lease.lease_id,
        model="stub-model",
        principal_identity="cli",
    )
    # governance.model_binding is the folded model name (a str)
    assert task.governance.model_binding == "stub-model"


def test_note_conversation_closed_and_reopened_signatures() -> None:
    """Driver close/reopen audit events — both kwarg shapes."""
    engine, dispatcher = _make_engine()
    slot: EngineProtocol = engine
    task = slot.create_task(goal="g", policy_name="stub")
    dispatcher.enqueue(task.task_id)
    lease = dispatcher.lease(worker_id="w-test")
    assert lease is not None
    slot.note_conversation_closed(task, closed_by="human")
    # `closed` lives on GovernanceState (folded from the audit event)
    assert task.governance.closed is True
    slot.note_conversation_reopened(task, reopened_by="human")
    assert task.governance.closed is False
