"""Cost: RuntimeLLMClient pricing-callback injection → LLMRequestFinished.cost_usd
→ fold → GovernanceState.cost_usd → BudgetGuard end-to-end (D-C4).

GovernanceState already has real token fields and fold already accumulates
cost_usd. This work item only proves the link is live: a non-``None`` pricing
callback turns ``cost_usd=0.0`` into a real catalog-computed number, and the
already-wired ``GovernanceState.cost_usd → BudgetGuard.max_cost_usd``
accumulator fires under real cost.
"""

from __future__ import annotations

from noeta.core.engine import Engine
from noeta.core.fold import fold
from noeta.core.hooks import HookManager
from noeta.guards.budget import Budget, BudgetGuard
from noeta.policies.stub import StubScriptedPolicy
from noeta.protocols.decisions import FinishDecision, ToolCall, ToolCallsDecision
from noeta.protocols.messages import (
    LLMRequest,
    LLMResponse,
    Message,
    TextBlock,
    Usage,
)
from noeta.protocols.step_context import StepContext
from noeta.providers.catalog import price
from noeta.runtime.llm import RuntimeLLMClient
from noeta.runtime.tool import ToolRuntime
from noeta.storage.memory import (
    InMemoryContentStore,
    InMemoryDispatcher,
    InMemoryEventLog,
)
from noeta.testing.composer import trivial_three_segment
from noeta.testing.fake_llm import FakeLLMProvider
from noeta.tools.fake import FakeTool


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ctx(task_id: str = "task-1") -> StepContext:
    return StepContext(task_id=task_id, lease_id="lease-1", trace_id="trace-1")


def _req(model: str = "claude-opus-4-8") -> LLMRequest:
    return LLMRequest(
        model=model,
        messages=[Message(role="user", content=[TextBlock(text="hi")])],
    )


def _response(usage: Usage) -> LLMResponse:
    return LLMResponse(
        stop_reason="end_turn",
        content=[TextBlock(text="ok")],
        usage=usage,
        raw={"id": "r"},
    )


def _finished(log: InMemoryEventLog, task_id: str = "task-1"):
    return next(
        e for e in log.read(task_id) if e.type == "LLMRequestFinished"
    )


# ---------------------------------------------------------------------------
# RuntimeLLMClient pricing-callback injection
# ---------------------------------------------------------------------------


def test_cost_usd_is_computed_from_injected_pricing_callback() -> None:
    log = InMemoryEventLog()
    cs = InMemoryContentStore()
    usage = Usage(uncached=1_000_000, output=500_000)
    provider = FakeLLMProvider(responses=[_response(usage)])
    client = RuntimeLLMClient(
        provider=provider,
        event_log=log,
        content_store=cs,
        pricing=price,
    )

    client.complete(_req("claude-opus-4-8"), _ctx())

    finished = _finished(log)
    assert finished.payload.cost_usd == price("claude-opus-4-8", usage)
    assert finished.payload.cost_usd > 0.0


def test_cost_usd_falls_back_to_zero_when_no_pricing() -> None:
    """stub / no-pricing path must not regress: cost_usd stays 0.0."""
    log = InMemoryEventLog()
    cs = InMemoryContentStore()
    provider = FakeLLMProvider(responses=[_response(Usage(uncached=10, output=5))])
    client = RuntimeLLMClient(provider=provider, event_log=log, content_store=cs)

    client.complete(_req(), _ctx())

    assert _finished(log).payload.cost_usd == 0.0


def test_error_response_costs_zero_even_with_pricing() -> None:
    """An error response carries empty Usage → cost 0, no accumulator pollution."""

    class _Exploding:
        def complete(self, request: LLMRequest) -> LLMResponse:  # noqa: ARG002
            raise RuntimeError("boom")

    log = InMemoryEventLog()
    cs = InMemoryContentStore()
    client = RuntimeLLMClient(
        provider=_Exploding(),
        event_log=log,
        content_store=cs,
        pricing=price,
    )

    client.complete(_req("claude-opus-4-8"), _ctx())

    assert _finished(log).payload.cost_usd == 0.0


