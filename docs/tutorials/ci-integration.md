# Tutorial: CI integration with Noeta

Run Noeta in your CI pipeline to smoke-test agent recipes, validate
custom tools, or automate code review. This tutorial wires Noeta into
GitHub Actions using the offline `FakeLLMProvider` — no API key needed.

## Why a fake provider in CI?

`FakeLLMProvider` is a scripted, offline LLM double: it answers with
pre-scripted responses, needs no API key, is deterministic, and does no network
round-trips. A real provider works in CI too (pass the gateway key as a
secret), but the fake is the right starting point for smoke tests.

## Step 1: Write a smoke test

Create `tests/test_agent_smoke.py`:

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

    provider = FakeLLMProvider(
        responses=[
            LLMResponse(
                stop_reason="end_turn",
                content=[TextBlock(text="Smoke test passed.")],
                usage=Usage(uncached=1, output=1),
            )
        ]
    )

    with tempfile.TemporaryDirectory(prefix="noeta-ci-smoke-") as tmp:
        result = query(
            options,
            goal="Say hello.",
            provider=provider,
            workspace_dir=Path(tmp),
            model="stub-model",
        )

    # ``result`` IS the envelope list, so stream-level assertions still work.
    types = [env.type for env in result]
    assert "TaskCreated" in types
    assert "TaskCompleted" in types

    # ``.answer()`` raises QueryFailedError if the task did not complete, so a
    # failed run can never masquerade as a passing assertion.
    assert "Smoke test passed" in str(result.answer())
```

Run it locally:

```bash
uv run pytest tests/test_agent_smoke.py -v
```

## Step 2: Test a custom tool

If your agent uses custom tools, test that they're wired correctly:

```python
"""Smoke test: custom tool gets called."""

import tempfile
from pathlib import Path

from noeta.sdk import (
    LLMResponse,
    Options,
    TextBlock,
    ToolContext,
    ToolResult,
    ToolUseBlock,
    Usage,
    query,
    tool,
)
from noeta.sdk.testing import FakeLLMProvider


@tool(
    name="ping",
    version="1",
    risk_level="low",
    input_schema={
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
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

    provider = FakeLLMProvider(
        responses=[
            LLMResponse(
                stop_reason="tool_use",
                content=[
                    ToolUseBlock(
                        call_id="p1",
                        tool_name="ping",
                        arguments={},
                    )
                ],
                usage=Usage(uncached=1, output=1),
            ),
            LLMResponse(
                stop_reason="end_turn",
                content=[TextBlock(text="Pinged.")],
                usage=Usage(uncached=1, output=1),
            ),
        ]
    )

    with tempfile.TemporaryDirectory() as tmp:
        envelopes = list(query(
            options,
            goal="Ping.",
            provider=provider,
            workspace_dir=Path(tmp),
        ))

    tool_calls = [
        e.payload.tool_name
        for e in envelopes
        if e.type == "ToolCallStarted"
    ]
    assert "ping" in tool_calls, f"Expected ping in {tool_calls}"
```

## Step 3: Wire into GitHub Actions

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

SDK smoke tests exercise the library in-process — no server and no
frontend build.

## Step 4: Run the full test suite in CI

Noeta's own CI runs these checks. Reference them for your own pipeline:

```bash
# Core test suite with coverage
uv run pytest --cov=noeta --cov-report=term --cov-fail-under=85

# Fresh-venv two-wheel install smoke (opt-in via the install_smoke marker)
uv run pytest -v -m install_smoke tests/test_install_smoke.py

# Naming lint (forbidden class names per CONTEXT.md)
uv run python scripts/lint-naming.py

# Import topology lint (L0..L3 layer boundaries)
uv run lint-imports --config .importlinter

# mypy strict on protocol definitions
MYPYPATH=packages/noeta-runtime \
  uv run mypy --strict \
    --namespace-packages --explicit-package-bases \
    packages/noeta-runtime/noeta/protocols
```

`make check` runs the coverage, mypy, and lint gates together.

## Step 5: Using a real provider in CI (optional)

When you need a real model in CI (e.g. for integration tests against
actual LLM behaviour):

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

Build the provider from those secrets and mark the test `live` so a
gateway-less run skips it:

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

## Key points

- **Fake provider for smoke tests.** `FakeLLMProvider` is exported from
  `noeta.sdk.testing` — the public home for offline doubles. No secrets,
  no network.
- **`uv run pytest`** is the test entry point; the workspace-root
  `pyproject.toml` sets `testpaths = ["tests"]`.
- **The `live` marker does not skip on its own.** It is declared in
  `pyproject.toml`, but the default `addopts` excludes only `install_smoke`.
  Gate a live test with `@pytest.mark.skipif` on the required env vars so it
  self-skips when the gateway is absent, or select with `-m "not live"`.

## Source

- `.github/workflows/ci.yml` — the repo's own CI pipeline
- `Makefile` — `make install`, `make test`, `make lint`, `make check`
- `pyproject.toml` — pytest config (`testpaths`, `markers`, `addopts`)
- `packages/noeta-runtime/noeta/testing/fake_llm.py` — `FakeLLMProvider`,
  re-exported at `noeta.sdk.testing`
- See also: [Your first agent](first-agent.md),
  [Swap providers](../how-to/swap-providers.md),
  [Engine & execution](../concepts/engine-execution.md)
