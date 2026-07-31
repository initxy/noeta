# Noeta compared to other agent frameworks

If you are deciding whether Noeta is the right tool, this page is the honest
version of that comparison — including the cases where it is the wrong choice.

Noeta is a runtime for long-horizon, task-oriented agents: it hosts, records,
schedules and replays agent execution without prescribing how an agent is
written. Every statement about Noeta below is checked against the code in this
repository. Statements about other projects are limited to their headline
design — for anything finer, read their own documentation.

## What Noeta is

- **Two libraries, in-process.** `noeta-runtime` is the kernel and declares no
  dependencies; `noeta-sdk` is the only thing you import and carries every
  capability implementation. There is no CLI and no HTTP server: a host embeds
  `noeta.sdk` and drives the loop itself.
- **State is a fold over an event log.** Each task owns an append-only
  `EventLog` stream; task state is `fold(events)`. Large bodies live in a
  content-addressed `ContentStore` (an event payload is capped at 4 KB) and are
  referenced by `ContentRef`.
- **Waiting is first class.** A task suspends on a `WakeCondition`
  (`SubtaskCompleted` / `HumanResponseReceived` / `TimerFired` /
  `ExternalEvent`); the `Dispatcher` matches an incoming wake event against
  suspended tasks and re-enqueues, and a `Worker` leases the task to advance it.
- **Compaction is recorded, not destructive.** A compaction step emits
  `CompactionRequested` plus `Compacted`; the summary body goes to the
  `ContentStore` and the composer swaps the covered prefix at compose time. The
  original messages stay on the stream, where audit and replay can read them.
- **Provider neutrality is enforced.** `LLMProvider` is the internal protocol;
  every vendor adapter lives in the `providers` built-in plugin. The kernel
  cannot reach an adapter, because nothing may statically import
  `noeta.builtins` — the `sdk-core-not-builtins` `import-linter` contract fails
  the build if it does.
- **Sixteen extension surfaces, one loader.** Contributions are declared in a
  static plugin manifest across three planes: identity (`tool`, `agent`,
  `content_kind`, `prompt_fragment`, `policy`, `control_tool`), wiring
  (`guard`, `observer`, `provider`, `reminder_provider`, `reminder`,
  `tool_result_transform`, `session_pack`), and host (`mcp_server`, `skills`,
  `sandbox_provider`). A manifest is inert data — a plugin's contributions are
  listable and collision-checkable before any of its code is imported.
- **Subagents are ordinary tasks.** `spawn_subtask` and `spawn_subtasks`
  create independent event-sourced tasks with their own streams; results return
  through a `SubtaskCompleted` wake, not a nested call.
- **Governance runs before the act.** `Guard` hooks fire at `before_tool_call`,
  `before_spawn_subtask`, and `before_finish`, returning
  `allow` / `deny` / `require_approval`; `Observer` hooks are read-only and
  their failure cannot affect the task.

## Noeta and the Claude Agent SDK

The Claude Agent SDK is a client library for building agents on Claude. It
ships an agent loop, built-in tools, MCP support, subagents, permission modes,
and hooks, and it manages the conversation for you.

| Concern | Claude Agent SDK | Noeta |
| --- | --- | --- |
| **Who owns the substrate** | Anthropic hosts the model; the library runs the loop in your process | You own loop, store, and wake machinery; the model is behind an adapter |
| **What is persisted** | The conversation, managed for you | An event ledger; state is `fold(events)`, never stored as the primary copy |
| **Suspend / wake** | Resume a session | `WakeCondition` matching + `Dispatcher` + `Lease`, delivered exactly once |
| **Compaction** | Automatic summarisation | `CompactionRequested` / `Compacted` events; originals stay on the stream |
| **Tools** | Built-in tools, `@tool`, MCP | 11 built-in tools, `@tool` (carrying `version` and `risk_level`), MCP over stdio and HTTP, plus in-process SDK MCP servers |
| **Permissions** | `permission_mode`, an approval callback, hooks | `permission_mode` (`default` / `acceptEdits` / `bypassPermissions`), `can_use_tool`, and Guards that rule before the act |
| **Extension** | Hooks | Sixteen manifest-declared surfaces plus the single-writer rule (observers cannot mutate) |
| **Concurrency** | One client in-process | `Client.start_workers(n)` for a resident pool; multi-host on Postgres |
| **Shape** | One library, TypeScript and Python | Two Python wheels: `noeta-runtime` (kernel) and `noeta-sdk` (what you import) |

The two answer different questions. The SDK asks "how do I give my code an
agent loop?" Noeta asks "how do I turn an agent's running into a ledger I can
replay, audit, and carry elsewhere?"

## Noeta and LangGraph

LangGraph expresses an agent as a graph of nodes and edges, with a checkpointer
that persists graph state so a thread can be resumed, interrupted for human
input, and rewound.

