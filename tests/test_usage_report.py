"""The cost read model: what a task and its sub-agents cost, per model.

``LLMRequestFinished`` records each round-trip's usage, cost and latency, and
``LLMRequestStarted`` the model, but a host had no way to add them up across a
task tree or to tell a real ``$0`` from a model the catalog cannot price.
``Client.usage`` / ``QueryResult.usage`` fold those pairs into per-model rows
and totals and name every unpriced model.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from noeta.client.parts import catalog_is_priced
from noeta.client.usage import build_usage_report
from noeta.protocols.events import (
    BackgroundSubagentStartedPayload,
    LLMRequestFinishedPayload,
    LLMRequestStartedPayload,
    SubtaskSpawnedPayload,
    TaskCreatedPayload,
)
from noeta.protocols.values import ContentRef
from noeta.sdk import (
    Client,
    LLMResponse,
    ModelUsage,
    Options,
    TextBlock,
    Usage,
    UsageReport,
    query,
)
from noeta.sdk.testing import FakeLLMProvider
from noeta.storage.memory import InMemoryEventLog


PRICED = "claude-sonnet-5"
#: Catalogued without published rates.
RATELESS = "gpt-5.5-2026-04-24"
#: Not in the catalog at all — a gateway routing name.
GATEWAY = "gateway-internal-x"

_REF = ContentRef(hash="a" * 64, size=1, media_type="application/json")


class _Stream:
    def __init__(self, log: InMemoryEventLog, task_id: str) -> None:
        self.log = log
        self.task_id = task_id
        self._n = 0

    def emit(self, type_: str, payload: Any) -> None:
        self.log.emit(task_id=self.task_id, type=type_, payload=payload)

    def created(self, parent: str | None = None, *, background: bool | None = None) -> None:
        self.emit(
            "TaskCreated",
            TaskCreatedPayload(
                goal="g", policy_name="p", parent_task_id=parent, background=background
            ),
        )

    def call(
        self,
        model: str,
        usage: Usage,
        *,
        cost: float,
        latency: int,
        finish: bool = True,
        success: bool = True,
    ) -> None:
        self._n += 1
        call_id = f"{self.task_id}-call-{self._n}"
        self.emit(
            "LLMRequestStarted",
            LLMRequestStartedPayload(call_id=call_id, model=model, request_ref=_REF),
        )
        if finish:
            self.emit(
                "LLMRequestFinished",
                LLMRequestFinishedPayload(
                    call_id=call_id,
                    success=success,
                    cost_usd=cost,
                    latency_ms=latency,
                    usage=usage,
                ),
            )

    def spawn(self, child: str) -> None:
        self.emit(
            "SubtaskSpawned",
            SubtaskSpawnedPayload(subtask_id=child, agent_name="worker", goal="sub"),
        )

    def spawn_background(self, child: str) -> None:
        self.emit(
            "BackgroundSubagentStarted",
            BackgroundSubagentStartedPayload(
                subtask_id=child, agent_name="worker", goal="bg", call_id="toolu_bg"
            ),
        )


def _tree() -> InMemoryEventLog:
    """root (2 priced calls) → fg child (1 gateway call) → grandchild (1 priced
    call); root → background child (1 rateless call + 1 cut-off call).

    Two decoys hang off the root: a spawn whose stream never started, and a
    spawn record naming a stream whose genesis belongs to another parent.
    """
    log = InMemoryEventLog()
    root = _Stream(log, "root")
    root.created()
    root.call(
        PRICED,
        Usage(uncached=100, cache_read=1000, cache_write=50, output=200, reasoning_tokens=20),
        cost=0.01,
        latency=300,
    )
    root.spawn("fg")
    root.spawn_background("bg")
    root.spawn("never-started")
    root.spawn("impostor")
    root.call(PRICED, Usage(uncached=10, output=5), cost=0.02, latency=500)

    fg = _Stream(log, "fg")
    fg.created("root")
    fg.call(GATEWAY, Usage(uncached=7, output=3), cost=0.0, latency=100, success=False)
    fg.spawn("gc")

    gc = _Stream(log, "gc")
    gc.created("fg")
    gc.call(PRICED, Usage(uncached=1, cache_read=2, output=4), cost=0.005, latency=50)

    bg = _Stream(log, "bg")
    bg.created("root", background=True)
    bg.call(RATELESS, Usage(uncached=40, output=60, reasoning_tokens=30), cost=0.0, latency=900)
    bg.call(RATELESS, Usage(), cost=0.0, latency=0, finish=False)

    impostor = _Stream(log, "impostor")
    impostor.created("somebody-else")
    impostor.call(PRICED, Usage(uncached=999_999), cost=99.0, latency=1)
    return log


def _report(log: InMemoryEventLog, task_id: str = "root", **kw: Any) -> UsageReport:
    return build_usage_report(log, task_id, is_priced=catalog_is_priced, **kw)


def _row(report: UsageReport, model: str) -> ModelUsage:
    (row,) = [r for r in report.per_model if r.model == model]
    return row


def test_tree_rolls_up_per_model_across_foreground_background_and_depth() -> None:
    report = _report(_tree())

    assert report.task_id == "root"
    assert report.tasks == ("root", "fg", "bg", "gc")
    assert [r.model for r in report.per_model] == sorted([PRICED, RATELESS, GATEWAY])

    priced = _row(report, PRICED)
    assert priced == ModelUsage(
        model=PRICED,
        requests=3,
        unfinished_requests=0,
        input_tokens=(100 + 1000 + 50) + 10 + (1 + 2),
        output_tokens=200 + 5 + 4,
        cache_read_tokens=1000 + 2,
        cache_write_tokens=50,
        reasoning_tokens=20,
        cost_usd=pytest.approx(0.035),  # type: ignore[arg-type]
        latency_ms_total=850,
        latency_ms_max=500,
        priced=True,
    )

    gateway = _row(report, GATEWAY)
    assert (gateway.requests, gateway.input_tokens, gateway.output_tokens) == (1, 7, 3)
    assert gateway.priced is False

    rateless = _row(report, RATELESS)
    assert rateless.requests == 1
    assert rateless.unfinished_requests == 1
    assert rateless.reasoning_tokens == 30
    assert rateless.latency_ms_max == 900
    assert rateless.priced is False

    assert report.requests == 5
    assert report.unfinished_requests == 1
    assert report.input_tokens == 1163 + 7 + 40
    assert report.output_tokens == 209 + 3 + 60
    assert report.cache_read_tokens == 1002
    assert report.cache_write_tokens == 50
    assert report.reasoning_tokens == 50
    assert report.cost_usd == pytest.approx(0.035)
    assert report.latency_ms_total == 850 + 100 + 900
    assert report.latency_ms_max == 900
    assert report.unpriced_models == tuple(sorted([RATELESS, GATEWAY]))


def test_include_children_false_counts_only_the_task_itself() -> None:
    report = _report(_tree(), include_children=False)
    assert report.tasks == ("root",)
    assert [r.model for r in report.per_model] == [PRICED]
    assert report.requests == 2
    assert report.cost_usd == pytest.approx(0.03)
    assert report.unpriced_models == ()


def test_a_subtree_reports_from_its_own_root() -> None:
    report = _report(_tree(), "fg")
    assert report.tasks == ("fg", "gc")
    assert report.requests == 2
    assert report.unpriced_models == (GATEWAY,)


def test_unknown_task_and_task_without_calls_are_empty() -> None:
    log = _tree()
    assert _report(log, "nope") == UsageReport(task_id="nope")

    _Stream(log, "quiet").created()
    quiet = _report(log, "quiet")
    assert quiet.tasks == ("quiet",)
    assert quiet.per_model == ()
    assert quiet.requests == 0
    assert quiet.cost_usd == 0.0
    assert quiet.latency_ms_max == 0


def test_priced_check_resolves_aliases_and_flags_rateless_rows() -> None:
    assert catalog_is_priced(PRICED) is True
    assert catalog_is_priced("sonnet") is True
    assert catalog_is_priced(RATELESS) is False
    assert catalog_is_priced(GATEWAY) is False


# --- through the public surface ----------------------------------------------


def _end_turn(text: str) -> LLMResponse:
    return LLMResponse(
        stop_reason="end_turn",
        content=[TextBlock(text=text)],
        usage=Usage(uncached=120, cache_read=30, output=15),
    )


def _options() -> Options:
    return Options(
        system_prompt="you answer briefly",
        name="main",
        permission_mode="bypassPermissions",
    )


def test_client_usage_reports_a_real_turn(tmp_path: Path) -> None:
    client = Client(
        _options(),
        provider=FakeLLMProvider(responses=[_end_turn("done")]),
        workspace_dir=tmp_path,
        model=PRICED,
        multi_turn=False,
    )
    try:
        outcome = client.start(goal="say done")
        report = client.usage(outcome.task_id)
    finally:
        client.shutdown()

    assert report.tasks == (outcome.task_id,)
    (row,) = report.per_model
    assert row.model == PRICED
    assert row.priced is True
    assert row.requests == 1
    assert (row.input_tokens, row.cache_read_tokens, row.output_tokens) == (150, 30, 15)
    assert row.cost_usd > 0
    assert report.cost_usd == row.cost_usd
    assert report.unpriced_models == ()


def test_query_result_usage_survives_the_temporary_client(tmp_path: Path) -> None:
    result = query(
        _options(),
        "say done",
        provider=FakeLLMProvider(responses=[_end_turn("done")]),
        workspace_dir=tmp_path,
        model="stub-model",
    )
    report = result.usage()
    assert report.task_id == result.task_id
    assert report.requests == 1
    assert report.input_tokens == 150
    # The test sentinel is not catalogued: its $0 is flagged, not trusted.
    assert report.unpriced_models == ("stub-model",)
    assert report.per_model[0].priced is False
