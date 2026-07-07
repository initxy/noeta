# Tutorial: CI integration with Noeta

Run Noeta in your CI pipeline to smoke-test agent recipes, validate
custom tools, or automate code review. This tutorial shows how to wire
Noeta into GitHub Actions using the offline stub provider — no API key
needed.

## Why stub in CI?

The `stub` provider is a scripted, offline double that answers with
pre-scripted responses. It's perfect for CI because:

- **No API key required.** Your CI never needs secrets for LLM access.
- **Deterministic.** Same inputs always produce the same outputs.
- **Fast.** No network round-trips.

You can also use a real provider in CI (pass `NOETA_AGENT_API_KEY` as a
secret), but the stub is the right starting point for smoke tests.

## Step 1: Write a smoke test

Create `tests/test_agent_smoke.py`:

```python
"""Smoke test: run the minimal agent recipe end-to-end with the stub provider."""

import tempfile
from pathlib import Path

from noeta.protocols.events import TaskCompletedPayload, answer_from_payload
from noeta.protocols.messages import LLMResponse, TextBlock, Usage
from noeta.sdk import Options, query
from noeta.storage.memory import InMemoryContentStore
from noeta.testing.fake_llm import FakeLLMProvider


def test_minimal_agent_runs():
    """The main recipe should produce a TaskCompleted envelope."""

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
        envelopes = list(query(
            options,
            goal="Say hello.",
            provider=provider,
            workspace_dir=Path(tmp),
            model="stub-model",
        ))

    # Verify we got a terminal state
    types = [env.type for env in envelopes]
    assert "TaskCreated" in types, "Agent should create a task"
    assert "TaskCompleted" in types, "Agent should reach terminal state"

    # Verify the answer is extractable
    store = InMemoryContentStore()
    answer = ""
    for env in envelopes:
        if env.type == "TaskCompleted":
            assert isinstance(env.payload, TaskCompletedPayload)
            answer = str(answer_from_payload(env.payload, store))

    assert "Smoke test passed" in answer, f"Unexpected answer: {answer}"
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

from noeta.protocols.messages import (
    LLMResponse, TextBlock, ToolUseBlock, Usage,
)
from noeta.protocols.tool import ToolContext, ToolResult
from noeta.sdk import Options, query, tool
from noeta.testing.fake_llm import FakeLLMProvider


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

> **No frontend build needed.** The `noeta-agent` wheel on PyPI already
> bundles the built web UI, so `uv sync` pulls a ready-to-run package —
> no `npm` step required.

## Step 4: Run the full test suite in CI

Noeta's own CI runs these checks. Reference them for your own pipeline:

```bash
# Core test suite with coverage
uv run pytest --cov=noeta --cov-report=term --cov-fail-under=85

# Fresh-venv install smoke (verifies pip install paths)
uv run pytest -v -m install_smoke tests/test_install_smoke.py

# Naming lint (forbidden terms per CONTEXT.md)
uv run python scripts/lint-naming.py

# Import topology lint (L0..L3 layer boundaries)
uv run lint-imports --config .importlinter

# mypy strict on protocol definitions
MYPYPATH=packages/noeta-runtime \
  uv run mypy --strict \
    --namespace-packages --explicit-package-bases \
    packages/noeta-runtime/noeta/protocols
```

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
          NOETA_AGENT_PROVIDER: openai
          NOETA_AGENT_BASE_URL: ${{ secrets.LLM_BASE_URL }}
          NOETA_AGENT_API_KEY: ${{ secrets.LLM_API_KEY }}
          NOETA_AGENT_MODEL: ${{ secrets.LLM_MODEL }}
        run: uv run pytest tests/test_integration.py -v -m live
```

Mark live tests with the `@pytest.mark.live` decorator so they're
skipped by default (the repo's `pyproject.toml` configures this):

```python
import pytest

@pytest.mark.live
def test_agent_with_real_llm():
    ...  # needs NOETA_AGENT_API_KEY
```

## Key points

- **Stub provider for smoke tests.** `FakeLLMProvider` is in
  `noeta.testing` — the public home for offline doubles. No secrets,
  no network.
- **`uv run pytest`** is the test entry point. The workspace-root
  `pyproject.toml` configures `testpaths = ["tests"]`.
- **`@pytest.mark.live`** gates real-LLM tests so they don't run in
  default CI. Use `-m "not live"` to skip them (already the default
  in `pyproject.toml`).

## Source

- `.github/workflows/ci.yml` — the repo's own CI pipeline
- `Makefile` — `make install`, `make run`, `make serve`, `make web`, `make dev`
- `pyproject.toml` — pytest config (`testpaths`, `markers`)
- `noeta.testing.fake_llm.FakeLLMProvider` — `packages/noeta-runtime/noeta/testing/fake_llm.py`
- See also: [Your first agent](first-agent.md),
  [Swap providers](../how-to/swap-providers.md),
  [Engine & execution](../concepts/engine-execution.md)
