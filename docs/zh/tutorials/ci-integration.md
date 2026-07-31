# 在 CI 中运行 Noeta

agent 配方和其他代码一样会腐坏：一个改名的工具、一个变了的权限模式、一次 provider 切换。本教程把 Noeta 接进 CI 流水线，让坏掉的配方在构建时就失败，而不是在用户的请求里失败。

这里的一切都跑在离线的 `FakeLLMProvider` 上 —— 不需要 API key，不碰网络，也不会抖。最后一步展示在你确实需要时如何加一个真实 provider 的 job。

**前置条件：**[你的第一个 agent](first-agent.md)，以及一个带 CI 流水线的仓库（示例用 GitHub Actions）。

## 为什么 CI 里用假 provider

`FakeLLMProvider` 回放一列脚本化的 `LLMResponse` 对象。它不需要凭证、不做网络往返，而且每次运行都返回同样的东西 —— 所以一个失败的测试意味着你的接线坏了，而不是模型今天状态不好。

真实 provider 在 CI 里也能用（把网关 key 作为 secret 传进去），但每次推送都该跑的是脚本化的运行。

## 第 1 步：写一个冒烟测试

冒烟测试的意义在于：配方能编译、这一轮能跑、Task 能到达终态。创建 `tests/test_agent_smoke.py`：

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

运行它：

```bash
uv run pytest tests/test_agent_smoke.py -v
```

```
tests/test_agent_smoke.py::test_minimal_agent_runs PASSED                 [100%]
1 passed
```

## 第 2 步：测试一个自定义工具

对配方下断言只能证明 agent 编译成功了。要证明你的工具确实跑了，就把模型脚本成去调用它，然后对 `ToolCallStarted` 事件下断言：

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

因为整次运行就是一条被记录下来的事件流，你可能想要的每一条 CI 断言 —— 跑了哪些工具、哪些 Guard 拒绝了、用了多少轮 —— 都只是对 `result` 的一次列表推导。

## 第 3 步：接进 GitHub Actions

往 `.github/workflows/ci.yml` 里加一个 job：

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

没有服务、没有端口、没有前端构建：SDK 是进程内的，所以冒烟 job 就是一个普通的 Python 测试 job。

## 第 4 步：借用 Noeta 自己的关卡

Noeta 的 CI 跑下面这些检查。挑适合你项目的用：

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

`make check` 把覆盖率、mypy、命名和导入拓扑这几道关卡一起跑；`make lint` 是只做静态检查的快速子集。

## 第 5 步（可选）：一个真实 provider 的 job

当你需要真实的模型行为时 —— 提示词回归、工具调用格式 —— 加第二个从 secrets 读取凭证的 job：

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

用这些 secret 构建 provider，并把测试标记为 `live`，这样没有网关的检出会跳过它而不是失败：

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

在你自己的 `pyproject.toml` 里声明这个 marker，并决定它如何被选中。Noeta 的根配置默认排除 `live` 和 `install_smoke`（`addopts = "-ra -m 'not install_smoke and not live'"`），所以一个 live 测试只有在你用 `-m live` 主动要求时才会跑。无论如何都保留 `skipif` —— 正是它让这种"主动开启"的运行在没有网关的机器上降级为跳过，而不是报错。

## 下一步

- **构建你正在测试的那个 agent** —— [你的第一个 agent](first-agent.md)。
- **让 CI 指向你的网关** —— [切换 Provider](../how-to/swap-providers.md)。
- **理解账本记录了什么** —— [引擎与执行](../concepts/engine-execution.md)。
- **查阅这些测试替身** —— [SDK 参考](../reference/sdk.md)；`FakeLLMProvider` 住在 `noeta.sdk.testing`。
