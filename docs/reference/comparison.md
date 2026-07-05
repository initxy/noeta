# Comparison: Noeta vs Claude Agent SDK, LangGraph & Temporal

Noeta and the Claude Agent SDK both give you an agent loop, tools, MCP,
sub-agents, and sessions. The difference is not in those nouns — it is in
the spine underneath. Noeta makes state event-folded; the SDK does not.

## Head-to-head

| Server-side concern | Claude Agent SDK | Noeta |
| --- | --- | --- |
| **Who owns the substrate** | Anthropic hosts the model; you host the loop in-process | You own the whole stack — loop, durable store, wake machinery |
| **State / session model** | Session JSONL — an append-style conversation recording, auto-persisted | Event-sourced log + content-addressed store; state = `fold(events)` |
| **Recovery / resume** | Resume or fork by session id; replay the conversation to continue | Fold is recovery — refolding the log restores state with no separate load logic |
| **Suspend / wake** | Resume / fork by session id; no first-class durable wake | First-class durable wake: a suspended task is matched by projection and re-enqueued, exactly-once |
| **Context compaction** | Auto-summary, irreversible; interceptable by a `PreCompact` hook | Compaction is a recorded event — auditable, reproducible, original history never scrubbed |
| **Provider** | Configures multiple backends (Anthropic / Bedrock / Vertex / Azure), but Anthropic-centric | Vendor-neutral internal protocol; each vendor behind an adapter; the kernel is forbidden to depend on any vendor |
| **Tools** | Built-in tools + `@tool` + in-process SDK MCP server | Built-in tools + `@tool` (with `version` / `risk_level`) + MCP (stdio / HTTP) |
| **Permissions** | `permission_mode` + `canUseTool` + a hook chain | `permission_mode` + Guards (permission-before-acting) |
| **Extension** | Hooks, imperative interception (`PreToolUse`, `PostToolUse`, …) | Five extension seams (tools, policy, guards, observers, content channels) plus the single-writer constraint (observers are read-only) |
| **Sub-agents** | Agent definitions; output returns to the parent; nesting ≤ 5 levels | Subtasks are independent event-sourced tasks; fan-out concurrency; results flow back via a `SubtaskCompleted` wake |
| **Concurrency / distribution** | A single `query` / `Client` in-process | A distributed-queue substrate of lease + durable log (currently shipping single-machine) |
| **Shape** | A TypeScript / Python library sending straight to the Claude API | Three packages — `noeta-runtime` (engine), `noeta-sdk` (client facade), `noeta-agent` (bundled app) |

## When each wins

**Reach for the Claude Agent SDK when** you want an agent loop out of the
box, tracking official Anthropic capabilities closely, with minimal
operational burden. It is a well-hosted client library — install, point at
your API key, and go.

**Reach for Noeta when** you need to own the execution substrate: durable
replay and audit of every step, provider portability across vendors, the
ability to host an agent as a long-running service with crash recovery,
or to fold state from a log that outlives any individual process. The
cost is that you run the infrastructure.

These are not competitors so much as answers to different questions. The
SDK asks "how do I give my code an agent loop?" Noeta asks "how do I make
an agent's running into a ledger I can replay, audit, and carry
elsewhere?"

> **Honest caveats.** Noeta is an early pre-1.0 preview. The shipped
> deployment is single-host / single-worker; multi-host coordination and
> multi-worker fencing are not yet shipped. The ecosystem is smaller —
> fewer built-in tools, no plugin marketplace, a younger community. If
> "it just works against Anthropic's API" is the primary requirement, the
> Claude Agent SDK is the lower-friction choice today.

## Three differences spelled out

**The shape of ground truth.** Session JSONL is also an append log, but
what it records is the *conversation*. Noeta records *events*, and state
is a projection folded out of those events. One is like a recording of a
conversation; the other is like a state machine's ledger. The ledger lets
resume, compaction, and audit all land on one mechanism — resume is a
refold, compaction is a recorded event, audit is another fold. The
recording model needs a separate set of logic for each.

**The reversibility of compaction.** The SDK's auto-compaction is
summary-style: the original content is displaced by a summary, and the
process is irreversible (to archive it you rely on a `PreCompact` hook
grabbing a copy yourself). Noeta's compaction only records a summary
boundary into the log; the original messages are still there, and the
summary is overlaid at context-assembly time. So the same task, recovered,
compacts the same way — and you can dig up what was actually pared away.

**The provider boundary.** The SDK supports multiple backends, but the
shape is Anthropic-centric — the message format, the tool calling
convention, the reasoning model. Noeta makes the internal protocol a
vendor-neutral canonical, then enforces that the kernel depends on no
vendor SDK with an import-linter rule. The cost is an extra adapter layer
per vendor; the return is recordings that are not bound to a vendor, and
tasks you can fold and audit without installing any vendor's SDK.

## Noeta vs LangGraph

LangGraph is the closest open-source neighbor: it also persists agent
state, supports human-in-the-loop interrupts, and can rewind ("time
travel") to earlier points. The difference is what gets persisted and
what machinery ships around it.

| Concern | LangGraph | Noeta |
| --- | --- | --- |
| **Unit of persistence** | A checkpoint per super-step: a snapshot of the graph's channel state | An append-only event ledger; state is `fold(events)`, never stored as the primary copy |
| **What history means** | A list of state snapshots you can rewind to or fork from | A causal record — every event carries `actor` / `causation_id` / `trace_id`, so history answers *why*, not just *what* |
| **Control flow** | You define a graph of nodes and edges; the model routes within it | No graph. The Policy decides each step; task structure emerges from decisions |
| **Resume / wake** | Caller re-invokes the thread with a resume command; queues and crons live in the hosted Platform | Dispatcher + lease + worker with durable, exactly-once wake ships in the open-source core |
| **Compaction** | Left to the application (or bolt-on memory libraries) | A recorded, reversible event — the summary overlays at compose time, originals stay in the log |
| **Ecosystem** | Mature, large integration catalog, token streaming, big community | Young, smaller toolset, no token streaming yet |

**Reach for LangGraph when** you want to express your agent as a graph,
lean on the LangChain integration catalog, and need streaming and
ecosystem maturity today.

**Reach for Noeta when** the question is auditability and substrate
ownership: a snapshot tells you what the state *was*; a ledger tells you
what *happened* — which tool ran on whose authority, what was compacted
away, what woke a sleeping task. And the scheduling machinery (leases,
durable wake, crash reclaim) is part of the open-source core, not a
hosted product.

## Noeta vs Temporal (a brief note)

Temporal is a workflow engine: you define a DAG of activities in code,
and Temporal durably schedules and retries them. It is excellent for
human-orchestrated business processes with durable timers.

Noeta is an agent runtime: the LLM drives the control flow dynamically,
not a pre-defined graph. A task's structure emerges from the model's
decisions, not from a workflow definition. They solve different problems
— Temporal for when you know the shape of the work ahead of time, Noeta
for when the model discovers it as it goes.

## See also

- [Event sourcing](../concepts/event-sourcing.md) — why state = fold(log)
- [Wake & resume](../concepts/wake-resume.md) — the delivery guarantee
- [Architecture overview](../architecture/overview.md) — the full picture
