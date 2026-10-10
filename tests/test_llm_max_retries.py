"""``HostConfig.llm_max_retries`` sets the client's transient-retry budget.

Two clients over one store can need different budgets — a worker that should
ride out one gateway 5xx, an interactive client that keeps the default — so the
budget is per client and reaches the one ``RuntimeLLMClient`` every turn's
Engine records its model calls through.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from noeta.protocols.errors import TransientError
from noeta.protocols.messages import LLMRequest
from noeta.sdk import Client, HostConfig, LLMResponse, Options, TextBlock, Usage


class _FlakyProvider:
    """Fails the first ``failures`` calls with a 5xx-shaped transient error."""

    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.calls = 0

    def complete(self, request: LLMRequest) -> LLMResponse:  # noqa: ARG002
        self.calls += 1
        if self.calls <= self.failures:
            # retry_after=0 keeps the backoff off the wall clock.
            raise TransientError("502 bad gateway", retry_after=0.0)
        return LLMResponse(
            stop_reason="end_turn",
            content=[TextBlock(text="done")],
            usage=Usage(uncached=1, output=1),
        )


def _run(tmp_path: Path, provider: _FlakyProvider, **hc: object) -> tuple[str, int]:
    client = Client(
        Options(system_prompt="be brief", permission_mode="bypassPermissions"),
        provider=provider,
        workspace_dir=tmp_path,
        model="stub-model",
        multi_turn=False,
        host_config=HostConfig(**hc),  # type: ignore[arg-type]
    )
    try:
        outcome = client.start(goal="go")
        events = client.events(outcome.task_id)
        retries = sum(1 for env in events if env.type == "LLMRetryScheduled")
        return events[-1].type, retries
    finally:
        client.shutdown()


def test_the_budget_is_spent_before_the_step_fails(tmp_path: Path) -> None:
    provider = _FlakyProvider(failures=2)
    status, retries = _run(tmp_path, provider, llm_max_retries=2)
    assert status == "TaskCompleted"
    assert retries == 2
    assert provider.calls == 3


def test_a_budget_too_small_fails_the_step(tmp_path: Path) -> None:
    provider = _FlakyProvider(failures=2)
    status, retries = _run(tmp_path, provider, llm_max_retries=1)
    assert status == "TaskFailed"
    assert retries == 1
    assert provider.calls == 2


def test_zero_tries_each_call_once(tmp_path: Path) -> None:
    provider = _FlakyProvider(failures=1)
    status, retries = _run(tmp_path, provider, llm_max_retries=0)
    assert status == "TaskFailed"
    assert retries == 0
    assert provider.calls == 1


def test_unset_keeps_the_runtime_default(tmp_path: Path) -> None:
    provider = _FlakyProvider(failures=3)
    status, retries = _run(tmp_path, provider)
    assert status == "TaskCompleted"
    assert retries == 3


@pytest.mark.parametrize("bad", [-1, True, 1.5])
def test_a_meaningless_budget_is_refused(bad: object) -> None:
    with pytest.raises(ValueError, match="llm_max_retries"):
        HostConfig(llm_max_retries=bad)  # type: ignore[arg-type]
