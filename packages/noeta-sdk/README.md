# noeta-sdk

The thin in-process **client surface** (`noeta.sdk` facade — `query` / `Client`
/ `Options` / `tool` / extension interfaces) over the
[noeta-runtime](https://github.com/initxy/noeta) kernel, plus **the built-in
plugin catalogue** (`noeta.builtins` — every official capability
implementation: the fs/web tool packs, provider adapters, guards, reminders,
memory, browser, app, MCP, sandbox backends, skills, the ReAct policy) and
the official presets. Like
claude-agent-sdk / LangChain: `import noeta.sdk`, run an agent in-process; no
engine internals, no HTTP server.

Part of the [Noeta](https://github.com/initxy/noeta) workspace. Apache-2.0.

## Install

```bash
pip install noeta-sdk      # noeta-runtime comes along as a transitive dependency
```

Then `import noeta.sdk` — that single module is the whole public surface. Python 3.11+.

## Quickstart — zero credentials

No API key, no network: drive one turn with the deterministic offline provider
from `noeta.sdk.testing`.

```python
import tempfile
from pathlib import Path

from noeta.sdk import Options, query, LLMResponse, TextBlock, Usage
from noeta.sdk.testing import FakeLLMProvider

provider = FakeLLMProvider(
    responses=[
        LLMResponse(
            stop_reason="end_turn",
            content=[TextBlock(text="Hello from a Noeta SDK agent!")],
            usage=Usage(uncached=1, output=1),
        )
    ]
)

with tempfile.TemporaryDirectory() as tmp:
    result = query(
        Options(
            system_prompt="You are a concise assistant.",
            allowed_tools=("read",),
            permission_mode="bypassPermissions",
        ),
        goal="Say hello.",
        provider=provider,
        workspace_dir=Path(tmp),
        model="stub-model",
    )
    assert result.answer() == "Hello from a Noeta SDK agent!"
```

`query` returns the full event-envelope stream for the turn; `result.answer()`
reads the answer off the terminal envelope. Swap the `FakeLLMProvider` for a live
adapter from `noeta.sdk.providers` (`AnthropicProvider` / `OpenAICompatProvider`)
to connect a real model — the provider is wiring, not agent identity.

## Learn more

- Runnable [`examples/`](https://github.com/initxy/noeta/tree/main/examples) —
  a minimal agent, custom tools, an in-process MCP server, a permission gate,
  provider swapping, sub-agent delegation, and surviving `kill -9` mid-task.
- [Documentation](https://initxy.github.io/noeta/tutorials/first-agent/) —
  tutorials, how-to guides, and the SDK reference.
