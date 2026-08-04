"""Compaction has to survive real traffic, not just a synthetic first pass.

Three failure modes killed a live session outright, each of them non-retryable:

1. the summarize round-trip was built with ``tools=[]`` while the collapsed
   prefix still carried ``tool_use`` / ``tool_result`` blocks — a provider
   rejects that body (Anthropic: 400), the empty-summary guard turns the error
   into ``FailDecision(compaction_summary_failed, retryable=False)``, and the
   FIRST real proactive compaction ends the task. It also threw away the cached
   system+tools prefix for that call;
2. every compaction re-summarised ``raw_history[:boundary]`` from index 0 while
   the trigger measured the *composed* (post-summary) size, so the Nth
   summarize input grew with N until the summarize call itself overflowed —
   same non-retryable death, two or three compactions in;
3. a context overflow that arrives as an HTTP-200 ``stop_reason`` (rather than a
   400 body) fell through to a bare ``error`` and became
   ``FailDecision(llm_error)``, never reaching the passive-compaction path.

These tests pin the three fixes: live tool schemas on the summarize request, a
bounded summarize input across consecutive compactions, and the 200-shape
overflow reaching ``CompactionRequestedDecision``.
"""

from __future__ import annotations

from typing import Any

import json

import httpx
import respx

from noeta.context.composer import COMPOSER_VERSION, ThreeSegmentComposer
from noeta.core.engine import Engine
from noeta.core.hooks import HookManager
from noeta.core.wiring import wire_default_observers
from noeta.builtins.providers.impl.anthropic import AnthropicProvider
from noeta.builtins.react.impl import ReActPolicy
from noeta.protocols.decisions import CompactionRequestedDecision
from noeta.protocols.messages import (
    LLMRequest,
    LLMResponse,
    Message,
    TextBlock,
    ToolUseBlock,
    Usage,
)
from noeta.protocols.step_context import StepContext
from noeta.protocols.token_estimate import estimate_messages_tokens
from noeta.protocols.tool import ToolContext, ToolResult
from noeta.runtime.llm import RuntimeLLMClient
from noeta.runtime.tool import ToolRuntime
from noeta.storage.memory import (
    InMemoryContentStore,
    InMemoryDispatcher,
    InMemoryEventLog,
)
from noeta.testing.composer import fake_view
from noeta.testing.fake_llm import FakeLLMProvider


_SYSTEM_PROMPT = "You are a coding agent. Work the task then finish."
#: The first words of ``summarize.md`` — how a fake provider tells a summarize
#: round-trip apart from a normal turn.
_SUMMARY_MARKER = "Summarize the conversation so far"

#: The compaction knobs, derived exactly as ``derive_compaction_config`` does:
#: ``available = context_window - max_output - buffer`` and a protected tail of
#: a third of that. The assertions below compare against ``_AVAILABLE``, so the
#: derivation has to match the production one.
_CONTEXT_WINDOW = 2_000
_MAX_OUTPUT = 100
_BUFFER = 100
_AVAILABLE = _CONTEXT_WINDOW - _MAX_OUTPUT - _BUFFER
_TAIL = _AVAILABLE // 3


def _ctx() -> StepContext:
    return StepContext(task_id="t-1", lease_id="l-1", trace_id="tr-1")


def _system_text(request: LLMRequest) -> str:
    if request.system is None:
        return ""
    return "".join(
        b.text for b in request.system.content if isinstance(b, TextBlock)
    )


def _is_summarize(request: LLMRequest) -> bool:
    return _SUMMARY_MARKER in _system_text(request)


# ---------------------------------------------------------------------------
# (a) the summarize request carries the live tool schemas
# ---------------------------------------------------------------------------


_TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "work",
            "description": "Do a unit of work.",
            "parameters": {"type": "object", "additionalProperties": True},
        },
    }
]


