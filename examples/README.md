# Examples

These are **SDK usage examples**, organised by *what a library user wants
to do* rather than by internal module. Each file is a small, runnable
script whose top docstring names the SDK capability it demonstrates. Every
one ships an offline `FakeLLMProvider` so it runs with **no API key and no
network**; the docstrings show how to swap in a real provider
(`OpenAICompatProvider` / `AnthropicProvider`).

Each example has a smoke test in
[`tests/test_examples_smoke.py`](../tests/test_examples_smoke.py) (import +
minimal path), so they cannot silently rot as the SDK evolves.

| Task | Example | SDK surface |
| --- | --- | --- |
| Run a minimal agent | [`minimal_agent.py`](./minimal_agent.py) | `Options` + `query` |
| Drive an agent in-process (pure SDK) | [`sdk_minimal.py`](./sdk_minimal.py) | `query` + `Client.messages` |
| Give an agent a custom tool | [`custom_tool.py`](./custom_tool.py) | the `@tool` decorator |
| Bundle tools into an in-process MCP server | [`mcp_server.py`](./mcp_server.py) | `create_sdk_mcp_server` + `Options.mcp_servers` |
| Gate tool calls with a permission callback | [`permission_gate.py`](./permission_gate.py) | `Options.permission_mode` + `Options.can_use_tool` |
| Swap the provider (provider neutrality) | [`swap_provider.py`](./swap_provider.py) | provider is wiring, not identity |
| Delegate to a sub-agent | [`spawn_subtask.py`](./spawn_subtask.py) | `Options.agents` + `spawn_subagent` |

Run any of them directly:

```bash
python examples/minimal_agent.py
```

## Want a real model?

[`openai-compatible/`](./openai-compatible/) is a ready-to-use config + instructions
for booting the official coding agent (`python -m noeta.agent`) against a
real OpenAI-compatible endpoint.

## `_internal/` — contributor demos (not SDK examples)

[`_internal/`](./_internal/) holds the real-provider acceptance gates.
They walk through internal mechanics (EventLog / the lease loop) rather
than the SDK public surface. They are kept — not deleted — as working
samples of the kernel's behaviour. See
[`_internal/README.md`](./_internal/README.md).