# ---------------------------------------------------------------------------
# fold accumulation
# ---------------------------------------------------------------------------


def test_fold_accumulates_cost_usd_across_finished_events() -> None:
    log = InMemoryEventLog()
    cs = InMemoryContentStore()
    u1 = Usage(uncached=1_000_000, output=0)
    u2 = Usage(uncached=0, output=1_000_000)
    provider = FakeLLMProvider(responses=[_response(u1), _response(u2)])
    client = RuntimeLLMClient(
        provider=provider, event_log=log, content_store=cs, pricing=price
    )

    # Create a real task so fold has a TaskStarted to anchor on.
    disp = InMemoryDispatcher()
    log.bind_lease_registry(disp)
    runtime = ToolRuntime(event_log=log, content_store=cs)
    engine = Engine(
        event_log=log,
        content_store=cs,
        composer=trivial_three_segment(cs),
        policy=StubScriptedPolicy([FinishDecision(answer="x")]),
        hooks=HookManager(),
        tools={},
        tool_runtime=runtime,
    )
    task = engine.create_task(goal="g", policy_name="scripted")

    client.complete(_req("claude-opus-4-8"), _ctx(task.task_id))
    client.complete(_req("claude-opus-4-8"), _ctx(task.task_id))

    folded = fold(log, cs, task.task_id)
    expected = price("claude-opus-4-8", u1) + price("claude-opus-4-8", u2)
    assert folded.governance.cost_usd == expected
    assert folded.governance.input_tokens == 1_000_000
    assert folded.governance.output_tokens == 1_000_000


# ---------------------------------------------------------------------------
# end-to-end: catalog pricing → fold → BudgetGuard.max_cost_usd deny
# ---------------------------------------------------------------------------


def test_budget_guard_denies_tool_call_after_real_cost_exceeds_max() -> None:
    """End-to-end: an expensive LLM round-trip (priced via the catalog and
    recorded into the EventLog) pushes GovernanceState.cost_usd past
    ``max_cost_usd`` so the following tool call is denied."""
    log = InMemoryEventLog()
    cs = InMemoryContentStore()
    disp = InMemoryDispatcher()
    log.bind_lease_registry(disp)
    runtime = ToolRuntime(event_log=log, content_store=cs)

    tool = FakeTool(name="echo", script={("a",): "out-a"})
    policy = StubScriptedPolicy(
        [
            ToolCallsDecision(
                calls=[ToolCall(tool_name="echo", arguments={"k": "a"}, call_id="c1")]
            ),
            FinishDecision(answer="done"),
        ]
    )
    hooks = HookManager()
    # 1 MTok opus input = $5.00 → set the ceiling below that.
    hooks.register(BudgetGuard(Budget(max_cost_usd=1.0)))

    engine = Engine(
        event_log=log,
        content_store=cs,
        composer=trivial_three_segment(cs),
        policy=policy,
        hooks=hooks,
        tools={"echo": tool},
        tool_runtime=runtime,
    )
    task = engine.create_task(goal="g", policy_name="scripted")

    # Record an expensive LLM round-trip BEFORE the engine step so the
    # accumulated cost is already over budget when the guard checks the call.
    expensive = RuntimeLLMClient(
        provider=FakeLLMProvider(responses=[_response(Usage(uncached=1_000_000))]),
        event_log=log,
        content_store=cs,
        pricing=price,
    )
    expensive.complete(_req("claude-opus-4-8"), _ctx(task.task_id))

    disp.enqueue(task.task_id)
    lease = disp.lease(worker_id="w")
    assert lease is not None
    finished = engine.run_one_step(task, lease_id=lease.lease_id)

    assert finished.status == "terminal"
    types = [e.type for e in log.read(task.task_id)]
    assert "ToolCallDenied" in types
    denied = next(e for e in log.read(task.task_id) if e.type == "ToolCallDenied")
    assert "max_cost_usd" in denied.payload.reason
