# Noeta — a durable, provider-neutral runtime + SDK for AI agents

**English** · [简体中文](README.zh-CN.md)

**[Documentation](https://initxy.github.io/noeta/tutorials/first-agent/)** · [Your first agent](https://initxy.github.io/noeta/tutorials/first-agent/) · [SDK reference](https://initxy.github.io/noeta/reference/sdk/) · [Comparison](https://initxy.github.io/noeta/reference/comparison/)

> **A Python library for building long-horizon agents on a durable,
> event-sourced runtime** — crash-safe exactly-once execution, suspend/wake
> for humans and timers, worker leases, and full audit + replay. In-process
> like the Claude Agent SDK: no server, no HTTP between your code and the
> engine. Runs fully offline with zero credentials, and speaks to Anthropic
> or any OpenAI-compatible endpoint when you wire one in.

Noeta ships as **two libraries** sharing one `noeta.` namespace:

- **noeta-sdk** — the thin client you import, and the **only** public surface.
  `query()` / `Client` / `Options` / `@tool` / `create_sdk_mcp_server`, the
  four official agents in `noeta.presets`, the open extension interfaces
  (Tool / LLMProvider / Policy / Guard / Observer / ContentChannel), and the
  plugin loader. It contains no engine — it forwards in-process into the
  runtime.
- **noeta-runtime** — the pure engine underneath: durable event-sourced task
  execution, fold/snapshot, the scheduler and worker leases, builtin tools,
  provider adapters, and the context composer. You install it only as a
  transitive dependency of `noeta-sdk`; you never import it directly.

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

# A network-free provider scripted to answer in one turn.
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
multi-turn session (`Client.messages`), and the same recording keeps folding.

## Connect a real model

The provider is an `Options` field — **wiring, not identity** — so swapping it
changes nothing about the agent, its tools, or its recorded history. The
adapters are exported through `noeta.sdk.providers`:

```python
from noeta.sdk import Options
from noeta.sdk.providers import AnthropicProvider, OpenAICompatProvider

anthropic = Options(system_prompt="…", provider=AnthropicProvider(api_key="sk-ant-…"))
openai = Options(system_prompt="…", provider=OpenAICompatProvider(
    base_url="https://api.openai.com/v1", api_key="sk-…"))
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

Bundles of these contributions ship as **plugins** (`load_plugins` /
`merge_plugins`), merged deterministically into `Options` before compile. The
runnable [`examples/plugins/`](examples/plugins/) cover guards (protected
paths, approval modes) and observers (git checkpointing).

## A real host, from the public surface only

[`examples/reference-host/`](examples/reference-host/) is the smallest program
that stands up a durable, plugin-extended, streaming agent the way an embedding
product would — durable SQLite storage, live token streaming, plugins, and the
official `main` preset, all wired from `noeta.sdk` / `noeta.sdk.storage` /
`noeta.presets` and **no** runtime internal. If the reference host can build an
agent, a third-party host can too.

```bash
python examples/reference-host/host.py   # drives one turn against a scripted offline provider
```

## The runtime underneath

Every turn is a durable, event-sourced engine task:

- **Crash-safe, exactly-once execution.** State is folded from an append-only
  event log, never held in memory — kill the process mid-task and a fresh one
  resumes at the exact point, exactly once.
- **Long-horizon tasks.** A task can suspend for hours or days waiting on a
  human answer, a timer, or a sub-task, then wake exactly once when the
  condition fires — waiting costs nothing while it sleeps. The drain loop
  ships as a library primitive (`noeta.runtime.worker.WorkerLoop`); an
  embedder constructs and runs it (see
  [Deploy a worker](https://initxy.github.io/noeta/how-to/deploy-worker/)).
- **Full audit & replay.** Every event, LLM turn, tool call, and token/cache
  stat is recorded; compaction is a reversible overlay, so a recovered task
  compacts the same way and you can still read what was pared away.
- **Provider-neutral.** Anthropic and OpenAI-compatible adapters sit behind
  one internal protocol — recorded history isn't bound to any vendor's shape,
  and the kernel is forbidden (by an import-linter rule) to depend on any
  vendor SDK.
- **Deterministic offline mode.** The scripted `FakeLLMProvider` runs the
  whole stack with no network, so install, storage, and wiring are provable on
  a fresh checkout (and in CI).

## Use only the layer you need

| Package | You get | Analogous to |
| --- | --- | --- |
| `noeta-sdk` | The client facade you import: `query()`, `Client`, `Options`, `@tool`, presets, the extension interfaces. | Claude Agent SDK |
| `noeta-runtime` | The pure engine — event log, fold, scheduler, tools, policies, providers. A transitive dependency you never import directly. | — |

The runnable [`examples/`](examples/) cover the SDK surface end to end — a
minimal agent, custom tools, an in-process MCP server, a permission gate,
provider swapping, sub-agent delegation, and surviving `kill -9` mid-task —
each with an offline smoke test so they cannot silently rot.

## Documentation

Full documentation is rendered at
**[initxy.github.io/noeta](https://initxy.github.io/noeta/tutorials/first-agent/)**.
The same files live under [`docs/`](docs/) for source browsing.

| Layer | Start at | Read it when |
| --- | --- | --- |
| Tutorials | [Your first agent](https://initxy.github.io/noeta/tutorials/first-agent/) | You're new and want it running. |
| How-to guides | [Configure a provider](https://initxy.github.io/noeta/how-to/configure-provider/) · [Build custom tools](https://initxy.github.io/noeta/how-to/build-custom-tools/) · [Write a plugin](https://initxy.github.io/noeta/how-to/write-a-plugin/) | You have a specific task to get done. |
| Concepts | [Event sourcing](https://initxy.github.io/noeta/concepts/event-sourcing/) | You want to understand the design. |
| Reference | [SDK](https://initxy.github.io/noeta/reference/sdk/) · [WorkerLoop](https://initxy.github.io/noeta/reference/worker-loop/) · [Comparison](https://initxy.github.io/noeta/reference/comparison/) · [Tools](https://initxy.github.io/noeta/reference/tools/) | You need exact facts. |

Deeper cuts: the [architecture overview](https://initxy.github.io/noeta/architecture/overview/),
[troubleshooting](https://initxy.github.io/noeta/operations/troubleshooting/), and the
[ADRs](https://github.com/initxy/noeta/tree/main/docs/adr) recording why each cross-module decision is the way it is
(vocabulary lives in [`CONTEXT.md`](CONTEXT.md)).

## Contributing

Development setup and repository layout live in
[`CONTRIBUTING.md`](CONTRIBUTING.md); working conventions (human or agent)
start at the root [`AGENTS.md`](AGENTS.md) router.

## License

Apache License 2.0 — see [`LICENSE`](LICENSE).
