# Noeta

**A durable, provider-neutral runtime for long-horizon AI agents.** In-process like the Claude Agent SDK — no server, no HTTP hop — but every turn is a recorded, replayable event ledger.

**English** · [简体中文](README.zh-CN.md) · [Docs](https://initxy.github.io/noeta/) · [First agent](https://initxy.github.io/noeta/tutorials/first-agent/) · [SDK reference](https://initxy.github.io/noeta/reference/sdk/)

## Why Noeta

| | Noeta gives you |
|---|---|
| **Crash-safe** | State is `fold(events)`, never held in memory. Kill the process mid-task — the next worker resumes exactly where it stopped, exactly once. |
| **Server-ready** | Built for multi-process and multi-host. `Client.start_workers(n)` runs a resident pool; on Postgres, several hosts share one database with lease-fenced writes. The Engine is stateless — any worker can fold and advance any task. |
| **Long-horizon** | A task suspends for a human answer, a timer, or a subtask, then wakes durably when the condition fires. Waiting costs nothing while it sleeps. |
| **Provider-neutral** | Anthropic, OpenAI-compatible, and Responses API sit behind one protocol. The kernel is structurally forbidden from importing a vendor SDK. |
| **Auditable** | Every LLM round-trip, tool call, guard verdict, and token count is an event. Compaction is a reversible overlay — the originals stay on the stream. |
| **Extensible** | 16 manifest-declared extension surfaces. Noeta's own built-ins (fs, web, memory, browser, MCP, …) ride the same plugin loader as yours. |

## Noeta vs Cloud Agent SDK vs Pi Agent

| | **Noeta** | **Cloud Agent SDK** | **Pi Agent** |
|---|---|---|---|
| **Focus** | Durable, server-ready agent runtime | Build agents on Google Cloud (Gemini) | Computer-use: mouse, keyboard, screen |
| **Deployment** | Multi-worker pool, multi-host on Postgres | Client library, single process | Desktop process |
| **Persistence** | Event-sourced ledger — `state = fold(log)` | Conversation state, managed for you | Ephemeral — no durable execution model |
| **Suspend / wake** | First-class: human, timer, subtask, external event — exactly-once delivery | Session resume | Not applicable |
| **Model lock-in** | None — swap providers by wiring an adapter | Gemini-first | Any LLM (it's a control layer) |
| **Extension** | 16 plugin surfaces + single-writer invariant | Tools + hooks | Computer-control primitives |
| **Audit / replay** | Full event log, fold-reproducible | Limited | None |

**Noeta is for when the agent's *running* must be a ledger you can replay, audit, and scale across workers and hosts** — not just a loop you can call.

## Architecture

<p align="center">
  <img src="docs/assets/diagrams/architecture.svg" alt="Noeta architecture — noeta-sdk over noeta-runtime, builtins as plugins" width="820">
</p>

Two libraries, one `noeta.` namespace:

- **`noeta-sdk`** — the only thing you import. `query` / `Client` / `Options` / `@tool`, the four preset agents, and every built-in capability as a plugin under `noeta.builtins`.
- **`noeta-runtime`** — the pure kernel: Engine, fold/snapshot, Worker/Dispatcher/Lease, the context composer. Carries no capability implementation, depends on stdlib only.

The kernel never statically imports `noeta.builtins` — every capability reaches it through the plugin loader's dynamic `ref` resolution. That rule is what makes provider neutrality structural.

## Quickstart

```bash
uv pip install noeta-sdk      # noeta-runtime comes along as a transitive dep
```

Zero credentials, no network — drive one turn with the offline `FakeLLMProvider`:

<!-- runnable: smoke -->
```python
from noeta.sdk import Options, query, LLMResponse, TextBlock, Usage
from noeta.sdk.testing import FakeLLMProvider

provider = FakeLLMProvider(responses=[
    LLMResponse(stop_reason="end_turn",
                content=[TextBlock(text="Hello from Noeta.")],
                usage=Usage(uncached=1, output=1))
])

result = query(
    Options(system_prompt="You are concise.",
            allowed_tools=("read",),
            permission_mode="bypassPermissions"),
    goal="Say hello.",
    provider=provider,
    model="stub-model",
)
assert result.answer() == "Hello from Noeta."
```

Connect a real model by swapping the provider — it's wiring, not identity:

```python
from noeta.sdk import Options
from noeta.sdk.providers import AnthropicProvider, OpenAICompatProvider

anthropic = Options(system_prompt="…", provider=AnthropicProvider(default_max_tokens=1024))
openai    = Options(system_prompt="…", provider=OpenAICompatProvider(base_url="https://api.openai.com/v1"))
```

## How to extend

Everything open is an `Options` field or a plugin contribution. Noeta's built-ins use the exact same path.

### Add a tool

```python
from noeta.sdk import tool

@tool
def get_weather(city: str) -> str:
    """Get the current weather for a city."""
    return f"{city}: sunny, 22°C"

opts = Options(system_prompt="…", tools=(get_weather,))
```

### Add a plugin

A plugin is a manifest-declared contribution package. Declare contributions to any of the 16 surfaces across three planes:

| Plane | Surfaces | Enters agent identity? |
|---|---|---|
| **Identity** | `tool`, `agent`, `content_kind`, `prompt_fragment`, `policy`, `control_tool` | Yes |
| **Wiring** | `guard`, `observer`, `provider`, `reminder_provider`, `reminder`, `tool_result_transform`, `session_pack` | No (process-wide) |
| **Host** | `mcp_server`, `skills`, `sandbox_provider` | No (host-wired) |

```toml
# pyproject.toml — a plugin manifest
[tool.noeta]
name = "my-plugin"
requires-noeta = ">=0.4.0,<0.5.0"

[[tool.noeta.tool]]
name = "get_weather"
ref = "my_plugin.tools:get_weather"
```

Load it and activate it per agent:

```python
from noeta.sdk import load_plugins, Options

plugins = load_plugins(modules=["my_plugin"])
opts = Options(system_prompt="…", plugins=("my-plugin",))
```

See [Write a plugin](https://initxy.github.io/noeta/how-to/write-a-plugin/) for the full manifest shape.

## Where to go next

- **Tutorial:** [Your first agent](https://initxy.github.io/noeta/tutorials/first-agent/)
- **How-to:** [Configure a provider](https://initxy.github.io/noeta/how-to/configure-provider/) · [Build custom tools](https://initxy.github.io/noeta/how-to/build-custom-tools/) · [Write a plugin](https://initxy.github.io/noeta/how-to/write-a-plugin/) · [Use a sandbox](https://initxy.github.io/noeta/how-to/use-sandbox/) · [Deploy with Docker](https://initxy.github.io/noeta/how-to/docker-deployment/) · [Deploy a worker](https://initxy.github.io/noeta/how-to/deploy-worker/)
- **Concepts:** [Event sourcing](https://initxy.github.io/noeta/concepts/event-sourcing/) · [Wake & resume](https://initxy.github.io/noeta/concepts/wake-resume/) · [Composer & cache](https://initxy.github.io/noeta/concepts/composer-and-cache/)
- **Reference:** [SDK](https://initxy.github.io/noeta/reference/sdk/) · [Plugins](https://initxy.github.io/noeta/reference/plugins/) · [Comparison](https://initxy.github.io/noeta/reference/comparison/)

The runnable [`examples/`](examples/) cover custom tools, MCP servers, permission gates, sub-agent delegation, and surviving `kill -9` mid-task — each with an offline smoke test.

## License

Apache 2.0 — see [`LICENSE`](LICENSE).
