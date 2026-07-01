"""Tests for the real-provider subtask demo.

The example script ``examples/_internal/real_provider_subtask_demo.py`` is the
headline demonstration of Noeta's value proposition — it composes two
Engines, drives a child task via a real LLM, and exercises the
wake-resume path.

Two test surfaces here:

1. **Smoke** — replicates the demo's golden flow with a deterministic
   scripted provider so CI exercises the relative-ordering invariants
   without an API key.
2. **Skip-when-env-missing** — verifies the example script exits 0
   with ``skipped: ...`` when no real provider is configured.

The real-provider branch of the demo is exercised manually by humans
running the example with their own API key; CI never burns provider
credit.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from noeta.testing.profile import default_budget, default_permission_policy
from noeta.context.composer import ThreeSegmentComposer
from noeta.core.engine import Engine
from noeta.core.fold import fold
from noeta.core.wiring import wire_default_observers
from noeta.guards.permission import PermissionPolicy
from noeta.policies.react import ReActPolicy
from noeta.policies.stub import StubScriptedPolicy
from noeta.protocols.decisions import FinishDecision, SpawnSubtaskDecision
from noeta.protocols.messages import (
    LLMRequest,
    LLMResponse,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
)
from noeta.protocols.wake import SubtaskCompleted, SubtaskResult
from noeta.runtime.llm import RuntimeLLMClient
from noeta.storage.memory import (
    InMemoryContentStore,
    InMemoryDispatcher,
    InMemoryEventLog,
)
from noeta.tools.fake import FakeTool


class _DeterministicProvider:
    """Stand-in for OpenAI/Anthropic in CI. **Stateless** — turn is
    decided by inspecting the request's message history for a prior
    ToolResultBlock. First turn → tool_use(echo); second turn (after
    tool result) → end_turn."""

    def complete(self, request: LLMRequest) -> LLMResponse:
        has_tool_result = any(
            isinstance(b, ToolResultBlock)
            for msg in request.messages
            for b in (msg.content or [])
        )
        if not has_tool_result:
            return LLMResponse(
                stop_reason="tool_use",
                content=[
                    ToolUseBlock(
                        call_id="demo-call-1",
                        tool_name="echo",
                        arguments={"text": "hello"},
                    )
                ],
                usage=Usage(uncached=1, output=1),
            )
        return LLMResponse(
            stop_reason="end_turn",
            content=[TextBlock(text="echo-said: hello")],
            usage=Usage(uncached=1, output=1),
        )


def _build_echo_tool() -> Any:
    return FakeTool(
        name="echo",
        script={("hello",): "echo-said: hello"},
        input_schema={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        },
    )


def test_demo_golden_path_invariants_with_deterministic_provider() -> None:
    """Drive the demo's parent+child flow with an in-process
    deterministic provider; assert the same relative-ordering
    invariants the example asserts in its teardown.
    """
    dispatcher = InMemoryDispatcher()
    event_log = InMemoryEventLog(lease_validator=dispatcher)
    content_store = InMemoryContentStore()
    wire_default_observers(event_log, dispatcher)

    tools = {"echo": _build_echo_tool()}
    system_prompt = (
        "You are a demo assistant. Use the echo tool then finish."
    )
    composer = ThreeSegmentComposer(
        system_prompt=system_prompt, tools=tools, content_store=content_store
    )

    # Permission policy must allow "helper" subtask agent.
    perm = PermissionPolicy(
        allowed_tools=frozenset({"echo"}),
        denied_tools=frozenset(),
        max_risk_level=None,
        allowed_subtask_agents=frozenset({"helper"}),
    )
    del perm  # demo doesn't wire HookManager in this minimal smoke; engine default OK

    scripted_parent = StubScriptedPolicy(
        [
            SpawnSubtaskDecision(
                agent_name="helper", goal="echo hello and finish"
            ),
            FinishDecision(answer="parent done"),
        ]
    )
    engine_parent = Engine(
        event_log=event_log,
        content_store=content_store,
        composer=composer,
        policy=scripted_parent,
        tools=tools,
    )

    provider = _DeterministicProvider()
    child_llm = RuntimeLLMClient(
        provider=provider, event_log=event_log, content_store=content_store
    )
    react_child = ReActPolicy(
        llm=child_llm,
        tools=tools,
        system_prompt=system_prompt,
        model="stub-model",
        max_steps=4,
    )
    engine_child = Engine(
        event_log=event_log,
        content_store=content_store,
        composer=composer,
        policy=react_child,
        tools=tools,
    )

    # ---- Parent step 1: spawn + suspend ----
    parent_task = engine_parent.create_task(
        goal="orchestrate", policy_name="scripted_demo"
    )
    dispatcher.enqueue(parent_task.task_id)
    parent_lease = dispatcher.lease(worker_id="p1", lease_seconds=60.0)
    assert parent_lease is not None
    engine_parent.append_user_message(
        parent_task, content=[TextBlock(text="please orchestrate")], lease_id=parent_lease.lease_id
    )
    parent_after = engine_parent.run_one_step(
        parent_task, lease_id=parent_lease.lease_id
    )
    dispatcher.release(
        parent_lease.lease_id,
        next_state=parent_after.status,
        wake_on=parent_after.wake_on,
    )
    assert parent_after.status == "suspended"
    assert isinstance(parent_after.wake_on, SubtaskCompleted)
    child_id = parent_after.wake_on.subtask_id

    # ---- Child step: ReAct with deterministic provider ----
    child_lease = dispatcher.lease(
        worker_id="c1", lease_seconds=60.0, task_id=child_id
    )
    assert child_lease is not None
    child_task = fold(event_log, content_store, child_id)
    child_after = engine_child.run_one_step(
        child_task, lease_id=child_lease.lease_id
    )
    dispatcher.release(child_lease.lease_id, next_state=child_after.status)
    assert child_after.status == "terminal"

    # ---- Parent step 2: wake-resume + finish ----
    parent_wake_lease = dispatcher.lease(
        worker_id="p2", lease_seconds=60.0, task_id=parent_task.task_id
    )
    assert parent_wake_lease is not None
    assert parent_wake_lease.wake_event is not None
    assert isinstance(parent_wake_lease.wake_event, SubtaskCompleted)
    assert parent_wake_lease.wake_event.subtask_id == child_id

    woken_parent = fold(event_log, content_store, parent_task.task_id)
    woken_parent = engine_parent.note_woken(
        woken_parent,
        lease_id=parent_wake_lease.lease_id,
        wake_event=parent_wake_lease.wake_event,
    )
    woken_parent = engine_parent.run_one_step(
        woken_parent, lease_id=parent_wake_lease.lease_id
    )
    dispatcher.release(
        parent_wake_lease.lease_id, next_state=woken_parent.status
    )
    assert woken_parent.status == "terminal"

    # ---- Assert the I1 invariants (parent + child) ----
    parent_envs = event_log.read(parent_task.task_id)
    parent_types = [e.type for e in parent_envs]
    assert parent_envs[0].type == "TaskCreated"
    assert parent_envs[-1].type == "TaskCompleted"

    def _idx(typ: str) -> int:
        for i, env in enumerate(parent_envs):
            if env.type == typ:
                return i
        raise AssertionError(f"missing {typ} in {parent_types}")

    assert _idx("SubtaskSpawned") < _idx("TaskSuspended") < _idx(
        "SubtaskCompleted"
    ) < _idx("TaskWoken")

    woken_env = parent_envs[_idx("TaskWoken")]
    assert isinstance(woken_env.payload.wake_event, SubtaskCompleted)
    assert woken_env.payload.wake_event.subtask_id == child_id
    assert isinstance(woken_env.payload.wake_event.result, SubtaskResult)
    assert woken_env.payload.wake_event.result.status == "completed"

    child_envs = event_log.read(child_id)
    child_types = [e.type for e in child_envs]
    assert child_envs[0].type == "TaskCreated"
    assert child_envs[0].payload.parent_task_id == parent_task.task_id
    assert child_envs[-1].type == "TaskCompleted"
    assert "LLMRequestStarted" in child_types
    assert "LLMResponseRecorded" in child_types
    assert "LLMRequestFinished" in child_types
    assert "ToolCallStarted" in child_types
    assert "ToolResultRecorded" in child_types
    assert "ToolCallFinished" in child_types


def test_example_anthropic_provider_carries_default_max_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B3 regression — Anthropic adapter fail-fasts when neither the
    request nor the provider carries ``max_tokens``. The demo docstring
    promises ``NOETA_PROVIDER=anthropic NOETA_API_KEY=... NOETA_MODEL=...``
    works without an extra env var, so ``_build_provider()`` must
    supply an explicit default (1024) when ``NOETA_MAX_TOKENS`` is unset.
    """
    import importlib.util

    example_path = (
        Path(__file__).resolve().parents[1]
        / "examples"
        / "_internal"
        / "real_provider_subtask_demo.py"
    )
    spec = importlib.util.spec_from_file_location("demo_example", example_path)
    assert spec is not None and spec.loader is not None
    demo_example = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(demo_example)

    monkeypatch.setenv("NOETA_PROVIDER", "anthropic")
    monkeypatch.setenv("NOETA_API_KEY", "dummy-key-for-test")
    monkeypatch.setenv("NOETA_MODEL", "claude-test")
    monkeypatch.delenv("NOETA_MAX_TOKENS", raising=False)

    provider = demo_example._build_provider()
    assert provider is not None
    # AnthropicProvider stores the default on ``_default_max_tokens``.
    assert getattr(provider, "_default_max_tokens", None) == 1024


