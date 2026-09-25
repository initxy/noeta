# noeta-sdk

**Agents that keep running.** A Python SDK for agents that survive crashes,
wait days for a human, and scale from one script to a cluster — without
changing the agent.

`noeta-sdk` is the package you import (`noeta.sdk`). It carries the client
API, every built-in capability (file and web tools, model adapters, memory,
MCP, sandboxes) and the official presets, over the
[noeta-runtime](https://pypi.org/project/noeta-runtime/) kernel. Part of
[Noeta](https://github.com/initxy/noeta). Apache-2.0.

## Install

```bash
pip install noeta-sdk      # noeta-runtime comes along as a dependency
```

Python 3.11+. The `Glob` and `Grep` tools shell out to
[ripgrep](https://github.com/BurntSushi/ripgrep): `rg` must be on `PATH`
(`apt install ripgrep`, `brew install ripgrep`).

## Quickstart

```python
from noeta.sdk import Options, query
from noeta.sdk.providers import AnthropicProvider   # reads ANTHROPIC_API_KEY

result = query(
    Options(system_prompt="You are a concise coding assistant."),
    goal="What files are in this directory, and what does each one do?",
    provider=AnthropicProvider(),
    model="claude-sonnet-5",
)
print(result.answer())
```

`OpenAICompatProvider` and `OpenAIResponsesProvider` in the same module connect
any OpenAI-compatible gateway; the agent does not change. To run with no API
key, `noeta.sdk.testing.FakeLLMProvider` replays scripted responses offline.

## Learn more

- [Documentation](https://initxy.github.io/noeta/) — quickstart, tutorial,
  guides and the SDK reference
- [Why Noeta](https://initxy.github.io/noeta/why-noeta.html) — what sets it
  apart, and how it compares
- Runnable [`examples/`](https://github.com/initxy/noeta/tree/main/examples) —
  custom tools, an in-process MCP server, a permission gate, subagents, and
  surviving `kill -9` mid-task
