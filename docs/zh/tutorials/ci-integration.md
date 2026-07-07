# 教程：与 Noeta 的 CI 集成 { #tutorial-ci-integration-with-noeta }

在你的 CI 管道中运行 Noeta，以对代理配方进行冒烟测试、验证自定义工具，或自动化代码审查。本教程展示如何使用离线 stub provider 将 Noeta 接入 GitHub Actions——无需 API key。

## 为什么在 CI 中使用 stub { #why-stub-in-ci }

`stub` provider 是一个脚本化的离线替身，以预设脚本化响应回答。它非常适合 CI，因为：

- **无需 API key。** 你的 CI 永远不需要 LLM 访问的密钥。
- **确定性。** 相同的输入总是产生相同的输出。
- **快速。** 无网络往返。

你也可以在 CI 中使用真实 provider（将 `NOETA_AGENT_API_KEY` 作为密钥传递），但 stub 是冒烟测试的正确起点。

## 步骤 1：编写冒烟测试 { #step-1-write-a-smoke-test }

创建 `tests/test_agent_smoke.py`：

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

本地运行：

```bash
uv run pytest tests/test_agent_smoke.py -v
```

## 步骤 2：测试自定义工具 { #step-2-test-a-custom-tool }

如果你的代理使用自定义工具，测试它们是否正确接线：

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

## 步骤 3：接入 GitHub Actions { #step-3-wire-into-github-actions }

添加一个 job 到 `.github/workflows/ci.yml`：

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

> **无需构建前端。** PyPI 上的 `noeta-agent` wheel 已内置构建好的 web UI，`uv sync` 拉取的是即装即用的包——不需要 `npm` 步骤。

## 步骤 4：在 CI 中运行完整测试套件 { #step-4-run-the-full-test-suite-in-ci }

Noeta 自己的 CI 运行这些检查。为你自己的管道参考它们：

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

## 步骤 5：在 CI 中使用真实 provider（可选） { #step-5-using-a-real-provider-in-ci-optional }

当你需要 CI 中的真实模型时（例如针对实际 LLM 行为的集成测试）：

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

使用 `@pytest.mark.live` 装饰器标记实时测试，以便它们默认被跳过（仓库的 `pyproject.toml` 配置了这一点）：

```python
import pytest

@pytest.mark.live
def test_agent_with_real_llm():
    ...  # needs NOETA_AGENT_API_KEY
```

## 要点 { #key-points }

- **冒烟测试使用 stub provider。** `FakeLLMProvider` 位于 `noeta.testing`——离线替身的公开归宿。无密钥，无网络。
- **`uv run pytest`** 是测试入口点。工作区根目录的 `pyproject.toml` 配置了 `testpaths = ["tests"]`。
- **`@pytest.mark.live`** 门控真实 LLM 测试，以便它们不在默认 CI 中运行。使用 `-m "not live"` 跳过它们（已经是 `pyproject.toml` 中的默认值）。

## 来源 { #source }

- `.github/workflows/ci.yml` —— 仓库自己的 CI 管道
- `Makefile` —— `make install`、`make run`、`make serve`、`make web`、`make dev`
- `pyproject.toml` —— pytest 配置（`testpaths`、`markers`）
- `noeta.testing.fake_llm.FakeLLMProvider` —— `packages/noeta-runtime/noeta/testing/fake_llm.py`
- 另见：[第一个代理](/tutorials/first-agent)、[切换提供者](/how-to/swap-providers)、[Engine 与执行](/concepts/engine-execution)