def test_example_anthropic_max_tokens_env_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``NOETA_MAX_TOKENS`` is set, the demo's helper threads it
    through to the provider."""
    import importlib.util

    example_path = (
        Path(__file__).resolve().parents[1]
        / "examples"
        / "_internal"
        / "real_provider_subtask_demo.py"
    )
    spec = importlib.util.spec_from_file_location("demo_example", example_path)
    assert spec is not None and spec.loader is not None
    demo_example = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(demo_example)

    monkeypatch.setenv("NOETA_PROVIDER", "anthropic")
    monkeypatch.setenv("NOETA_API_KEY", "dummy-key-for-test")
    monkeypatch.setenv("NOETA_MODEL", "claude-test")
    monkeypatch.setenv("NOETA_MAX_TOKENS", "4096")

    provider = demo_example._build_provider()
    assert provider is not None
    assert getattr(provider, "_default_max_tokens", None) == 4096


def test_example_script_skips_cleanly_when_env_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Running the example with no provider env should print ``skipped:
    ...`` and exit 0. CI runs this branch to keep the example honest."""
    monkeypatch.delenv("NOETA_OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("NOETA_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("NOETA_OPENAI_MODEL", raising=False)
    monkeypatch.delenv("NOETA_API_KEY", raising=False)
    monkeypatch.delenv("NOETA_MODEL", raising=False)
    monkeypatch.setenv("NOETA_PROVIDER", "openai")

    example_path = (
        Path(__file__).resolve().parents[1]
        / "examples"
        / "_internal"
        / "real_provider_subtask_demo.py"
    )
    assert example_path.exists(), example_path

    result = subprocess.run(
        [sys.executable, str(example_path)],
        capture_output=True,
        text=True,
        env={
            **{k: v for k, v in __import__("os").environ.items()
               if not k.startswith("NOETA_")},
            "NOETA_PROVIDER": "openai",
        },
    )
    assert result.returncode == 0, result.stderr
    assert "skipped:" in result.stdout