def _tool_bearing_history(n: int = 12) -> list[Message]:
    """A history that carries real ``tool_use`` blocks — the shape a provider
    rejects when the request declares no tools."""
    msgs: list[Message] = [
        Message(role="user", content=[TextBlock(text="do the work " + "x" * 400)])
    ]
    for i in range(n):
        msgs.append(
            Message(
                role="assistant",
                content=[
                    TextBlock(text=f"step {i} " + "y" * 800),
                    ToolUseBlock(
                        call_id=f"c{i}", tool_name="work", arguments={"step": i}
                    ),
                ],
            )
        )
    return msgs


def test_summarize_request_carries_the_live_tool_schemas() -> None:
    """The summarize round-trip declares the SAME tools as a normal turn.

    With ``tools=[]`` the request is a tool-bearing history with no tool
    definitions — invalid at the provider — and it also drops off the cached
    system+tools prefix. The live schemas come from the same View the main
    request reads them from, so the call stays deterministic.
    """
    provider = FakeLLMProvider(
        responses=[
            LLMResponse(
                stop_reason="end_turn", content=[TextBlock(text="condensed note")]
            )
        ]
    )
    client = RuntimeLLMClient(
        provider=provider,
        event_log=InMemoryEventLog(),
        content_store=InMemoryContentStore(),
    )
    policy = ReActPolicy(
        llm=client,
        tools={},
        system_prompt=_SYSTEM_PROMPT,
        model="gpt-4o",
        context_window=_CONTEXT_WINDOW,
        max_output_tokens=_MAX_OUTPUT,
        compaction_buffer=_BUFFER,
        tail_token_budget=_TAIL,
        composer_version=COMPOSER_VERSION,
    )
    view = fake_view(
        _tool_bearing_history(), provider_tool_schemas=list(_TOOL_SCHEMAS)
    )

    decision = policy.decide(_ctx(), view)

    assert isinstance(decision, CompactionRequestedDecision)
    assert len(provider.received_requests) == 1
    summarize_req = provider.received_requests[0]
    assert _is_summarize(summarize_req)
    # The live tool list, verbatim — not an empty list.
    assert summarize_req.tools == list(view.provider_tool_schemas)
    assert summarize_req.tools
    # ... and the history it declares them for really does carry tool blocks.
    assert any(
        isinstance(b, ToolUseBlock)
        for m in summarize_req.messages
        for b in m.content
    )


def test_summarize_request_is_deterministic() -> None:
    """Same history + same tools ⇒ byte-identical summarize request, which is
    what resume / replay rests on."""
    view = fake_view(
        _tool_bearing_history(), provider_tool_schemas=list(_TOOL_SCHEMAS)
    )
    built = []
    for _ in range(2):
        provider = FakeLLMProvider(
            responses=[
                LLMResponse(
                    stop_reason="end_turn", content=[TextBlock(text="note")]
                )
            ]
        )
        client = RuntimeLLMClient(
            provider=provider,
            event_log=InMemoryEventLog(),
            content_store=InMemoryContentStore(),
        )
        policy = ReActPolicy(
            llm=client,
            tools={},
            system_prompt=_SYSTEM_PROMPT,
            model="gpt-4o",
            context_window=_CONTEXT_WINDOW,
            max_output_tokens=_MAX_OUTPUT,
            compaction_buffer=_BUFFER,
            tail_token_budget=_TAIL,
            composer_version=COMPOSER_VERSION,
        )
        policy.decide(_ctx(), view)
        built.append(provider.received_requests[0])
    assert built[0] == built[1]


# ---------------------------------------------------------------------------
# (b) the summarize input stays bounded across consecutive compactions
# ---------------------------------------------------------------------------


class _WorkTool:
    """A real tool. Its output is small on purpose: the growth that re-triggers
    compaction lives in the assistant text blocks (never pruned), so the raw
    history and the composed history grow together and the size assertion below
    measures the summarize input rather than the prune valve."""

    name = "work"
    risk_level = "low"
    description = "Do a unit of work."
    input_schema: dict[str, Any] = {
        "type": "object",
        "additionalProperties": True,
    }

    def invoke(
        self, arguments: dict[str, Any], ctx: ToolContext  # noqa: ARG002
    ) -> ToolResult:
        return ToolResult(
            success=True, output=f"ok-{arguments.get('step', '?')}", summary="ok"
        )


