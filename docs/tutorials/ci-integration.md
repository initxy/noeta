# Run Noeta in CI

Agent recipes rot like any other code: a renamed tool, a changed permission
mode, a provider swap. This tutorial wires Noeta into a CI pipeline so a broken
recipe fails a build instead of a user's request.

Everything here runs against the offline `FakeLLMProvider` — no API key, no
network, no flakiness. The last step shows how to add a real-provider job when
you need one.

**Prerequisites:** [Your first agent](first-agent.md), and a repository with a
CI pipeline (the examples use GitHub Actions).

## Why a fake provider in CI

`FakeLLMProvider` replays a scripted list of `LLMResponse` objects. It needs no
credentials, does no network round-trips, and returns the same thing every run —
so a failing test means your wiring broke, not that a model had an off day.

A real provider works in CI too (pass the gateway key as a secret), but scripted
runs are what belong on every push.

## Step 1: Write a smoke test

The point of a smoke test is that the recipe compiles, the turn runs, and the
task reaches a terminal. Create `tests/test_agent_smoke.py`:

```python
"""Smoke test: run the minimal agent recipe end-to-end with the fake provider."""

import tempfile
from pathlib import Path

from noeta.sdk import LLMResponse, Options, TextBlock, Usage, query
from noeta.sdk.testing import FakeLLMProvider


def test_minimal_agent_runs():
    options = Options(
        system_prompt="You are a concise assistant.",
        name="main",
        allowed_tools=("read",),
        permission_mode="bypassPermissions",
    )

    provider = FakeLLMProvider(responses=[
        LLMResponse(
            stop_reason="end_turn",
            content=[TextBlock(text="Smoke test passed.")],
            usage=Usage(uncached=1, output=1),
        )
    ])

    with tempfile.TemporaryDirectory(prefix="noeta-ci-smoke-") as tmp:
        result = query(options, goal="Say hello.", provider=provider,
                       workspace_dir=Path(tmp), model="stub-model")

    # `result` IS the envelope list, so stream-level assertions work directly.
    types = [env.type for env in result]
    assert "TaskCreated" in types
    assert "TaskCompleted" in types

    # `.answer()` raises QueryFailedError if the task did not complete, so a
    # failed run can never masquerade as a passing assertion.
    assert "Smoke test passed" in str(result.answer())
```

Run it:

```bash
uv run pytest tests/test_agent_smoke.py -v
```

```
tests/test_agent_smoke.py::test_minimal_agent_runs PASSED                 [100%]
1 passed
```

## Step 2: Test a custom tool

A recipe assertion proves the agent compiled. To prove your tool actually ran,
script the model into calling it and assert on the `ToolCallStarted` events:

```python
"""Smoke test: the custom tool gets called."""

import tempfile
from pathlib import Path

from noeta.sdk import (
    LLMResponse, Options, TextBlock, ToolContext, ToolResult, ToolUseBlock,
    Usage, query, tool,
)
from noeta.sdk.testing import FakeLLMProvider


@tool(
    name="ping",
    version="1",
    risk_level="low",
    description="Return pong.",
    input_schema={"type": "object", "properties": {},
                  "additionalProperties": False},
)
def ping(arguments: dict, ctx: ToolContext) -> ToolResult:
    return ToolResult(success=True, output="pong")


def test_custom_tool_called():
    options = Options(
        system_prompt="Use the ping tool.",
        name="tester",
        allowed_tools=(ping,),
        permission_mode="bypassPermissions",
    )

    provider = FakeLLMProvider(responses=[
        LLMResponse(
            stop_reason="tool_use",
            content=[ToolUseBlock(call_id="p1", tool_name="ping", arguments={})],
            usage=Usage(uncached=1, output=1),
        ),
        LLMResponse(
            stop_reason="end_turn",
            content=[TextBlock(text="Pinged.")],
            usage=Usage(uncached=1, output=1),
        ),
    ])

    with tempfile.TemporaryDirectory() as tmp:
        result = query(options, goal="Ping.", provider=provider,
                       workspace_dir=Path(tmp), model="stub-model")

    called = [e.payload.tool_name for e in result if e.type == "ToolCallStarted"]
    assert called == ["ping"], f"expected ping, got {called}"
```

