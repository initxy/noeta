# 第一个代理 { #your-first-agent }

仅使用 `noeta.sdk` 在进程内运行一个最小的 Noeta 代理。无需服务器，无需 HTTP——只需导入 SDK，交给它一个 provider 和一个工作区，就能得到代理的回答。

## 最小示例 { #minimal-example }

```python
import tempfile
from pathlib import Path

from noeta.sdk import Client, Options, query

# --- 1. Build the agent recipe ----------------------------------------------
#
# Options is the declarative recipe: system prompt, which tools to open,
# permission strategy. "name" is the agent's identity label (defaults to
# "main"). "allowed_tools" accepts built-in tool name strings — here we
# open just "read" so the agent can look at files.

options = Options(
    system_prompt="You are a concise assistant.",
    name="main",
    allowed_tools=("read",),
    permission_mode="bypassPermissions",
)

# --- 2. Wire in a provider + workspace --------------------------------------
#
# The provider is *wiring*, injected at query time (not part of the recipe
# identity). For a real run, swap _demo_provider() for:
#
#   from noeta.providers.openai_compat import OpenAICompatProvider
#   provider = OpenAICompatProvider(base_url=..., api_key=...)
#
# or:
#
#   from noeta.providers.anthropic import AnthropicProvider
#   provider = AnthropicProvider(api_key=..., default_max_tokens=1024)

from noeta.protocols.messages import LLMResponse, TextBlock, Usage
from noeta.testing.fake_llm import FakeLLMProvider

provider = FakeLLMProvider(
    responses=[
        LLMResponse(
            stop_reason="end_turn",
            content=[TextBlock(text="Hello from a minimal Noeta agent!")],
            usage=Usage(uncached=1, output=1),
        )
    ]
)

# --- 3. Run one turn --------------------------------------------------------
#
# query() returns an iterable of EventEnvelope records — the canonical,
# machine-readable record of everything the agent did. The final answer
# rides the terminal TaskCompleted envelope.

with tempfile.TemporaryDirectory(prefix="noeta-first-") as tmp:
    envelopes = query(
        options,
        goal="Say hello.",
        provider=provider,
        workspace_dir=Path(tmp),
        model="stub-model",
    )

    # Extract the answer from the TaskCompleted payload.
    from noeta.protocols.events import TaskCompletedPayload, answer_from_payload
    from noeta.storage.memory import InMemoryContentStore

    store = InMemoryContentStore()
    answer = ""
    for env in envelopes:
        if env.type == "TaskCompleted":
            assert isinstance(env.payload, TaskCompletedPayload)
            answer = str(answer_from_payload(env.payload, store))

    print(f"agent answer: {answer!r}")
```

## 使用 Client 进行多轮对话 { #using-client-for-multi-turn }

`query()` 是一次性的。对于多轮对话（发送后续目标、检查消息历史），使用 `Client`：

```python
from noeta.sdk import Client, Options

options = Options(
    system_prompt="You are a helpful assistant.",
    name="main",
    allowed_tools=("read",),
    permission_mode="bypassPermissions",
)

client = Client(
    options,
    provider=provider,
    workspace_dir=Path(tmp),
    model="stub-model",
    multi_turn=False,
)

try:
    outcome = client.start(goal="Say hello.")
    # outcome.task_id identifies the conversation
    messages = client.messages(outcome.task_id)
    # messages is a list of UserMessage / AssistantMessage / ToolUse / ToolResultView
    for msg in messages:
        print(msg)
finally:
    client.shutdown()
```

## 要点 { #key-points }

- **`Options` 即身份。** 两个具有相同 prompt/tools/name 的 `Options` 编译为相同的代理 spec，与接入的 provider 无关。
- **Provider 是接线。** 将其传递给 `query()` 或 `Client()`——永远不要把它烤进 `Options`。这正是你的代理可以跨供应商移植的原因。
- **`permission_mode="bypassPermissions"`** 禁用高风险工具的批准门控。在生产环境中，你通常会使用 `"default"` 并提供一个 `can_use_tool` 回调（见[权限门控](permission-gating.md)）。
- **`allowed_tools`** 接受名称字符串（`"read"`、`"write"`、……）或 `@tool` 装饰的闭包（见[自定义工具](custom-tool.md)）。

## 来源 { #source }

- `examples/minimal_agent.py` —— 一次性 `query()` 演示
- `examples/sdk_minimal.py` —— `Client` + `as_messages` 演示
- `noeta.sdk.Options` —— `packages/noeta-sdk/noeta/client/options.py`
- `noeta.sdk.query` / `noeta.sdk.Client` —— `packages/noeta-sdk/noeta/client/client.py`
- `noeta.sdk.tool` —— `packages/noeta-sdk/noeta/sdk/authoring.py`
