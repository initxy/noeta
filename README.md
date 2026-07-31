# Noeta — a durable, provider-neutral runtime + SDK for AI agents

**English** · [简体中文](README.zh-CN.md)

**[Documentation](https://initxy.github.io/noeta/tutorials/first-agent/)** · [Your first agent](https://initxy.github.io/noeta/tutorials/first-agent/) · [SDK reference](https://initxy.github.io/noeta/reference/sdk/) · [Comparison](https://initxy.github.io/noeta/reference/comparison/)

Noeta is a Python library for building long-horizon agents on a durable,
event-sourced runtime. It runs in-process like the Claude Agent SDK — no server,
no HTTP between your code and the engine — and every turn is a recorded engine
task: crash-safe and exactly-once, able to suspend for a human answer or a timer
and wake when the condition fires, fully auditable and replayable.

## Why not just write the loop yourself

A hand-rolled `while` loop around an LLM keeps its state in memory. Kill the
process and the task is gone; there is no record of why the agent did what it
did, no way to pause for a human without blocking a thread, and no way to swap
model vendors without rewriting the loop. Noeta gives you:

- **Crash-safe, exactly-once execution.** State is folded from an append-only
  event log, never held in memory — kill the process mid-task and a fresh one
  resumes at the exact point, exactly once.
- **Long-horizon suspend/wake.** A task can park for hours or days on a human
  answer, a timer, or a sub-task, then wake exactly once when the condition
  fires. Waiting costs nothing while it sleeps.
- **Full audit & replay.** Every event, LLM turn, tool call, and token/cache
  stat is recorded; compaction is a reversible overlay, so a recovered task
  compacts the same way and you can still read what was pared away.
- **Provider neutrality.** Anthropic and OpenAI-compatible adapters sit behind
  one internal protocol; recorded history is not bound to any vendor's shape,
  and the kernel is forbidden (by an import-linter rule) to depend on a vendor SDK.
- **A deterministic offline mode.** A scripted `FakeLLMProvider` drives the whole
  stack with no network, so install, storage, and wiring are provable on a fresh
  checkout and in CI.

## Install

```bash
uv pip install noeta-sdk      # noeta-runtime comes along as a transitive dependency
```

Then `import noeta.sdk` — that single module is the whole public surface.
Python 3.11+.

## Quickstart — zero credentials

No API key, no network. Build an `Options` recipe and drive one turn with
`query`, using the deterministic offline provider from `noeta.sdk.testing`:

<!-- runnable: smoke -->
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
            name="main",
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

`query` returns the full event-envelope stream for the turn — the
machine-readable record of everything the agent did — and `result.answer()`
reads the answer off the terminal envelope. Swap the `Client` facade in for a
multi-turn conversation (`Client.messages`), and the same recording keeps folding.

## Connect a real model

The provider is an `Options` field — **wiring, not identity** — so swapping it
changes nothing about the agent, its tools, or its recorded history. The adapters
are re-exported through `noeta.sdk.providers`:

```python
from noeta.sdk import Options
from noeta.sdk.providers import AnthropicProvider, OpenAICompatProvider

# api_key falls back to ANTHROPIC_API_KEY / OPENAI_API_KEY when omitted.
# Anthropic requires a token budget: pass default_max_tokens, or max_tokens per request.
anthropic = Options(
    system_prompt="…",
    provider=AnthropicProvider(default_max_tokens=1024),
)
openai = Options(
    system_prompt="…",
    provider=OpenAICompatProvider(base_url="https://api.openai.com/v1"),
)
```

See [Configure a provider](https://initxy.github.io/noeta/how-to/configure-provider/)
for the Responses API, secondary gateways, and the offline testing double.

## What you can extend

Everything open is an `Options` field, re-exported through `noeta.sdk`:

| Seam | Extends |
| --- | --- |
| `@tool` | stamp a function with name, version, risk level, and input schema to make it a first-class tool |
| `mcp_servers` | in-process SDK MCP tools (`create_sdk_mcp_server`) or connectors to external stdio / HTTP MCP servers |
| `provider` | any adapter satisfying `LLMProvider` (the basis of provider neutrality) |
| `policy` | swap the ReAct decision function for your own |
| `guards` | synchronous checks before a tool call / spawn / finish |
| `observers` | read-only event subscribers — audit, metrics |
| `content_channels` | register a `ContentKindSpec` to place custom resident content into context |

Bundles of these contributions ship as **plugins**, loaded with `load_plugins`
and selected per agent through `Options.plugins`. The runnable
[`examples/plugins/`](examples/plugins/) cover guards (protected paths, approval
modes), observers (git checkpointing), and a RAG-style memory recall provider.

## Architecture

Two libraries share one `noeta.` namespace, both at version 0.4.0:

- **noeta-sdk** — the thin client you import, and the **only** public surface.
  `query` / `Client` / `Options` / `@tool` / `create_sdk_mcp_server`, the four
  official agents in `noeta.presets`, the open extension interfaces (`Tool` /
  `LLMProvider` / `Policy` / `Guard` / `Observer` / `ContentKindSpec`), and the
  plugin loader. Every official capability — the fs/web tool packs, the provider
  adapters, memory, skills, the durable storage backends — lives here as a
  built-in plugin under `noeta.builtins`, reached only through the loader's
  dynamic resolution.
- **noeta-runtime** — the pure kernel underneath: durable event-sourced task
  execution, fold/snapshot, the scheduler and worker leases, and the context
  composer. It carries **no capability implementation of its own** and depends on
  no vendor SDK. You install it only as a transitive dependency of `noeta-sdk`
  and never import it directly.

A task advances one step at a time inside the Engine (`compose → decide →
dispatch`). Folded state comes only from the append-only EventLog plus the
content-addressed ContentStore, so any process can reconstruct a task from its
stream alone. A Worker leases a task, runs it to the next suspend or terminal,
and releases; the drain loop ships as the library primitive
`noeta.runtime.worker.WorkerLoop`, which an embedding host constructs and runs.

## A real host, from the public surface only

[`examples/reference-host/`](examples/reference-host/) is the smallest program
that stands up a durable, plugin-extended, streaming agent the way an embedding
product would — durable SQLite storage, live token streaming, plugins, and the
official `main` preset, all wired from `noeta.sdk` / `noeta.sdk.storage` /
`noeta.presets` with **no** runtime internal. If the reference host can build an
agent, a third-party host can too.

```bash
python examples/reference-host/host.py   # drives one turn against a scripted offline provider
```

The runnable [`examples/`](examples/) cover the SDK surface end to end — a
minimal agent, custom tools, an in-process MCP server, a permission gate,
provider swapping, sub-agent delegation, and surviving `kill -9` mid-task —
each with an offline smoke test so they cannot silently rot.

## Documentation

Full documentation is rendered at
**[initxy.github.io/noeta](https://initxy.github.io/noeta/tutorials/first-agent/)**;
the same files live under [`docs/`](docs/) for source browsing.

| Layer | Start at | Read it when |
| --- | --- | --- |
| Tutorials | [Your first agent](https://initxy.github.io/noeta/tutorials/first-agent/) | You're new and want it running. |
| How-to guides | [Configure a provider](https://initxy.github.io/noeta/how-to/configure-provider/) · [Build custom tools](https://initxy.github.io/noeta/how-to/build-custom-tools/) · [Write a plugin](https://initxy.github.io/noeta/how-to/write-a-plugin/) · [Deploy a worker](https://initxy.github.io/noeta/how-to/deploy-worker/) | You have a specific task to get done. |
| Concepts | [Event sourcing](https://initxy.github.io/noeta/concepts/event-sourcing/) | You want to understand the design. |
| Reference | [SDK](https://initxy.github.io/noeta/reference/sdk/) · [WorkerLoop](https://initxy.github.io/noeta/reference/worker-loop/) · [Comparison](https://initxy.github.io/noeta/reference/comparison/) · [Tools](https://initxy.github.io/noeta/reference/tools/) | You need exact facts. |

Deeper cuts: the [architecture overview](https://initxy.github.io/noeta/architecture/overview/),
[troubleshooting](https://initxy.github.io/noeta/operations/troubleshooting/), and the
[ADRs](https://github.com/initxy/noeta/tree/main/docs/adr) recording why each
cross-module decision is the way it is (vocabulary lives in [`CONTEXT.md`](CONTEXT.md)).

## Contributing

Development setup and repository layout live in
[`CONTRIBUTING.md`](CONTRIBUTING.md); working conventions (human or agent) start
at the root [`AGENTS.md`](AGENTS.md) router.

## License

Apache License 2.0 — see [`LICENSE`](LICENSE).
