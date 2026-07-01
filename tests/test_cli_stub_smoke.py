"""End-to-end smoke test against the stub provider.

This is the Phase 2 Scenario A acceptance check: a runtime wired with
the deterministic :class:`StubProvider` runs a single task to terminal
status with no API key, no network, and a deterministic two-turn LLM
response sequence.

The old form drove this through ``noeta run --provider stub`` (operator
CLI). The library-SDK refactor removed that command suite; the *behaviour* under
test — the stub double driving the ReAct loop's tool-call + finish
branches to a terminal task — lives in the cli-free assembly seam
:func:`noeta.testing.profile.build_runtime` plus
:class:`noeta.testing.stub_provider.StubProvider`, so this test is
retargeted there and the assertions (terminal status, ``task-`` id
prefix, recorded events, no-key path) are preserved.
"""

from __future__ import annotations

import pytest

from noeta.protocols.messages import TextBlock
from noeta.protocols.task import Task
from noeta.testing.profile import (
    build_runtime,
    default_budget,
    permission_policy_for,
    resolve_tool_pack,
)
from noeta.testing.stub_provider import StubProvider


def _drive_one_task_to_settle() -> Task:
    """Assemble a stub-provider runtime, create one task, and drive it
    through a single lease window — the cli-free equivalent of the old
    ``noeta run --provider stub --goal smoke`` golden path."""
    tools, allowed_tools = resolve_tool_pack("none")
    bundle = build_runtime(
        provider=StubProvider(),
        model="stub-model",
        system_prompt="You are a helpful assistant.",
        tools=tools,
        sqlite_path=":memory:",
        sse_broadcaster=None,
        max_steps=5,
        permission_policy=permission_policy_for(allowed_tools),
        budget=default_budget(),
    )
    try:
        task = bundle.engine.create_task(goal="smoke", policy_name="react")
        bundle.dispatcher.enqueue(task.task_id)
        lease = bundle.dispatcher.lease(worker_id="smoke-run", lease_seconds=600.0)
        assert lease is not None
        bundle.engine.append_user_message(
            task, content=[TextBlock(text="smoke")], lease_id=lease.lease_id
        )
        task = bundle.engine.run_one_step(task, lease_id=lease.lease_id)
        bundle.dispatcher.release(lease.lease_id, next_state=task.status)
        # Read events before shutdown so the assertion sees the recording.
        events = len(bundle.event_log.read(task.task_id))
    finally:
        bundle.shutdown()
    assert events > 0
    return task


def test_stub_provider_run_reaches_terminal() -> None:
    task = _drive_one_task_to_settle()
    assert task.status == "terminal"
    assert task.task_id.startswith("task-")


def test_stub_provider_does_not_require_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: the stub double must reach terminal with no provider
    key or network. The old operator CLI guarded this with a
    ``missing --api-key`` SystemExit branch in ``build_provider``; that
    command is gone, but the underlying guarantee — ``StubProvider``
    needs no env, key, or network — is preserved here by driving the run
    to terminal with the key env vars cleared."""
    monkeypatch.delenv("NOETA_API_KEY", raising=False)
    monkeypatch.delenv("NOETA_OPENAI_API_KEY", raising=False)

    task = _drive_one_task_to_settle()
    assert task.status == "terminal"