| Concern | LangGraph | Noeta |
| --- | --- | --- |
| **Unit of persistence** | A checkpoint of graph state | An append-only event ledger; state is derived, never the stored copy |
| **What history answers** | What the state *was* at a point | What *happened* — every envelope carries `actor` / `causation_id` / `trace_id` |
| **Control flow** | A graph you define; the model routes within it | No graph. The Policy decides each step; task structure emerges from decisions |
| **Scheduling** | The caller re-invokes the thread | `Dispatcher` + `Lease` + `WorkerLoop` ship in the library, including stale reclaim |
| **Compaction** | Application concern | A recorded step; the summary overlays at compose time |
| **Ecosystem** | Large integration catalogue, mature community | Small: 18 built-in plugins, no marketplace, young community |
| **Token streaming** | Through the graph's event API | Through a host-supplied `HostConfig.delta_sink`; deltas are ephemeral and the ledger stays the only durable record |

Reach for LangGraph when you want a graph and an integration catalogue. Reach
for Noeta when the question is auditability and substrate ownership — which
tool ran on whose authority, what was compacted away, what woke a sleeping
task — and you want the scheduling machinery in the library rather than in a
hosted product.

## Noeta and Temporal

Temporal is a durable execution platform: you write workflows and activities in
code, and the service durably schedules, retries, and times them.

Noeta is not a workflow engine. The LLM drives control flow, so a task's shape
emerges from the model's decisions rather than from a definition written ahead
of time. Temporal fits when you know the shape of the work; Noeta fits when the
model discovers it as it goes. Noeta keeps `Workflow` out of its vocabulary —
fixed procedures are expressed as a deterministic Policy plus `spawn_subtask`.

## Noeta and the Google Cloud Agent SDK

The Cloud Agent SDK builds agents on Google Cloud: Gemini models, tools wired
to GCP services (BigQuery, Cloud Storage, …), and a `Runner` that drives the
agent loop. It is a client library — the agent runs in your process, but the
substrate (model, tool integrations) is Google's.

| Concern | Cloud Agent SDK | Noeta |
| --- | --- | --- |
| **Deployment** | Client library, single process | Multi-worker pool; multi-host on Postgres with lease-fenced writes |
| **Model** | Gemini-first | Any provider behind `LLMProvider` |
| **Persistence** | Conversation state, managed | Event ledger; `state = fold(events)` |
| **Suspend / wake** | Session resume | `WakeCondition` + `Dispatcher` + `Lease`, exactly-once |
| **Tool ecosystem** | GCP service integrations | 11 built-in tools + your plugins; MCP |
| **Extension** | Tools + hooks | 16 manifest-declared surfaces |
| **Audit / replay** | Limited | Full event log, fold-reproducible |

Reach for the Cloud Agent SDK when you are all-in on Google Cloud and want
GCP service tools out of the box. Reach for Noeta when you need a durable,
provider-neutral ledger you can audit and replay, and you want to run it as a
multi-worker or multi-host service rather than a single-process client.

## Noeta and Pi Agent

Pi Agent is a computer-use framework: it gives an LLM control of a mouse,
keyboard, and screen capture so the agent can drive a desktop GUI. It is a
control layer over the physical computer, not an agent runtime.

| Concern | Pi Agent | Noeta |
| --- | --- | --- |
| **Deployment** | Desktop process | Multi-worker pool; multi-host on Postgres |
| **What it does** | Lets an LLM click, type, and read the screen | Hosts, records, and schedules agent execution |
| **Persistence** | Ephemeral — no durable execution model | Event-sourced ledger, crash-safe and replayable |
| **Suspend / wake** | Not applicable | First-class: human, timer, subtask, external |
| **Model** | Any LLM (it's a control layer) | Any provider behind `LLMProvider` |
| **Tools** | Mouse / keyboard / screenshot primitives | fs, web, memory, browser, MCP, your plugins |
| **Audit** | None | Full event log |

Pi Agent and Noeta solve different layers. Pi Agent answers "how does the
agent interact with a GUI?" Noeta answers "how does the agent's running become
a durable, auditable ledger?" They are complementary: a Noeta task could
invoke Pi Agent-style computer-use tools through the `browser` built-in or a
custom tool plugin.

## When Noeta is the wrong choice

You run the infrastructure. Multi-host deployments require the Postgres backend;
the SQLite and in-memory backends are single-host. The built-in tool set is
small and there is no plugin marketplace. If "it works against a vendor's API
with no operational surface" is the requirement, a hosted client library is the
lower-friction choice.

## Next

- [Quickstart](../tutorials/quickstart.md) — try it in five minutes
- [Event sourcing](../concepts/event-sourcing.md) — why state is `fold(log)`
- [Known limitations](../operations/limitations.md) — the boundaries in detail
- [Architecture overview](../architecture/overview.md) — the full picture