class _SizedProvider:
    """Records every request and reports usage equal to the neutral estimate.

    Reporting real usage is what makes the fake *sized*: the policy's trigger
    mixes recorded usage with the appended delta, and the density conversion
    divides the tail budget by ``real / estimate``. Pinning that ratio at 1.0
    (usage == estimate) keeps the arithmetic honest without pretending to know
    a vendor tokenizer.
    """

    def __init__(self, *, compactions_before_finish: int) -> None:
        self.requests: list[LLMRequest] = []
        self.summarize_requests: list[LLMRequest] = []
        self._target = compactions_before_finish
        self._turn = 0

    def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        usage = Usage(uncached=estimate_messages_tokens(request.messages))
        if _is_summarize(request):
            self.summarize_requests.append(request)
            return LLMResponse(
                stop_reason="end_turn",
                content=[
                    TextBlock(
                        text=(
                            f"CONDENSED NOTE {len(self.summarize_requests)}: "
                            "intent, concepts, paths, errors, user messages, "
                            "pending tasks, decisions."
                        )
                    )
                ],
                usage=usage,
            )
        if len(self.summarize_requests) >= self._target:
            return LLMResponse(
                stop_reason="end_turn",
                content=[TextBlock(text="all done")],
                usage=usage,
            )
        self._turn += 1
        return LLMResponse(
            stop_reason="tool_use",
            content=[
                TextBlock(text=f"turn {self._turn}: " + "z" * 1_100),
                ToolUseBlock(
                    call_id=f"call-{self._turn}",
                    tool_name="work",
                    arguments={"step": self._turn},
                ),
            ],
            usage=usage,
        )


def _run_multi_compaction(compactions: int, *, constraint: str = ""):
    dispatcher = InMemoryDispatcher()
    event_log = InMemoryEventLog(lease_validator=dispatcher)
    content_store = InMemoryContentStore()
    wire_default_observers(event_log, dispatcher)
    provider = _SizedProvider(compactions_before_finish=compactions)
    llm = RuntimeLLMClient(
        provider=provider, event_log=event_log, content_store=content_store
    )
    tools = {"work": _WorkTool()}
    engine = Engine(
        event_log=event_log,
        content_store=content_store,
        composer=ThreeSegmentComposer(
            system_prompt=_SYSTEM_PROMPT,
            tools=tools,
            content_store=content_store,
            tail_token_budget=_TAIL,
            available_window=_AVAILABLE,
        ),
        policy=ReActPolicy(
            llm=llm,
            tools=tools,
            system_prompt=_SYSTEM_PROMPT,
            model="gpt-4o",
            max_steps=80,
            context_window=_CONTEXT_WINDOW,
            max_output_tokens=_MAX_OUTPUT,
            compaction_buffer=_BUFFER,
            tail_token_budget=_TAIL,
            composer_version=COMPOSER_VERSION,
        ),
        tools=tools,
        tool_runtime=ToolRuntime(
            event_log=event_log, content_store=content_store
        ),
        hooks=HookManager(),
    )
    task = engine.create_task(goal="long task", policy_name="react")
    dispatcher.enqueue(task.task_id)
    lease = dispatcher.lease(worker_id="rec")
    assert lease is not None
    # Seed just enough history to trip the FIRST proactive compaction; every
    # later one is produced by the loop's own growth.
    for i in range(6):
        prefix = f"{constraint}\n" if (constraint and i == 0) else ""
        engine.append_user_message(
            task,
            content=[TextBlock(text=f"{prefix}seed {i}: " + "s" * 1_190)],
            lease_id=lease.lease_id,
        )
    final = engine.run_one_step(task, lease_id=lease.lease_id)
    return provider, event_log, task.task_id, final


