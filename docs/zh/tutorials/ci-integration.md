# 教程：与 Noeta 的 CI 集成

在你的 CI 管道中运行 Noeta，用于对代理配方做冒烟测试、验证自定义工具，或自动化代码审查。本教程用离线的 `FakeLLMProvider` 把 Noeta 接入 GitHub Actions——无需 API key。

## 为什么在 CI 中用 fake provider？

`FakeLLMProvider` 是一个脚本化的离线 LLM 替身：它以预先脚本化的响应作答，不需要 API key，是确定性的，也不做任何网络往返。真实 provider 在 CI 中同样能用（把网关密钥作为 secret 传入），但对于冒烟测试，fake provider 才是正确的起点。

## 步骤 1：编写冒烟测试

创建 `tests/test_agent_smoke.py`：

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

本地运行：

```bash
uv run pytest tests/test_agent_smoke.py -v
```

## 步骤 2：测试自定义工具

如果你的代理使用了自定义工具，就要测试它们是否被正确接线：

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

## 步骤 3：接入 GitHub Actions

向 `.github/workflows/ci.yml` 添加一个 job：

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

SDK 冒烟测试在进程内运行库本身——不需要服务器，也不需要前端构建。

## 步骤 4：在 CI 中运行完整测试套件

Noeta 自己的 CI 就运行这些检查。为你自己的管道参考它们：

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

`make check` 把 coverage、mypy 和 lint 这几道闸门一起跑。

## 步骤 5：在 CI 中使用真实 provider（可选）

当你需要 CI 中的真实模型时（例如针对真实 LLM 行为做集成测试）：

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

用这些 secret 构建 provider，并把测试标记为 `live`，让没有网关的运行跳过它：

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

## 要点

- **冒烟测试用 fake provider。** `FakeLLMProvider` 从 `noeta.sdk.testing` 导出——离线替身的公开归宿。无 secret，无网络。
- **`uv run pytest`** 是测试入口点；工作区根目录的 `pyproject.toml` 设置了 `testpaths = ["tests"]`。
- **`live` 标记不会自己跳过。** 它在 `pyproject.toml` 中声明，但默认的 `addopts` 只排除了 `install_smoke`。用 `@pytest.mark.skipif` 依据所需环境变量门控一个 live 测试，让它在网关缺失时自动跳过；或者用 `-m "not live"` 选择。

## 来源

- `.github/workflows/ci.yml` —— 仓库自己的 CI 管道
- `Makefile` —— `make install`、`make test`、`make lint`、`make check`
- `pyproject.toml` —— pytest 配置（`testpaths`、`markers`、`addopts`）
- `packages/noeta-runtime/noeta/testing/fake_llm.py` —— `FakeLLMProvider`，在 `noeta.sdk.testing` 处再导出
- 另见：[你的第一个代理](first-agent.md)、
  [切换 provider](../how-to/swap-providers.md)、
  [Engine 与执行](../concepts/engine-execution.md)
