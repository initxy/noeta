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

**Agents that keep running.** Noeta is a Python SDK for agents that survive
crashes, wait days for a human, and scale from one script to a cluster —
without changing the agent.

**English** · [简体中文](README.zh-CN.md) · [Docs](https://initxy.github.io/noeta/) · [Quickstart](https://initxy.github.io/noeta/start/quickstart.html) · [Why Noeta](https://initxy.github.io/noeta/why-noeta.html)

## A working agent in a few lines

```bash
uv pip install noeta-sdk
export ANTHROPIC_API_KEY=sk-ant-...
```

```python
from noeta.sdk import Options, query
from noeta.sdk.providers import AnthropicProvider

result = query(
    Options(system_prompt="You are a concise coding assistant."),
    goal="What files are in this directory, and what does each one do?",
    provider=AnthropicProvider(),
    model="claude-sonnet-5",
)
print(result.answer())
```

The agent explores the directory with its built-in file tools and answers.
`result` also holds every model call, tool call and token count from the run.

## What sets it apart

**Survives crashes.** Task state is rebuilt from an append-only event log,
never held in memory. Kill a worker mid-task and another one carries on from
the last step. The same log is a complete audit trail.

**Waiting costs nothing.** A task can pause for a human approval, a timer, a
subtask or an outside event — for seconds or months. It uses no resources
while it sleeps and is woken exactly once.

**From a script to a cluster.** `query()` in a script, `Client.start_workers(n)`
inside a service, several hosts on one Postgres. The agent code is the same at
every step, and there is no daemon or extra service to run.

Also: **every capability is a plugin** — file tools, web, memory, MCP,
sandboxes and model adapters use the same public API yours does. **Any
model** — Anthropic, any OpenAI-compatible gateway, or the OpenAI Responses
API, one line to switch. **Approval before action** — risky tool calls pause
for a human; guards can block a call before it runs.

## How it compares

| | **Noeta** | Claude Agent SDK | LangGraph | Temporal |
|---|---|---|---|---|
| What it is | Durable agent runtime, as a library | Agent loop library for Claude | Graph-based agent framework | Durable workflow platform |
| Control flow | The model decides each step | The model decides | A graph you define | Workflow code you write |
| What is saved | Every event; state is derived from it | The conversation | Checkpoints of graph state | Workflow history |
| Waiting for a human / timer | Built in, woken exactly once | Resume the conversation | Interrupt, then the caller resumes | Built in |
| Scaling out | Worker pool; many hosts on Postgres | One process | Up to you, or the hosted platform | A Temporal cluster |
| Models | Any, one line to switch | Claude | Any | — |
| Extra service to run | None | None | None | Temporal server |

Pick Noeta when an agent runs unattended for a long time and you need to
recover, audit and scale it. [Full comparison, and when not to use it](https://initxy.github.io/noeta/why-noeta.html).

## Benchmarks

| Benchmark | Scope | `noeta-agent` `main` (Claude Opus 4.8) | Field |
|---|---|---|---|
| Terminal-Bench 2.1 | 40-task stratified sample | **82.5%** (33/40) | public board spans 58.7%–83.8% |
| SWE-bench Verified | 15-instance subset | **86.7%** (13/15) | top ~79%, mid-pack ~66–77% |

An agent built only on the public SDK ([noeta-agent](https://github.com/initxy/noeta-agent)),
run on the official harness ([harbor](https://github.com/harbor-framework/harbor))
and scored by each task's own verifier. Both rows are samples, not full
leaderboard runs. [Method and caveats](https://initxy.github.io/noeta/benchmarks.html).

## Documentation

| | |
|---|---|
| **Start** | [Quickstart](https://initxy.github.io/noeta/start/quickstart.html) · [Tutorial: build an agent](https://initxy.github.io/noeta/start/tutorial.html) |
| **Guides** | [Models](https://initxy.github.io/noeta/guides/models.html) · [Custom tools](https://initxy.github.io/noeta/guides/tools.html) · [MCP](https://initxy.github.io/noeta/guides/mcp.html) · [Subagents](https://initxy.github.io/noeta/guides/subagents.html) · [Plugins](https://initxy.github.io/noeta/guides/plugins.html) · [Testing](https://initxy.github.io/noeta/guides/testing.html) · [Deploy](https://initxy.github.io/noeta/guides/deploy.html) |
| **Understand** | [How it works](https://initxy.github.io/noeta/how-it-works/) · [ADRs](docs/adr/) |
| **Look up** | [SDK reference](https://initxy.github.io/noeta/reference/sdk.html) · [Options](https://initxy.github.io/noeta/reference/options.html) · [Built-in tools](https://initxy.github.io/noeta/reference/tools.html) |

Prefer code? [`examples/`](examples/) has runnable scripts for custom tools,
MCP servers, permission gates, subagents and surviving `kill -9`, each with an
offline test. [`examples/reference-host/`](examples/reference-host/) is a
complete host built from the public API alone.

## License

Apache 2.0 — see [`LICENSE`](LICENSE).