def test_summarize_input_stays_within_the_window_across_compactions() -> None:
    """Three consecutive compactions, and EVERY summarize input fits the
    available window.

    Re-summarising the raw prefix from index 0 makes input N ≈ N × (window −
    tail): the second or third summarize call exceeds the window and dies as
    ``compaction_summary_failed``. Feeding the previous note back in plus only
    the delta keeps every pass about one window wide.
    """
    provider, log, task_id, final = _run_multi_compaction(3)
    types = [e.type for e in log.read(task_id)]

    assert len(provider.summarize_requests) >= 3, types
    for i, req in enumerate(provider.summarize_requests):
        size = estimate_messages_tokens(req.messages)
        assert size <= _AVAILABLE, (
            f"summarize #{i + 1} input is {size} estimated tokens, over the "
            f"{_AVAILABLE}-token available window"
        )

    # Every compaction landed and strictly advanced the raw-history boundary.
    compacted = [e for e in log.read(task_id) if e.type == "Compacted"]
    assert len(compacted) >= 3, types
    boundaries = [e.payload.boundary_count for e in compacted]
    assert all(b2 > b1 for b1, b2 in zip(boundaries, boundaries[1:])), boundaries

    # The session survived all of it.
    assert "TaskCompleted" in types
    assert "TaskFailed" not in types
    assert final.status == "terminal"


def test_later_summarize_inputs_open_on_the_previous_note() -> None:
    """The bound is not a coincidence of sizing: from the second compaction on,
    the summarize input literally begins with the note the previous compaction
    produced, and the messages the previous note already covers are not re-sent.
    """
    provider, _log, _task_id, _final = _run_multi_compaction(3)
    for i, req in enumerate(provider.summarize_requests[1:], start=2):
        head = req.messages[0]
        assert head.role == "user"
        assert isinstance(head.content[0], TextBlock)
        assert head.content[0].text.startswith(f"CONDENSED NOTE {i - 1}:"), (
            f"summarize #{i} did not open on the previous note: "
            f"{head.content[0].text[:60]!r}"
        )
        # Exactly one note, never a chain of them.
        assert sum(
            1
            for m in req.messages
            for b in m.content
            if isinstance(b, TextBlock) and b.text.startswith("CONDENSED NOTE")
        ) == 1


def test_every_summarize_request_declares_the_live_tools_end_to_end() -> None:
    """(a) again, but end to end: whatever tool schemas the main turns carry,
    the summarize turns carry the same ones — over a history that really does
    contain ``tool_use`` / ``tool_result`` pairs."""
    provider, _log, _task_id, _final = _run_multi_compaction(3)
    main_requests = [r for r in provider.requests if not _is_summarize(r)]
    assert main_requests
    live_tools = main_requests[0].tools
    assert live_tools, "the fixture must mount at least one tool"
    for req in provider.summarize_requests:
        assert req.tools == live_tools


def test_safety_constraint_survives_a_chain_of_compactions() -> None:
    """A "do not touch X" stated in the very first turns still binds after three
    compactions.

    Bounding the input is only safe if the constraint post-check keeps working
    once the original turn is no longer in the input: it now scans the previous
    note, which carries the constraint forward. The fake summarizer never
    reproduces the constraint on its own, so every note in the chain owes it to
    the post-check — and the re-injected block must stay in the same form pass
    after pass rather than growing a bullet each time.
    """
    constraint = "Do not touch config/secrets.yaml ever."
    provider, _log, _task_id, _final = _run_multi_compaction(
        3, constraint=constraint
    )
    assert len(provider.summarize_requests) >= 3
    for i, req in enumerate(provider.summarize_requests[1:], start=2):
        note = req.messages[0].content[0]
        assert isinstance(note, TextBlock)
        # The head really is the previous NOTE — not the original turn, which a
        # from-zero input would have re-sent verbatim.
        assert note.text.startswith("CONDENSED NOTE")
        assert constraint in note.text, (
            f"the note fed into summarize #{i} lost the constraint"
        )
        # Idempotent re-injection: no "- - " / "- - - " bullet accumulation.
        assert "- - " not in note.text


# ---------------------------------------------------------------------------
# (c) a 200-shape context overflow compacts instead of killing the task
# ---------------------------------------------------------------------------


