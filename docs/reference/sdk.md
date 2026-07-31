# SDK reference: `noeta.sdk`

`noeta.sdk` is the one module you import. The client verbs, the agent recipe,
the extension interfaces and the plugin mechanism are all re-exported from it,
so application code never reaches into `noeta.client`, `noeta.core` or any
other internal package.

```python
from noeta.sdk import query, Client, Options, tool
```

The source of truth for every name on these pages is the `__all__` list in
`packages/noeta-sdk/noeta/sdk/__init__.py`. If a name is not in `__all__`, it is
not public, and a future release may move it.

## Where things live

The surface is large enough that it is split into three focused pages. Pick the
one that matches the question you have.

| Page | Covers | Reach for it when |
| --- | --- | --- |
| [query / Client](sdk-client.md) | `query`, `QueryResult`, `Client` and all its verbs, the resident worker pool, inspection, the typed error surface | you are *driving* an agent — starting a turn, approving a tool call, resuming a conversation |
| [Options](sdk-options.md) | `Options`, `AgentDefinition`, `SystemPromptPreset`, `compile_options`, permission modes, plugin activation, and `HostConfig` host wiring | you are *configuring* an agent — which tools, which prompt, which storage backend |
| [Types & testing](sdk-types.md) | the extension interfaces, message and content types, `LLMResponse` / `Usage`, the `@tool` authoring API, `noeta.sdk.testing` | you are *implementing* something Noeta calls back into — a tool, a provider, a guard |

Two more reference pages sit alongside them:
[Plugins](plugins.md) for the packaging and discovery mechanism, and
[Presets](presets.md) for the four official agents in `noeta.presets`.

## Submodules

Four names under `noeta.sdk` are separate modules rather than root re-exports,
because importing them pulls in weight most callers do not need.

| Module | Holds | Why it is separate |
| --- | --- | --- |
| `noeta.sdk.providers` | `AnthropicProvider`, `OpenAICompatProvider`, `OpenAIResponsesProvider`, `CATALOG`, `ModelSpec` | only a caller that builds a network provider pays for `httpx` |
| `noeta.sdk.storage` | `open_storage_stack` plus the sqlite / postgres adapters | only a caller that chose Postgres pays for `psycopg` |
| `noeta.sdk.testing` | `FakeLLMProvider` | test material must not be reachable from a production import |
| `noeta.presets` | the four official agents and their prompts | re-exported from the root as `presets`, and importable directly |

All four resolve lazily, so nothing statically imports `noeta.builtins` — the
rule that keeps the kernel free of vendor code.

## The shortest complete program

```python
from noeta.sdk import Options, query
from noeta.sdk.providers import AnthropicProvider

options = Options(system_prompt="You are a concise coding assistant.")

result = query(
    options,
    goal="List the Python files in this directory.",
    provider=AnthropicProvider(api_key="sk-ant-…"),
    workspace_dir=".",
)
print(result.answer())
# → 'docs/conf.py, setup.py, …'  (the agent's terminal answer, as a str)
```

`query` is the one-shot path. For a conversation that spans turns, build a
`Client` instead — see [query / Client](sdk-client.md).

## Next

- [Quickstart](../tutorials/quickstart.md) — a running agent in five minutes
- [Your first agent](../tutorials/first-agent.md) — a real agent with a custom
  tool and permissions
- [Architecture: packages](../architecture/packages.md) — why there are two
  wheels and one import path
- [Glossary](glossary.md) — every term on these pages, defined once
