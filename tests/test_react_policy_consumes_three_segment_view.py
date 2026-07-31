"""ReActPolicy builds its ``LLMRequest`` from the View, not from its own
constructor arguments.

:class:`ThreeSegmentComposer` is the single source of truth for prompt
material, so ``provider_tool_schemas`` becomes ``LLMRequest.tools`` verbatim,
``iter_messages()`` becomes ``LLMRequest.messages``, and the stable prefix's
first system Message becomes ``LLMRequest.system``. The policy is deliberately
constructed with a different tool set and a placeholder system prompt, so any
fallback to the constructor copies fails these assertions loudly.
"""

from __future__ import annotations

from typing import Any

from noeta.context.composer import ThreeSegmentComposer
from noeta.builtins.react.impl import ReActPolicy
from noeta.protocols.messages import LLMResponse, Message, TextBlock
from noeta.protocols.step_context import StepContext
from noeta.protocols.task import Task, TaskState
from noeta.storage.memory import InMemoryContentStore
from noeta.testing.fake_llm import FakeLLMProvider
from noeta.runtime.llm import RuntimeLLMClient
from noeta.storage.memory import InMemoryEventLog


class _FakeTool:
    def __init__(
        self,
        name: str,
        input_schema: dict[str, Any] | None = None,
    ) -> None:
        self.name = name
        self.risk_level = "low"
        self.input_schema = input_schema or {
            "type": "object",
            "additionalProperties": True,
        }

    def invoke(self, arguments, ctx):  # pragma: no cover
        raise NotImplementedError


def _setup(
    *,
    system_prompt: str,
    schema: dict[str, Any],
    history: list[Message],
):
    cs = InMemoryContentStore()
    composer = ThreeSegmentComposer(
        system_prompt=system_prompt,
        tools={"echo": _FakeTool("echo", input_schema=schema)},
        content_store=cs,
    )
    task = Task(task_id="t-1", state=TaskState())
    task.runtime.messages.extend(history)
    view = composer.compose(task)

    provider = FakeLLMProvider(
        responses=[
            LLMResponse(stop_reason="end_turn", content=[TextBlock(text="ok")])
        ]
    )
    llm = RuntimeLLMClient(
        provider=provider, event_log=InMemoryEventLog(), content_store=cs
    )
    # Deliberately mismatched constructor material — the View must win.
    policy = ReActPolicy(
        llm=llm,
        tools={"echo": _FakeTool("echo")},
        system_prompt="constructor system that should NOT be used",
        model="m",
    )
    ctx = StepContext(task_id=task.task_id, lease_id="lease-1", trace_id="t")
    policy.decide(ctx, view)
    return provider, view


def test_tools_field_matches_view_provider_tool_schemas_not_constructor_tools() -> None:
    schema = {"type": "object", "properties": {"q": {"type": "string"}}}
    history = [Message(role="user", content=[TextBlock(text="hi")])]
    provider, view = _setup(
        system_prompt="be helpful", schema=schema, history=history
    )

    req = provider.received_requests[0]
    assert req.tools == view.provider_tool_schemas


def test_messages_field_matches_view_iter_messages() -> None:
    history = [Message(role="user", content=[TextBlock(text="hello world")])]
    provider, view = _setup(
        system_prompt="be helpful",
        schema={"type": "object"},
        history=history,
    )

    req = provider.received_requests[0]
    assert req.messages == view.iter_messages()


def test_system_field_comes_from_view_stable_prefix_not_constructor() -> None:
    history = [Message(role="user", content=[TextBlock(text="hi")])]
    provider, view = _setup(
        system_prompt="composer-supplied prompt",
        schema={"type": "object"},
        history=history,
    )

    req = provider.received_requests[0]
    # stable_prefix's first Message is the system Message Composer produced.
    expected_system = view.segments[0].content[0]
    assert req.system == expected_system
    # Sanity: NOT the constructor placeholder.
    assert req.system.content[0].text == "composer-supplied prompt"
