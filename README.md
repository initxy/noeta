<div align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/assets/logo/noeta-logo-dark.svg">
    <img src="docs/assets/logo/noeta-logo-light.svg" alt="Noeta — an event log folding into state" width="336">
  </picture>
  <p>
    <a href="https://pypi.org/project/noeta-sdk/"><img alt="PyPI" src="https://img.shields.io/pypi/v/noeta-sdk"></a>
    <a href="https://github.com/initxy/noeta/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/initxy/noeta/actions/workflows/ci.yml/badge.svg?branch=main"></a>
    <a href="https://pypi.org/project/noeta-sdk/"><img alt="Python versions" src="https://img.shields.io/pypi/pyversions/noeta-sdk"></a>
    <a href="LICENSE"><img alt="License: Apache-2.0" src="https://img.shields.io/badge/license-Apache--2.0-blue"></a>
  </p>
</div>

**A Python runtime and SDK for agents that have to keep running.** Import it and
drive an agent in your own process today; run the same agent on a multi-worker,
multi-host pool tomorrow — without touching the agent. Every capability is a
plugin, every model vendor is one line of wiring, and every run is durable
enough to survive `kill -9` and replay afterwards.

**English** · [简体中文](README.zh-CN.md) · [Docs](https://initxy.github.io/noeta/) · [Quickstart](https://initxy.github.io/noeta/tutorials/quickstart/) · [First agent](https://initxy.github.io/noeta/tutorials/first-agent/) · [SDK reference](https://initxy.github.io/noeta/reference/sdk/)

## It scores in the top band of the public leaderboard

| Benchmark | Scope | `noeta-agent` `main` (Claude Opus 4.8) | Field |
|---|---|---|---|
| Terminal-Bench 2.1 | 40-task stratified sample | **82.5%** (33/40) | public board spans 58.7%–83.8% |
| SWE-bench Verified | 15-instance subset | **86.7%** (13/15) | top ~79%, mid-pack ~66–77% |

Run through [harbor](https://github.com/harbor-framework/harbor) — the official
Terminal-Bench harness, the same one behind the public leaderboard — on the
official datasets, scored by each task's own verifier. The agent is
[`noeta-agent`](https://github.com/initxy/noeta-agent)'s `main` preset,
assembled entirely from this SDK's public surface, so the numbers exercise the
runtime end to end. Both rows are **samples**, labelled as such: a placement in
the field's band, not full-set leaderboard entries.

Full methodology, per-difficulty split, exclusions, and the exact re-runnable
commands: [Benchmarks](https://initxy.github.io/noeta/benchmarks/).

## Why Noeta

### Server-ready, not just a loop you call

`Client.start_workers(n)` turns the same process into a resident worker pool;
point the store at Postgres and several hosts share one database with
lease-fenced writes. The Engine is stateless — any worker can advance any task,
so scaling out is a storage swap, not a rewrite.

```python
with client:
    client.start_workers(4)                       # resident pool, one process
    client.start(goal="Ship the release notes.")  # a worker picks it up
```

No daemon to operate, no HTTP hop, no vendor service in the middle. You own the
process and the database. → [Deploy a worker](https://initxy.github.io/noeta/how-to/deploy-worker/)

### Every capability is a plugin — including ours

The kernel ships **zero** capabilities. File tools, web tools, memory, browser,
MCP, sandboxes, storage backends, and every provider adapter are built-in
plugins that reach the kernel through exactly one doorway: the loader's dynamic
`ref` resolution. An import linter fails the build if anything takes a shortcut.

Your plugin therefore rides the identical path Noeta's own capabilities do —
there is no privileged internal API you are locked out of.
→ [Write a plugin](https://initxy.github.io/noeta/how-to/write-a-plugin/) · [Plugin surfaces](https://initxy.github.io/noeta/reference/plugin-surfaces/)

### Sixteen extension surfaces, declared as inert data

A plugin is a package with a static manifest. Noeta can list and collision-check
everything it contributes *before importing a line of its code*.

| Plane | Surfaces | Enters agent identity? |
|---|---|---|
| **Identity** | `tool`, `agent`, `content_kind`, `prompt_fragment`, `policy`, `control_tool` | Yes |
| **Wiring** | `guard`, `observer`, `provider`, `reminder_provider`, `reminder`, `tool_result_transform`, `session_pack` | No — process-wide |
| **Host** | `mcp_server`, `skills`, `sandbox_provider` | No — host-wired |

### Kill the process mid-task; it resumes

State is never held in memory — it is `fold(events)`, recomputed from an
append-only log. A worker holds a heartbeat-renewed lease, so exactly one writer
exists per task. If it dies, the lease expires, the next worker folds the log,
seals the interrupted attempt as dead history, and carries on from the last
durable point. Exactly once.

### Waiting is free and first class

A task suspends for a human answer, a timer, a subtask, or an external event,
and costs nothing while it sleeps. The wake that revives it is durable,
single-worker, and delivered exactly once — so a month-long approval loop is the
same machinery as a five-second tool call.

### Any model, enforced — not promised

Anthropic, any OpenAI chat-completions gateway, and the OpenAI Responses API sit
behind one internal protocol that never names a vendor. Swapping is wiring, not
identity: the agent, its tools, and its recorded history are untouched.

```python
from noeta.sdk import Options
from noeta.sdk.providers import AnthropicProvider, OpenAICompatProvider

anthropic = Options(system_prompt="…", provider=AnthropicProvider(default_max_tokens=1024))
openai    = Options(system_prompt="…", provider=OpenAICompatProvider(base_url="https://api.openai.com/v1"))
```

### Auditable by construction

Every model round-trip, tool call, guard verdict, and token count is an event on
the stream. Compaction is a reversible overlay — the originals stay. "What the
agent did" and "what the agent is" cannot disagree, because folding is the only
way state comes into being.

How each of these works, one page per idea — the event log, the lease, the
plugin loader: [Concepts](https://initxy.github.io/noeta/concepts/) ·
[Architecture](https://initxy.github.io/noeta/architecture/overview/).

## 60-second quickstart

```bash
uv pip install noeta-sdk      # noeta-runtime comes along as a transitive dep
```

Python 3.11+. Drive a full turn with no API key and no network — the offline
`FakeLLMProvider` stands in for a model:

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
            allowed_tools=("Read",),
            permission_mode="bypassPermissions"),
    goal="Say hello.",
    provider=provider,
    model="stub-model",
)
assert result.answer() == "Hello from Noeta."
```

`Options` is the recipe, `query` drives one turn, and the result carries the
whole event stream. This block is executed by the test suite on every run, so it
works as printed.

Next: the [5-minute quickstart](https://initxy.github.io/noeta/tutorials/quickstart/)
takes it to multi-turn conversation, durable storage, and a resident worker pool.

## Extend it

Everything open is either an `Options` field or a plugin contribution.

```python
from noeta.sdk import tool

@tool
def get_weather(city: str) -> str:
    """Get the current weather for a city."""
    return f"{city}: sunny, 22°C"

opts = Options(system_prompt="…", allowed_tools=("Read", get_weather))
```

`allowed_tools` takes tool names and tool objects side by side — a custom tool
joins the built-in set with no registration step. To package it for reuse,
declare a manifest and activate it per agent:

```toml
# pyproject.toml — a plugin manifest
[tool.noeta]
name = "my-plugin"
requires-noeta = ">=0.5"

[[tool.noeta.tool]]
name = "get_weather"
ref = "my_plugin.tools:get_weather"
```

```python
from noeta.sdk import load_plugins, Options

plugins = load_plugins(modules=["my_plugin"])
opts = Options(system_prompt="…", plugins=("my-plugin",))
```

Full manifest shape and every surface: [Write a plugin](https://initxy.github.io/noeta/how-to/write-a-plugin/) · [Plugin surfaces](https://initxy.github.io/noeta/reference/plugin-surfaces/).

## How it compares

| | **Noeta** | **Claude Agent SDK** | **Pi Harness** |
|---|---|---|---|
| **Focus** | Durable, server-ready agent runtime | In-process agent loop on Claude | Terminal coding-agent harness (TS toolkit) |
| **Deployment** | Multi-worker pool; multi-host on Postgres | Client library, single process | Single process in your terminal |
| **Persistence** | Event ledger — `state = fold(log)` | The conversation, managed for you | In-memory session state |
| **Suspend / wake** | First class: human, timer, subtask, external event — exactly-once | Session resume | Interrupt / continue in the TUI |
| **Model lock-in** | None — swap providers by wiring an adapter | Claude-first | None — unified multi-provider API |
| **Extension** | 16 plugin surfaces + the single-writer rule | Tools, MCP, subagents, hooks | TypeScript packages: loop, tools, TUI |

**Reach for Noeta when the agent's *running* must be a ledger you can replay,
audit, and scale across workers and hosts** — not just a loop you can call.
Longer treatment, including LangGraph and Temporal:
[comparison](https://initxy.github.io/noeta/reference/comparison/).

## Learn more

Full docs: **[initxy.github.io/noeta](https://initxy.github.io/noeta/)**

| | |
|---|---|
| **Start here** | [Quickstart (5 min)](https://initxy.github.io/noeta/tutorials/quickstart/) · [Your first agent](https://initxy.github.io/noeta/tutorials/first-agent/) |
| **Ship it** | [Deploy a worker](https://initxy.github.io/noeta/how-to/deploy-worker/) · [Docker](https://initxy.github.io/noeta/how-to/docker-deployment/) · [Configure a provider](https://initxy.github.io/noeta/how-to/configure-provider/) · [Use a sandbox](https://initxy.github.io/noeta/how-to/use-sandbox/) |
| **Extend it** | [Custom tools](https://initxy.github.io/noeta/how-to/build-custom-tools/) · [Write a plugin](https://initxy.github.io/noeta/how-to/write-a-plugin/) · [Connect MCP](https://initxy.github.io/noeta/how-to/connect-mcp/) · [Spawn subagents](https://initxy.github.io/noeta/how-to/spawn-subagents/) |
| **Understand it** | [Concepts](https://initxy.github.io/noeta/concepts/) · [Architecture](https://initxy.github.io/noeta/architecture/overview/) · [ADRs](https://github.com/initxy/noeta/tree/main/docs/adr) |
| **Look it up** | [SDK reference](https://initxy.github.io/noeta/reference/sdk/) · [Tools](https://initxy.github.io/noeta/reference/tools/) · [Presets](https://initxy.github.io/noeta/reference/presets/) · [Glossary](https://initxy.github.io/noeta/reference/glossary/) |
| **Evidence** | [Benchmarks](https://initxy.github.io/noeta/benchmarks/) · [Known limitations](https://initxy.github.io/noeta/operations/limitations/) |

Prefer reading code? The runnable [`examples/`](examples/) cover custom tools,
MCP servers, permission gates, sub-agent delegation, and surviving `kill -9`
mid-task — each with an offline smoke test, plus
[`examples/reference-host/`](examples/reference-host/), a complete host
assembled from the public surface alone.

## License

Apache 2.0 — see [`LICENSE`](LICENSE).