_ANTHROPIC_BASE = "https://api.anthropic.test"
_ANTHROPIC_ENDPOINT = f"{_ANTHROPIC_BASE}/v1/messages"


def _anthropic_body(
    *,
    stop_reason: str,
    text: str = "ok",
    stop_details: Any = None,
    input_tokens: int = 2_600,
) -> dict[str, Any]:
    """A Messages-API body. ``input_tokens`` defaults to roughly the neutral
    estimate of the fixture history: the policy divides the tail budget by the
    observed ``real / estimate`` density, so a stub that reports a token count
    unrelated to the payload would protect a tail the whole history fits inside
    and the boundary could not advance."""
    body: dict[str, Any] = {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-4-7",
        "content": [{"type": "text", "text": text}],
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {"input_tokens": input_tokens, "output_tokens": 5},
    }
    if stop_details is not None:
        body["stop_details"] = stop_details
    return body


def _anthropic_stream(message: dict[str, Any]) -> httpx.Response:
    """Serve a Messages-API body over SSE — the only transport the adapter has.

    ``complete`` delegates to the streaming path with a discarding sink (D6),
    so a fixture that returned a whole JSON body is no longer something the
    provider can read. Blocks are delivered complete on their
    ``content_block_start``, which makes this a pure re-encoding: the
    accumulator rebuilds exactly ``message``.
    """
    head = {key: value for key, value in message.items() if key != "content"}
    frames: list[tuple[str, dict[str, Any]]] = [
        ("message_start", {"type": "message_start", "message": head})
    ]
    for index, block in enumerate(message.get("content") or []):
        frames.append(
            (
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": index,
                    "content_block": block,
                },
            )
        )
        frames.append(
            ("content_block_stop", {"type": "content_block_stop", "index": index})
        )
    frames.append(("message_stop", {"type": "message_stop"}))
    body = "".join(
        f"event: {name}\ndata: {json.dumps(payload)}\n\n" for name, payload in frames
    ).encode("utf-8")
    return httpx.Response(
        200, content=body, headers={"content-type": "text/event-stream"}
    )


@respx.mock
def test_http200_overflow_stop_reason_drives_compaction_not_failure() -> None:
    """``stop_reason="model_context_window_exceeded"`` is an overflow wearing a
    200. Unmapped it becomes a bare ``error`` → ``FailDecision(llm_error,
    retryable=False)`` and the task dies; mapped to the neutral overflow
    category it reaches the policy's existing passive path, which compacts and
    lets the step continue.
    """
    respx.post(_ANTHROPIC_ENDPOINT).mock(
        side_effect=[
            _anthropic_stream(
                _anthropic_body(
                    stop_reason="model_context_window_exceeded",
                    text="",
                    stop_details={"type": "model_context_window_exceeded"},
                )
            ),
            _anthropic_stream(
                _anthropic_body(stop_reason="end_turn", text="condensed note")
            ),
        ]
    )
    provider = AnthropicProvider(
        api_key="sk-ant-test", base_url=_ANTHROPIC_BASE
    )
    client = RuntimeLLMClient(
        provider=provider,
        event_log=InMemoryEventLog(),
        content_store=InMemoryContentStore(),
    )
    policy = ReActPolicy(
        llm=client,
        tools={},
        system_prompt=_SYSTEM_PROMPT,
        model="claude-opus-4-7",
        # Wide enough that the proactive estimate cannot pre-empt: the passive
        # path is the one under test.
        context_window=1_000_000,
        max_output_tokens=_MAX_OUTPUT,
        compaction_buffer=_BUFFER,
        tail_token_budget=_TAIL,
        composer_version=COMPOSER_VERSION,
    )
    view = fake_view(
        _tool_bearing_history(), provider_tool_schemas=list(_TOOL_SCHEMAS)
    )

    decision = policy.decide(_ctx(), view)

    assert isinstance(decision, CompactionRequestedDecision)
    assert decision.reason == "overflow"
    assert decision.summary.startswith("condensed note")
