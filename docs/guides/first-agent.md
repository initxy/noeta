# Your first agent

Run a minimal Noeta agent in-process using only `noeta.sdk`. No server, no
HTTP — just import the SDK, hand it a provider and a workspace, get back
the agent's answer.

## Minimal example

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

## Using Client for multi-turn

`query()` is one-shot. For a multi-turn conversation (send follow-up goals,
inspect message history), use `Client`:

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

## Key points

- **`Options` is identity.** Two `Options` with the same prompt/tools/name
  compile to the same agent spec, regardless of which provider is wired in.
- **Provider is wiring.** Pass it to `query()` or `Client()` — never bake it
  into `Options`. This is what makes your agent portable across vendors.
- **`permission_mode="bypassPermissions"`** disables the approval gate for
  high-risk tools. In production you'd typically use `"default"` and supply
  a `can_use_tool` callback (see [Permission gating](permission-gating.md)).
- **`allowed_tools`** accepts name strings (`"read"`, `"write"`, ...) or
  `@tool`-decorated closures (see [Custom tools](custom-tool.md)).

## Source

- `examples/minimal_agent.py` — one-shot `query()` demo
- `examples/sdk_minimal.py` — `Client` + `as_messages` demo
- `noeta.sdk.Options` — `packages/noeta-sdk/noeta/client/options.py`
- `noeta.sdk.query` / `noeta.sdk.Client` — `packages/noeta-sdk/noeta/client/client.py`
- `noeta.sdk.tool` — `packages/noeta-sdk/noeta/sdk/authoring.py`
