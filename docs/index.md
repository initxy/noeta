# Noeta

**Open-source, self-hostable runtime for AI agents. Provider-neutral, event-sourced, built for durability.**

Noeta is what you get when you take the agent loop from Claude Code or the
Claude Agent SDK and put it on a durable, inspectable, event-sourced spine —
without locking you to a single vendor or telling you how to write your agent.

Every step an agent takes lands in an append-only **EventLog**, and a task's
entire state is *folded* back from that log. Suspend and resume, crash
recovery, replay, and exactly-once wake are not features bolted on top; they
fall out of treating the log as the single source of truth.

Where an in-process agent library (Claude Agent SDK, LangChain) gives you the
loop, Noeta adds the durable substrate underneath it — so an agent's history
is a log you can fold, inspect, and re-enter, not ephemeral memory that dies
with the process.

## Why Noeta

- **Durable by construction** — every state change is an appended event; task
  state is deterministically folded from the log, never held across runs. Kill
  the process mid-task and fold brings it right back.
- **Provider-neutral** — Anthropic and OpenAI-compatible endpoints are adapters
  behind one internal protocol. Swapping providers is wiring, not a rewrite.
- **Bring your own agent** — the runtime hosts and schedules; you supply the
  policy, tools, and context. A ReAct policy and a coding agent ship in-tree,
  but nothing forces you to use them.
- **Offline-first** — a deterministic `stub` provider runs the whole stack with
  no API key and no network, so install, storage, and wiring are provable on a
  fresh checkout (and in CI).
- **Use the layer you need** — embed the kernel, import the SDK, or run the
  batteries-included coding agent with its bundled web UI.

## Quickstart (no API key)

The `stub` provider is a deterministic two-turn LLM double — no key, no network.

```bash
# Install the coding agent (pulls SDK + runtime transitively).
uv pip install -e apps/noeta-agent
python -m noeta.agent   # boots the offline stub coding agent + bundled web
```

Or from the repo root:

```bash
make install   # first time: editable install + web deps
make run        # build web + boot backend (offline stub, port 8765)
#  → open http://127.0.0.1:8765/chat
```

## Take a look

<figure markdown>
  ![The bundled web app — chat composer with a running task](assets/web-app.png){ width="840" }
  <figcaption>The bundled web app — chat composer with a running task.</figcaption>
</figure>

<figure markdown>
  ![The per-task trace view — the folded event stream](assets/trace.png){ width="840" }
  <figcaption>The per-task trace view — the folded event stream.</figcaption>
</figure>

## Where to go next

<div class="grid cards" markdown>

-   :material-rocket-launch:{ .lg .middle } **Quickstart**

    ---

    5-minute offline smoke test — install, boot the stub agent, view the trace.

    [:octicons-arrow-right-24: Start here](tutorials/quickstart.md)

-   :material-lightbulb-on-outline:{ .lg .middle } **Core Concepts**

    ---

    Event sourcing, Task model, Engine & execution, Fold & snapshot, Wake & resume, and more.

    [:octicons-arrow-right-24: Learn the model](concepts/event-sourcing.md)

-   :material-console:{ .lg .middle } **Noeta Agent**

    ---

    The bundled coding agent: tools, presets, skills, permission model, env config.

    [:octicons-arrow-right-24: Use the agent](reference/noeta-agent.md)

-   :material-api:{ .lg .middle } **API Reference**

    ---

    SDK API, HTTP routes, WorkerLoop, presets, tools, glossary.

    [:octicons-arrow-right-24: Browse the API](reference/sdk.md)

</div>

## Architecture

For the top-down architecture walkthrough — event-sourced engine, three-package
layout, provider adapters, context composition — see the
[architecture overview](architecture/overview.md).

For the why behind cross-module decisions, browse the
[Architecture Decision Records](adr/index.md).
