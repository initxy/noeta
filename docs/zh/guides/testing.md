# 离线测试

不用 API key、不连网也能测 agent：`FakeLLMProvider` 按脚本回放模型响应，每次运行都会把完整的事件日志交给你做断言。这些测试也可以放进 CI，每次提交都跑。

## 离线跑一轮

`FakeLLMProvider` 按顺序返回你给它的 `LLMResponse`：

```python
from noeta.sdk import LLMResponse, Options, TextBlock, Usage, query
from noeta.sdk.testing import FakeLLMProvider

provider = FakeLLMProvider(responses=[
    LLMResponse(
        stop_reason="end_turn",
        content=[TextBlock(text="Hello from Noeta.")],
        usage=Usage(uncached=1, output=1),
    ),
])

result = query(
    Options(system_prompt="You are concise.", allowed_tools=()),
    goal="Say hello.",
    provider=provider,
)

for env in result:                      # the raw event stream
    print(f"{env.seq:>3}  {env.type:<22}  actor={env.actor}")
for item in result.messages():          # the folded conversation
    print(item)
print(result.answer())                  # just the answer

types = [env.type for env in result]
assert types[0] == "TaskCreated" and types[-1] == "TaskCompleted"
assert result.answer() == "Hello from Noeta."
```

```
  0  TaskCreated             actor=engine
  1  AgentBound              actor=engine
  2  ModelBound              actor=engine
  3  ContextContentRecorded  actor=plugin:environment
  4  MessagesAppended        actor=engine
  5  TaskStarted             actor=engine
  6  ContextPlanComposed     actor=engine
  7  LLMRequestStarted       actor=llm
  8  LLMResponseRecorded     actor=llm
  9  LLMRequestFinished      actor=llm
 10  MessagesAppended        actor=engine
 11  TaskSnapshot            actor=engine
 12  TaskCompleted           actor=engine
UserMessage(text='Say hello.')
AssistantMessage(text='Hello from Noeta.')
Result(answer='Hello from Noeta.', status='completed')
Hello from Noeta.
```

`query` 返回一个 `QueryResult`，它本身就是事件列表，另外还提供两种读法：

| 读法 | 得到什么 | 适合做什么 |
| --- | --- | --- |
| 直接遍历 `result` | 按 `seq` 排好的 `EventEnvelope` | 断言到底发生了什么 |
| `result.messages()` | 对话内容 | 给用户展示过程 |
| `result.answer()` | 最终答案 | 大多数场景 |

任务没有正常完成时，`answer()` 会抛 `QueryFailedError`，失败的运行不会被当成正常答案蒙混过去。

## 断言工具被调用

脚本里安排一次工具调用，再检查 `ToolCallStarted` 事件：

```python
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
    input_schema={"type": "object", "properties": {}, "additionalProperties": False},
)
def ping(arguments: dict, ctx: ToolContext) -> ToolResult:
    return ToolResult(success=True, output="pong")


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

result = query(
    Options(system_prompt="Use the ping tool.", allowed_tools=(ping,)),
    goal="Ping.",
    provider=provider,
)

called = [e.payload.tool_name for e in result if e.type == "ToolCallStarted"]
assert called == ["ping"], called
assert result.answer() == "Pinged."
assert len(provider.received_requests) == 2
```

- 每条脚本响应只用一次。用完了还在要，就抛 `IndexError`，能抓出意料之外的循环调用。
- `provider.received_requests` 记下了 agent 发出的每个 `LLMRequest`，提示词和工具 schema 也可以一并检查。

## 测审批关卡

用 `Client` 驱动这一轮，检查高风险调用会停下来等审批、被拒绝的调用不会执行：

```python
from noeta.sdk import (
    NEXT_GOAL_WAKE_HANDLE, Client, LLMResponse, Options, TextBlock, ToolContext,
    ToolResult, ToolUseBlock, Usage, tool,
)
from noeta.sdk.testing import FakeLLMProvider


@tool(
    name="delete_record",
    version="1",
    risk_level="high",
    description="Delete a record by id.",
    input_schema={
        "type": "object",
        "properties": {"id": {"type": "string"}},
        "required": ["id"],
    },
)
def delete_record(arguments: dict, ctx: ToolContext) -> ToolResult:
    return ToolResult(success=True, output=f"deleted {arguments['id']}")


provider = FakeLLMProvider(responses=[
    LLMResponse(
        stop_reason="tool_use",
        content=[ToolUseBlock(call_id="d1", tool_name="delete_record",
                              arguments={"id": "42"})],
        usage=Usage(uncached=1, output=1),
    ),
    LLMResponse(
        stop_reason="end_turn",
        content=[TextBlock(text="Left it alone.")],
        usage=Usage(uncached=1, output=1),
    ),
])

options = Options(system_prompt="Manage records.", allowed_tools=(delete_record,))
with Client(options, provider=provider) as client:
    turn = client.start(goal="Delete record 42.")
    assert turn.status == "suspended" and turn.wake_handle == "approval-d1"

    turn = client.deny(turn.task_id, call_id="d1", reason="not today")
    assert turn.wake_handle == NEXT_GOAL_WAKE_HANDLE
    ran = [e for e in client.events(turn.task_id) if e.type == "ToolCallStarted"]
    assert ran == [], "a denied call must never run"
```

这里的 `call_id` 是你自己定的，所以 wake handle 可以预知：`approval-{call_id}`。

## 放进 CI

把测试放在 `tests/` 下用 pytest 跑。SDK 在进程内运行，CI 里就是一个普通的 Python 任务：

```yaml
  agent-tests:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v6
        with:
          enable-cache: true
      - run: uv sync --frozen
      - run: uv run pytest tests/ -v
```

## 加一个真模型任务

要防提示词回归，得用真模型测。给这类测试打上标记，只在明确要求时才跑，没有密钥就跳过：

```python
import os

import pytest

from noeta.sdk import Options, query
from noeta.sdk.providers import AnthropicProvider


@pytest.mark.live
@pytest.mark.skipif(not os.environ.get("ANTHROPIC_API_KEY"), reason="needs ANTHROPIC_API_KEY")
def test_agent_answers():
    result = query(Options(system_prompt="Be brief.", allowed_tools=()),
                   goal="Reply with the word OK.",
                   provider=AnthropicProvider(), model="claude-sonnet-5")
    assert "OK" in str(result.answer())
```

在自己的 `pyproject.toml` 里声明这个标记并默认排除（`addopts = "-m 'not live'"`），再单独开一个 CI 任务跑 `pytest -m live`，密钥从 secret 传进去。

::: tip 流式输出
`noeta.sdk.testing` 里还有 `FakeStreamingLLMProvider`，用来测试消费逐 token 增量输出的代码。
:::

## 下一步

- [自定义工具](tools.md)
- [类型与测试替身](../reference/types.md)
- [事件日志怎么工作](../how-it-works/event-log.md)
