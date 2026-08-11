---
layout: home

hero:
  name: "Noeta"
  text: "A Python runtime + SDK for agents that have to keep running"
  tagline: Drive an agent in your own process today; run the same agent on a multi-worker, multi-host pool tomorrow — without touching the agent. Every capability is a plugin, every model vendor is one line of wiring, and every run is durable enough to survive kill -9 and replay afterwards.
  actions:
    - theme: brand
      text: Quickstart (5 min)
      link: /tutorials/quickstart
    - theme: alt
      text: Benchmarks
      link: /benchmarks
    - theme: alt
      text: GitHub
      link: https://github.com/initxy/noeta

features:
  - title: Server-ready, not just a loop you call
    details: Client.start_workers(n) turns the same process into a resident worker pool; point the store at Postgres and several hosts share one database with lease-fenced writes. The Engine is stateless, so scaling out is a storage swap, not a rewrite — and there is no daemon to operate and no HTTP hop.

  - title: Every capability is a plugin — including ours
    details: The kernel ships zero capabilities. File tools, web tools, memory, browser, MCP, sandboxes, storage backends, and every provider adapter are built-in plugins reaching the kernel through one doorway. Your plugin rides the identical path; there is no privileged internal API you are locked out of.

  - title: Sixteen extension surfaces, declared as inert data
    details: A plugin is a package with a static manifest, so Noeta can list and collision-check everything it contributes before importing a line of its code. Tools, agents, policies, guards, observers, MCP servers, and sandbox providers are all contributions.

  - title: Kill the process mid-task; it resumes
    details: State is never held in memory — it is fold(events), recomputed from an append-only log, with a heartbeat-renewed lease making exactly one writer per task. The next worker seals the interrupted attempt and carries on from the last durable point, exactly once.

  - title: Waiting is free and first class
    details: A task suspends for a human answer, a timer, a subtask, or an external event, and costs nothing while it sleeps. The wake is durable, single-worker, and delivered exactly once — a month-long approval loop is the same machinery as a five-second tool call.

  - title: Any model, enforced — not promised
    details: Anthropic, any OpenAI chat-completions gateway, and the OpenAI Responses API sit behind one internal protocol that never names a vendor. Swapping endpoints is wiring, not a rewrite — the kernel is barred from importing a provider package, and the build fails if it tries.
---

## In the top band of the public leaderboard

| Benchmark | Scope | `noeta-agent` `main` (Claude Opus 4.8) | Field |
|---|---|---|---|
| Terminal-Bench 2.1 | 40-task stratified sample | **82.5%** (33/40) | public board spans 58.7%–83.8% |
| SWE-bench Verified | 15-instance subset | **86.7%** (13/15) | top ~79%, mid-pack ~66–77% |

Run through [harbor](https://github.com/harbor-framework/harbor), the official
Terminal-Bench harness, on the official datasets, scored by each task's own
verifier. The agent is [`noeta-agent`](https://github.com/initxy/noeta-agent)'s
`main` preset, assembled entirely from this SDK's public surface. Both rows are
**samples**, labelled as such — see [Benchmarks](/benchmarks) for the full
methodology, exclusions, and re-runnable commands.

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
            allowed_tools=("Read",),
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
| [Benchmarks](/benchmarks) | How an agent built on Noeta scores on public benchmarks, and how that was measured. |
