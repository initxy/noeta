# Examples

Runnable SDK examples, organised by *what a library user wants to do* rather
than by internal module. Each file's module docstring names the SDK capability
it demonstrates and exposes a `run()` entrypoint. Every example ships a
network-free scripted provider, so all of them run with no API key and no
network; swap in `OpenAICompatProvider` / `AnthropicProvider` from
`noeta.sdk.providers` to drive a real model.

Every example is covered by
[`tests/test_examples_smoke.py`](../tests/test_examples_smoke.py), which
imports it and drives its minimal path — so an example cannot silently rot
while the SDK moves.

| What you want to do | Example | SDK surface |
| --- | --- | --- |
| Drive one goal to an answer | [`minimal_agent.py`](./minimal_agent.py) | `Options` + `query` + `QueryResult.answer` |
| Keep a task open and read its conversation | [`sdk_minimal.py`](./sdk_minimal.py) | `Client.start` / `Client.messages` / `Client.shutdown` |
| Give an agent a tool you wrote | [`custom_tool.py`](./custom_tool.py) | the `@tool` decorator + `Options.allowed_tools` |
| Ship a whole toolbox as one unit | [`mcp_server.py`](./mcp_server.py) | `create_sdk_mcp_server` + `Options.mcp_servers` |
| Approve or refuse tool calls in-process | [`permission_gate.py`](./permission_gate.py) | `Options.permission_mode` + `Options.can_use_tool` |
| Move a workload between LLM vendors | [`swap_provider.py`](./swap_provider.py) | `compile_options` — the provider is wiring, not identity |
| Delegate part of a goal to a sub-agent | [`spawn_subtask.py`](./spawn_subtask.py) | `Options.agents` + `AgentDefinition` + `spawn_subagent` |
| Survive `kill -9` mid-task | [`crash_resume.py`](./crash_resume.py) | `noeta.sdk.storage` SQLite triple + `fold` + dispatcher wake |

Run any of them directly:

```bash
python examples/minimal_agent.py
```

`crash_resume.py` is the slow one (~8s): it spawns a second process and waits
for a real timer to come due.

## A real host

[`reference-host/`](./reference-host/) assembles a minimal but real Noeta host
from the `noeta.sdk` public surface only — durable SQLite storage, live token
streaming, manifest plugins, and the official `main` preset. It is the smallest
program that stands up a durable, plugin-extended, streaming agent the way an
embedding host would.

## Plugins

[`plugins/`](./plugins/) holds six worked manifest plugins — `approval-modes`,
`checklist-reminder`, `git-checkpoint`, `memory-recall`, `protected-paths`,
`redaction` — each with its own `noeta-plugin.toml` and README. They are what
the reference host loads.

## `_internal/` — contributor demos

[`_internal/`](./_internal/) holds real-provider acceptance gates that walk
through kernel mechanics rather than the SDK public surface. They are kept
separate so a library user reading `examples/` is not led into internals. See
[`_internal/README.md`](./_internal/README.md).