Because the whole run is a recorded event stream, every CI assertion you might
want — which tools ran, which guards denied, how many turns it took — is a list
comprehension over `result`.

## Step 3: Wire it into GitHub Actions

Add a job to `.github/workflows/ci.yml`:

```yaml
  agent-smoke:
    name: Agent recipe smoke tests
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Install uv
        uses: astral-sh/setup-uv@v3
        with:
          enable-cache: true

      - name: Sync workspace
        run: uv sync --frozen

      - name: Run agent smoke tests
        run: uv run pytest tests/test_agent_smoke.py -v
```

No services, no ports, no frontend build: the SDK is in-process, so a smoke job
is a plain Python test job.

## Step 4: Borrow Noeta's own gates

Noeta's CI runs the checks below. Take whichever fit your project:

```bash
# Test suite with coverage
uv run pytest --cov=noeta --cov-report=term --cov-fail-under=85

# Fresh-venv two-wheel install smoke (opt-in via the install_smoke marker)
uv run pytest -v -m install_smoke tests/test_install_smoke.py

# Naming lint (forbidden class names per CONTEXT.md)
uv run python scripts/lint-naming.py

# Import topology lint (the layer boundaries in .importlinter)
uv run lint-imports --config .importlinter

# mypy strict on protocol definitions
MYPYPATH=packages/noeta-runtime \
  uv run mypy --strict \
    --namespace-packages --explicit-package-bases \
    packages/noeta-runtime/noeta/protocols
```

`make check` runs the coverage, mypy, naming, and import-topology gates
together; `make lint` is the fast static-only subset.

## Step 5 (optional): A real-provider job

When you need real model behaviour — prompt regressions, tool-calling
formats — add a second job that reads credentials from secrets:

```yaml
  agent-integration:
    name: Agent integration (real provider)
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v3
        with:
          enable-cache: true
      - name: Sync
        run: uv sync --frozen
      - name: Run integration tests
        env:
          LLM_BASE_URL: ${{ secrets.LLM_BASE_URL }}
          LLM_API_KEY: ${{ secrets.LLM_API_KEY }}
          LLM_MODEL: ${{ secrets.LLM_MODEL }}
        run: uv run pytest tests/test_integration.py -v -m live
```

Build the provider from those secrets and mark the test `live`, so a
gateway-less checkout skips it instead of failing:

```python
import os

import pytest

from noeta.sdk.providers import OpenAICompatProvider


@pytest.mark.live
@pytest.mark.skipif(
    not os.environ.get("LLM_API_KEY"),
    reason="needs LLM_BASE_URL / LLM_API_KEY / LLM_MODEL",
)
def test_agent_with_real_llm():
    provider = OpenAICompatProvider(
        base_url=os.environ["LLM_BASE_URL"],
        api_key=os.environ["LLM_API_KEY"],
    )
    ...  # drive query(..., provider=provider, model=os.environ["LLM_MODEL"])
```

Declare the marker in your own `pyproject.toml` and decide how it is selected.
Noeta's root config excludes both `live` and `install_smoke` by default
(`addopts = "-ra -m 'not install_smoke and not live'"`), so a live test runs only
when you ask for it with `-m live`. Keep the `skipif` regardless — it is what
makes the opt-in run degrade to a skip rather than an error on a machine with no
gateway.

## Next steps

- **Build the agent you are testing** — [Your first agent](first-agent.md).
- **Point CI at your gateway** — [Swap providers](../how-to/swap-providers.md).
- **Understand what the ledger records** — [Engine & execution](../concepts/engine-execution.md).
- **Look up the doubles** — [SDK reference](../reference/sdk.md);
  `FakeLLMProvider` lives at `noeta.sdk.testing`.
