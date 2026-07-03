# 在你的应用中嵌入引擎 { #embedding-the-engine-in-your-app }

你不需要运行 `python -m noeta.agent` 来使用 Noeta。SDK 让你可以直接在自己的 Python 应用中嵌入代理——无需服务器，无需 HTTP，只需 `import noeta.sdk`。

## 最小嵌入 { #minimal-embedding }

```python
from pathlib import Path
from noeta.sdk import Client, Options, query

# 1. Define your agent
options = Options(
    system_prompt="You are a code assistant for this repository.",
    name="main",
    allowed_tools=("read", "grep", "glob"),
    permission_mode="bypassPermissions",
)

# 2. Wire in a provider
from noeta.providers.openai_compat import OpenAICompatProvider
provider = OpenAICompatProvider(
    base_url="https://api.openai.com/v1",
    api_key="sk-...",
)

# 3. Run
envelopes = query(
    options,
    goal="Find all TODO comments in src/.",
    provider=provider,
    workspace_dir=Path("./my-project"),
    model="gpt-5.5",
)

# 4. Inspect results
for env in envelopes:
    if env.type == "TaskCompleted":
        print("Agent finished.")
```

## 使用 Client 多轮对话 { #multi-turn-with-client }

对于交互式会话（发送后续目标、检查消息历史、管理生命周期）：

```python
from noeta.sdk import Client, Options

client = Client(
    options,
    provider=provider,
    workspace_dir=Path("./my-project"),
    model="gpt-5.5",
    multi_turn=False,
)

try:
    # Start a conversation
    outcome = client.start(goal="Analyze this codebase.")
    task_id = outcome.task_id

    # Read the message history
    messages = client.messages(task_id)
    for msg in messages:
        if hasattr(msg, "role"):
            print(f"[{msg.role}]")

    # Send a follow-up
    client.send_goal(task_id, "Now refactor module X.")

    # Inspect the raw event stream
    events = client.events(task_id)
    for env in events:
        print(f"seq={env.seq} type={env.type}")
finally:
    client.shutdown()
```

## 添加自定义工具 { #adding-custom-tools }

```python
from noeta.protocols.tool import ToolContext, ToolResult
from noeta.sdk import tool

@tool(
    name="my_api_call",
    version="1",
    risk_level="low",
    input_schema={
        "type": "object",
        "properties": {"endpoint": {"type": "string"}},
        "required": ["endpoint"],
        "additionalProperties": False,
    },
)
def my_api_call(arguments: dict, ctx: ToolContext) -> ToolResult:
    """Call my internal API."""
    import requests
    resp = requests.get(f"https://api.example.com/{arguments['endpoint']}")
    return ToolResult(success=True, output=resp.text)

options = Options(
    system_prompt="Use my_api_call when you need data from our API.",
    name="main",
    allowed_tools=("read", my_api_call),
    permission_mode="bypassPermissions",
)
```

完整的 `@tool` 模式见[自定义工具](custom-tool.md)。

## 添加子代理 { #adding-sub-agents }

```python
from noeta.sdk import AgentDefinition

options = Options(
    system_prompt="You are a team lead. Delegate to specialists.",
    name="lead",
    agents={
        "coder": AgentDefinition(
            description="Writes and edits code.",
            prompt="You are a senior engineer.",
            tools=["read", "edit", "write"],
        ),
        "reviewer": AgentDefinition(
            description="Reviews code for issues.",
            prompt="You are a code reviewer.",
            tools=["read", "grep"],
        ),
    },
    permission_mode="bypassPermissions",
)
```

完整模式见[子代理委派](subagent-delegation.md)。

## 要点 { #key-points }

- **一切来自 `noeta.sdk`。** `Options`、`Client`、`query`、`tool`、`AgentDefinition`、`HostConfig`——一个导入接口。
- **Provider 是接线，不是身份。** 将其传递给 `query()` / `Client()`。永远不要把它烤进 `Options`。这保持你的代理可移植。
- **`shutdown()` 是必需的。** `Client` 管理后台线程和连接。始终在 `finally` 块中调用 `client.shutdown()`。
- **无需服务器。** SDK 在进程内运行引擎。仅当你想要 Web UI 时才使用 `python -m noeta.agent`。

## 来源 { #source }

- `examples/sdk_minimal.py` —— 纯 SDK 进程内演示
- `noeta.sdk` 公开接口：`packages/noeta-sdk/noeta/sdk/__init__.py`
- `Client` / `query`：`packages/noeta-sdk/noeta/client/client.py`
- `Options` / `AgentDefinition`：`packages/noeta-sdk/noeta/client/options.py`
- `HostConfig`：`packages/noeta-sdk/noeta/client/host_config.py`
- 另见：[第一个代理](first-agent.md)、[自定义工具](custom-tool.md)、[子代理委派](subagent-delegation.md)、[持久化存储](durable-storage.md)
