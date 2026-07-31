"""Contributor demo — the subtask suspend / wake-resume handshake, end to end.

Two Engines share one sqlite stack: a scripted parent policy that spawns a
subtask and then finishes, and a ReAct policy running a real provider for the
child. The parent suspends on the child and wakes carrying the child's
``SubtaskResult`` intact, which is the sequence this walkthrough exists to make
observable — a contributor about to change a kernel mechanism can run it and
watch the current behaviour rather than infer it.

Run::

    # OpenAI-compatible:
    NOETA_OPENAI_BASE_URL=... NOETA_OPENAI_API_KEY=... NOETA_OPENAI_MODEL=... \\
    uv run python examples/_internal/real_provider_subtask_demo.py

    # Anthropic (its API requires ``max_tokens``; the demo defaults to 1024,
    # override via ``NOETA_MAX_TOKENS``):
    NOETA_PROVIDER=anthropic NOETA_API_KEY=... NOETA_MODEL=claude-... \\
    uv run python examples/_internal/real_provider_subtask_demo.py

A missing required variable prints ``skipped: ...`` and exits 0, so the script
is safe to run unconfigured.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Optional

from noeta.context.composer import ThreeSegmentComposer
from noeta.core.engine import Engine
from noeta.core.fold import fold
from noeta.core.wiring import wire_default_observers
from noeta.builtins.react.impl import ReActPolicy
from noeta.policies.stub import StubScriptedPolicy
from noeta.protocols.decisions import FinishDecision, SpawnSubtaskDecision
from noeta.protocols.messages import TextBlock
from noeta.protocols.tool import Tool
from noeta.protocols.wake import SubtaskCompleted, SubtaskResult
from noeta.runtime.llm import RuntimeLLMClient
from noeta.tools.fake import FakeTool


def _build_provider() -> Optional[Any]:
    """The configured provider, or ``None`` when its variables are incomplete.

    Selection is inlined rather than shared so the script stays runnable on its
    own, and ``None`` rather than an exception is what lets :func:`main` skip
    cleanly on an unconfigured machine.
    """
    provider_kind = os.environ.get("NOETA_PROVIDER", "openai")
    if provider_kind == "openai":
        required = ("NOETA_OPENAI_BASE_URL", "NOETA_OPENAI_API_KEY", "NOETA_OPENAI_MODEL")
        if any(os.environ.get(v) is None for v in required):
            return None
        from noeta.builtins.providers.impl.openai_compat import OpenAICompatProvider

        return OpenAICompatProvider(
            base_url=os.environ["NOETA_OPENAI_BASE_URL"],
            api_key=os.environ["NOETA_OPENAI_API_KEY"],
        )
    if provider_kind == "anthropic":
        if os.environ.get("NOETA_API_KEY") is None:
            return None
        from noeta.builtins.providers.impl.anthropic import AnthropicProvider

        # The Anthropic adapter fail-fasts when neither the request nor the
        # provider carries ``max_tokens``, so an explicit default is what makes
        # the run command in the module docstring work without an extra
        # variable.
        max_tokens_str = os.environ.get("NOETA_MAX_TOKENS")
        default_max_tokens = int(max_tokens_str) if max_tokens_str else 1024
        return AnthropicProvider(
            api_key=os.environ["NOETA_API_KEY"],
            default_max_tokens=default_max_tokens,
        )
    return None


def _model() -> str:
    return (
        os.environ.get("NOETA_OPENAI_MODEL")
        or os.environ.get("NOETA_MODEL")
        or "gpt-4o-mini"
    )


def _build_echo_tool() -> Tool:
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


def _assert_invariants(
    event_log: Any, parent_id: str, child_id: str
) -> None:
    """Pin the parent/child relative ordering of the handshake envelopes.

    Asserting relative order rather than the full event list is what keeps the
    demo honest against a real model: the runtime interleaves bookkeeping
    envelopes (``ContextPlanComposed``, ``TaskSnapshot``) whose count varies run
    to run, while the handshake's order does not.
    """
    parent_envs = event_log.read(parent_id)
    parent_types = [e.type for e in parent_envs]
    assert parent_envs[0].type == "TaskCreated", parent_types
    assert parent_envs[-1].type == "TaskCompleted", parent_types

    def _index_of(typ: str) -> int:
        for idx, env in enumerate(parent_envs):
            if env.type == typ:
                return idx
        raise AssertionError(f"missing {typ} in parent stream: {parent_types}")

    spawn_idx = _index_of("SubtaskSpawned")
    suspend_idx = _index_of("TaskSuspended")
    completed_idx = _index_of("SubtaskCompleted")
    woken_idx = _index_of("TaskWoken")
    assert spawn_idx < suspend_idx < completed_idx < woken_idx, (
        f"subtask handshake out of order: {parent_types}"
    )
    woken_env = parent_envs[woken_idx]
    assert isinstance(woken_env.payload.wake_event, SubtaskCompleted)
    assert woken_env.payload.wake_event.subtask_id == child_id
    assert isinstance(woken_env.payload.wake_event.result, SubtaskResult)

    child_envs = event_log.read(child_id)
    child_types = [e.type for e in child_envs]
    assert child_envs[0].type == "TaskCreated", child_types
    assert child_envs[0].payload.parent_task_id == parent_id
    assert child_envs[-1].type == "TaskCompleted", child_types
    # The child really went out to the provider, rather than terminating on a
    # policy shortcut that would make the whole run prove nothing.
    assert "LLMRequestStarted" in child_types
    assert "LLMResponseRecorded" in child_types
    assert "LLMRequestFinished" in child_types


def main() -> int:
    provider = _build_provider()
    if provider is None:
        print(
            "skipped: real provider env vars not set "
            "(NOETA_OPENAI_BASE_URL/NOETA_OPENAI_API_KEY/NOETA_OPENAI_MODEL "
            "or NOETA_PROVIDER=anthropic + NOETA_API_KEY + NOETA_MODEL)"
        )
        return 0

    model = _model()
    system_prompt = "You are a demo assistant. Use the echo tool with text='hello' then finish."

    with tempfile.TemporaryDirectory(prefix="noeta-demo-") as tmp:
        sqlite_path = str(Path(tmp) / "demo.sqlite")

        from noeta.sdk.storage import build_storage_stack

        event_log, content_store, dispatcher = build_storage_stack("sqlite", path=sqlite_path)
        wire_default_observers(event_log, dispatcher)

        tools = {"echo": _build_echo_tool()}
        composer = ThreeSegmentComposer(
            system_prompt=system_prompt, tools=tools, content_store=content_store
        )

        # The parent's decisions are scripted so the run isolates the handshake:
        # only the child's behaviour is left to the model.
        scripted_parent = StubScriptedPolicy(
            [
                SpawnSubtaskDecision(
                    agent_name="helper",
                    goal="echo hello and finish",
                ),
                FinishDecision(answer="parent finished after child returned"),
            ]
        )
        engine_parent = Engine(
            event_log=event_log,
            content_store=content_store,
            composer=composer,
            policy=scripted_parent,
            tools=tools,
        )

        child_llm = RuntimeLLMClient(
            provider=provider, event_log=event_log, content_store=content_store
        )
        react_child = ReActPolicy(
            llm=child_llm,
            tools=tools,
            system_prompt=system_prompt,
            model=model,
            max_steps=4,
        )
        engine_child = Engine(
            event_log=event_log,
            content_store=content_store,
            composer=composer,
            policy=react_child,
            tools=tools,
        )

        # ---- Beat 1: the parent spawns the child and suspends on it ----
        parent_task = engine_parent.create_task(
            goal="orchestrate", policy_name="scripted_demo"
        )
        dispatcher.enqueue(parent_task.task_id)
        parent_lease = dispatcher.lease(worker_id="demo-parent", lease_seconds=60.0)
        assert parent_lease is not None
        engine_parent.append_user_message(
            parent_task,
            content=[TextBlock(text="please orchestrate")],
            lease_id=parent_lease.lease_id,
        )
        parent_after_spawn = engine_parent.run_one_step(
            parent_task, lease_id=parent_lease.lease_id
        )
        dispatcher.release(
            parent_lease.lease_id,
            next_state=parent_after_spawn.status,
            wake_on=parent_after_spawn.wake_on,
        )
        assert parent_after_spawn.status == "suspended"
        assert isinstance(parent_after_spawn.wake_on, SubtaskCompleted)
        child_id = parent_after_spawn.wake_on.subtask_id
        print(f"parent {parent_task.task_id[:14]}... suspended waiting on {child_id[:14]}...")

        # ---- Beat 2: the child runs ReAct against the real provider ----
        child_lease = dispatcher.lease(
            worker_id="demo-child", lease_seconds=120.0, task_id=child_id
        )
        assert child_lease is not None
        child_task = fold(event_log, content_store, child_id)
        child_after = engine_child.run_one_step(
            child_task, lease_id=child_lease.lease_id
        )
        dispatcher.release(child_lease.lease_id, next_state=child_after.status)
        assert child_after.status == "terminal", (
            f"child did not reach terminal: {child_after.status}"
        )
        print(f"child {child_id[:14]}... reached terminal via real LLM")

        # ---- Beat 3: leasing the parent again hands back the wake event ----
        parent_wake_lease = dispatcher.lease(
            worker_id="demo-parent-resume",
            lease_seconds=60.0,
            task_id=parent_task.task_id,
        )
        assert parent_wake_lease is not None
        assert parent_wake_lease.wake_event is not None, (
            "expected lease.wake_event populated by wake-resume path"
        )
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
        print(f"parent {parent_task.task_id[:14]}... resumed + terminated")

        _assert_invariants(event_log, parent_task.task_id, child_id)
        print("event invariants OK")

    print("\ndemo complete — parent + child wake-resume all passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
