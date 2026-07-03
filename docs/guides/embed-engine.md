# Embedding the engine in your app

You don't need to run `python -m noeta.agent` to use Noeta. The SDK
lets you embed an agent directly in your own Python application — no
server, no HTTP, just `import noeta.sdk`.

## Minimal embedding

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

## Multi-turn with Client

For interactive sessions (send follow-up goals, inspect message
history, manage lifecycle):

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

## Adding custom tools

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

See [Custom tools](custom-tool.md) for the full `@tool` pattern.

## Adding sub-agents

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

See [Sub-agent delegation](subagent-delegation.md) for the full pattern.

## Key points

- **Everything comes from `noeta.sdk`.** `Options`, `Client`, `query`,
  `tool`, `AgentDefinition`, `HostConfig` — one import surface.
- **Provider is wiring, not identity.** Pass it to `query()` / `Client()`.
  Never bake it into `Options`. This keeps your agent portable.
- **`shutdown()` is required.** `Client` manages background threads and
  connections. Always call `client.shutdown()` in a `finally` block.
- **No server needed.** The SDK runs the engine in-process. Use
  `python -m noeta.agent` only when you want the web UI.

## Source

- `examples/sdk_minimal.py` — pure-SDK in-process demo
- `noeta.sdk` public surface: `packages/noeta-sdk/noeta/sdk/__init__.py`
- `Client` / `query`: `packages/noeta-sdk/noeta/client/client.py`
- `Options` / `AgentDefinition`: `packages/noeta-sdk/noeta/client/options.py`
- `HostConfig`: `packages/noeta-sdk/noeta/client/host_config.py`
- See also: [Your first agent](first-agent.md),
  [Custom tools](custom-tool.md),
  [Sub-agent delegation](subagent-delegation.md),
  [Durable storage](durable-storage.md)
