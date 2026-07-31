---
layout: home

hero:
  name: "Noeta"
  text: "A durable, provider-neutral runtime + SDK for AI agents"
  tagline: A Python library for long-horizon agents. Task state is folded from an append-only event log, so a killed process resumes where it stopped; tasks suspend for a human, a timer, or a subtask and wake durably. Import noeta.sdk and drive the engine in-process — no server, and no credentials needed to run it.
  actions:
    - theme: brand
      text: Quickstart (5 min)
      link: /tutorials/quickstart
    - theme: alt
      text: Your first agent
      link: /tutorials/first-agent
    - theme: alt
      text: GitHub
      link: https://github.com/initxy/noeta

features:
  - title: Survives crashes
    details: A task's truth is its append-only event log, not a value in memory. Kill the process mid-task and the next worker folds the log back, seals the interrupted attempt, and never silently re-runs a call that had side effects.

  - title: Long-horizon by design
    details: A task can suspend to wait on a person, a timer, or a subtask, and costs nothing while it sleeps. When the condition fires it wakes exactly once — the match is durable, so a crash in between re-delivers instead of dropping it.

  - title: Fully inspectable
    details: Every step, LLM round-trip, tool call, guard verdict, and token count is an event in the log. The trace tells you why a step happened, not just what happened.

  - title: In-process, then a worker pool
    details: Import noeta.sdk and drive the engine in your own process — no HTTP hop, no daemon to operate. The same code scales up with Client.start_workers(n), or across hosts on Postgres, because the Engine is stateless and writes are lease-fenced.

  - title: Provider-neutral
    details: Anthropic, any OpenAI chat-completions gateway, and the OpenAI Responses API sit behind one internal protocol that never names a vendor. Swapping endpoints is wiring, not a rewrite — the kernel is barred from importing a provider package.

  - title: Every capability is a plugin
    details: Tools, agents, policies, guards, observers, MCP servers, and sandbox providers are all manifest-declared contributions over sixteen extension surfaces. Noeta's own built-ins ride the identical loader your plugin does.
---

## 60-second taste

```bash
uv pip install noeta-sdk      # noeta-runtime comes along as a transitive dep
```

Zero credentials, no network — drive one turn with the offline `FakeLLMProvider`:

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

Point it at a real model by swapping the provider — see
[Configure a provider](/how-to/configure-provider).

## Find your way

New here? Read the [Quickstart](/tutorials/quickstart), then
[Your first agent](/tutorials/first-agent). Everything else is below.

### Tutorials — learn by doing

| Page | What you get |
|---|---|
| [Quickstart (5 min)](/tutorials/quickstart) | Install, run one turn offline, read the event log it produced. |
| [Your first agent](/tutorials/first-agent) | A real agent with a custom tool and a permission gate. |
| [CI integration](/tutorials/ci-integration) | Run agents deterministically in CI, no API key needed. |

### How-to — solve one problem

| Page | Use it when |
|---|---|
| [Configure a provider](/how-to/configure-provider) | You want a real model: Anthropic, an OpenAI-compatible gateway, or Responses. |
| [Build custom tools](/how-to/build-custom-tools) | Your agent needs to call your own code. |
| [Spawn sub-agents](/how-to/spawn-subagents) | A task should delegate part of the work and wait for the result. |
| [Connect MCP](/how-to/connect-mcp) | You want tools from an existing MCP server. |
| [Write a plugin](/how-to/write-a-plugin) | You want to package tools, agents, or policies for reuse. |
| [Deploy a worker](/how-to/deploy-worker) | Tasks should keep running outside the process that started them. |
| [Deploy with Docker](/how-to/docker-deployment) | You are shipping the worker as a container. |
| [Use a sandbox](/how-to/use-sandbox) | Tool calls must run isolated from the host. |
| [Multi-tenant memory](/how-to/multi-tenant-memory) | Several tenants share one deployment and must not see each other. |
| [Swap providers](/how-to/swap-providers) | An existing agent has to move to a different endpoint. |

### Concepts — understand the model

| Page | The idea |
|---|---|
| [All concepts](/concepts/) | The reading order, plus one line per concept. |
| [Event sourcing](/concepts/event-sourcing) | Why state is `fold(events)` and what that buys you. |
| [Task model](/concepts/task-model) | Task is the only primitive: states, attempts, subtasks. |
| [Engine & execution](/concepts/engine-execution) | What one step does: lease, fold, compose, decide, dispatch. |
| [Fold & snapshot](/concepts/fold-and-snapshot) | Rebuilding state from the log, and the snapshot that keeps it fast. |
| [Wake & resume](/concepts/wake-resume) | Suspending on a human, timer, or subtask — and waking exactly once. |
| [Guard vs Observer](/concepts/guard-observer) | Who can block a tool call, and who may only watch. |
| [Composer & cache](/concepts/composer-and-cache) | How the prompt is assembled in three segments to hit the provider cache. |
| [Provider neutrality](/concepts/provider-neutrality) | One internal protocol, three adapters, no vendor inside the kernel. |

### Architecture — how it is built

| Page | Covers |
|---|---|
| [Overview](/architecture/overview) | The guided tour of the whole system. |
| [Packages & import rules](/architecture/packages) | `noeta-sdk` over `noeta-runtime`, one namespace, the rules that keep them apart. |
| [State & writers](/architecture/state-and-writers) | State slices, the single-writer invariant, the versioned fold. |
| [Extension planes](/architecture/extension-planes) | Sixteen surfaces across three planes, and how built-ins ride them. |

### Reference — look things up

| Page | Contains |
|---|---|
| [SDK API map](/reference/sdk) | Everything importable from `noeta.sdk`, with links into the detail pages. |
| [query / Client](/reference/sdk-client) | The two entry points, their arguments, and what they return. |
| [Options](/reference/sdk-options) | Every `Options` field and the permission modes. |
| [Types & testing](/reference/sdk-types) | Events, content blocks, results, and the offline test doubles. |
| [Plugins overview](/reference/plugins) | What a plugin is and how it becomes active for an agent. |
| [Plugin manifest](/reference/plugin-manifest) | The manifest shape, loading, and version pinning. |
| [Plugin surfaces](/reference/plugin-surfaces) | All sixteen extension surfaces, one section each. |
| [Tools](/reference/tools) | The built-in tool catalog. |
| [Presets](/reference/presets) | The preset agents and what each is wired with. |
| [WorkerLoop](/reference/worker-loop) | Worker pool API, leases, polling behaviour. |
| [Comparison](/reference/comparison) | Noeta next to other agent frameworks. |
| [Glossary](/reference/glossary) | Every term, grouped by domain, with an A–Z index. |

### Operations — run it in production

| Page | Answers |
|---|---|
| [Troubleshooting](/operations/troubleshooting) | Symptom, cause, fix for the failures you will actually hit. |
| [Known limitations](/operations/limitations) | What Noeta does not do yet, stated plainly. |
