# Noeta

**English** · [简体中文](README.zh-CN.md)

**[Documentation](https://initxy.github.io/noeta/)** · [Quickstart](https://initxy.github.io/noeta/tutorials/quickstart/) · [SDK reference](https://initxy.github.io/noeta/reference/sdk/) · [Configure a provider](https://initxy.github.io/noeta/how-to/configure-provider/)

> Open-source, self-hostable runtime for AI agents. Provider-neutral, event-sourced, built for durability.

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

<p align="center">
  <img src="docs/assets/crash-resume.gif" alt="kill -9 a live worker mid-task; a second process folds the task back and finishes it" width="820">
  <br>
  <em><code>kill -9</code> a worker mid-task; a second process folds the task back from the log and finishes it — <a href="examples/crash_resume.py">examples/crash_resume.py</a>, fully offline.</em>
</p>

<p align="center">
  <img src="docs/assets/web-app.png" alt="Noeta coding-agent web app" width="820">
  <br>
  <em>The bundled coding-agent web app, served by <code>python -m noeta.agent</code>.</em>
</p>

<p align="center">
  <img src="docs/assets/trace.png" alt="Noeta per-task trace view" width="820">
  <br>
  <em>The per-task trace view — every event, LLM turn, and token/cache stat, straight from the EventLog.</em>
</p>

## Why Noeta

- **Durable by construction** — every state change is an appended event; task
  state is deterministically folded from the log, never held across runs. Kill
  the process mid-task and fold brings it right back.
- **Provider-neutral** — Anthropic and OpenAI-compatible endpoints are adapters
  behind one internal protocol. Swapping providers is wiring, not a rewrite; no
  vendor's shape leaks into the core.
- **Bring your own agent** — the runtime hosts and schedules; you supply the
  policy, tools, and context. A ReAct policy and a coding agent ship in-tree,
  but nothing forces you to use them.
- **Offline-first** — a deterministic `stub` provider runs the whole stack with
  no API key and no network, so install, storage, and wiring are provable on a
  fresh checkout (and in CI).
- **Use the layer you need** — embed the kernel (`noeta-runtime`), import the
  SDK (`noeta-sdk`), or run the batteries-included coding agent with its
  bundled web UI (`noeta-agent`). Each distribution pulls the layers below it.

## Quickstart

```bash
pip install noeta-agent        # pulls the SDK + runtime
python -m noeta.agent          # boots the offline stub coding agent + bundled web UI
```

No API key needed — the default `stub` provider is a deterministic LLM double.
Open the printed URL and send a message. The same boot, as a program:

<!-- runnable: smoke -->
```python
from noeta.agent.backend.lifecycle import BackendConfig, serve_backend

# Defaults are fully offline: the two-turn stub provider, :memory: storage.
# port=0 binds an OS-assigned port. Workspace is the current directory.
config = BackendConfig(port=0)
server, url, shutdown = serve_backend(config)
try:
    assert url.startswith("http://")
finally:
    shutdown()
```

For the guided path — install, run, open the web UI, read a trace — see the
[quickstart tutorial](https://initxy.github.io/noeta/tutorials/quickstart/). To wire a real Anthropic
or OpenAI-compatible model, see
[configure a provider](https://initxy.github.io/noeta/how-to/configure-provider/). To build your own
agent on the SDK — define a `@tool`, assemble `Options`, call `query()` — start
with [your first agent](https://initxy.github.io/noeta/tutorials/first-agent/) and the runnable
[`examples/`](examples/).

How does it compare to the Claude Agent SDK? Both give you an agent loop,
tools, MCP, and sub-agents; they differ in the spine underneath — see the
[server-side comparison](https://initxy.github.io/noeta/reference/comparison/).

## Documentation

Full documentation is rendered at **[initxy.github.io/noeta](https://initxy.github.io/noeta/)**. The same files live under [`docs/`](docs/) in this repo for source browsing.

| Layer | Start at | Read it when |
| --- | --- | --- |
| Tutorials | [Quickstart](https://initxy.github.io/noeta/tutorials/quickstart/) | You're new and want it running. |
| How-to guides | [Configure a provider](https://initxy.github.io/noeta/how-to/configure-provider/) | You have a specific task to get done. |
| Concepts | [Event sourcing](https://initxy.github.io/noeta/concepts/event-sourcing/) | You want to understand the design. |
| Reference | [SDK reference](https://initxy.github.io/noeta/reference/sdk/) | You need exact API facts. |

Deeper cuts: the [architecture overview](https://initxy.github.io/noeta/architecture/overview/),
[troubleshooting](https://initxy.github.io/noeta/operations/troubleshooting/), and the
[ADRs](https://initxy.github.io/noeta/adr/) recording why each cross-module decision is the way it is
(vocabulary lives in [`CONTEXT.md`](CONTEXT.md)).

## Status & scope

Noeta is an early, pre-1.0 preview. It runs, it is tested, and the core is
stable, but some capabilities are intentionally out of scope for now:

- **Single-host / single-worker.** The shipped worker drains the dispatcher
  in-process and is a preview, not a production daemon. Single-worker durable
  exactly-once wake is shipped; multi-host coordination, multi-worker fencing,
  and the partial-step-orphan edge (a crash mid-step) remain open — see
  [known limitations](https://initxy.github.io/noeta/operations/limitations/).
- **Human-in-the-loop / timer wake** — approvals, structured questions, and
  timer wake are shipped end-to-end; what's missing is out-of-band
  notification (webhook / inbox) when a task starts waiting on a human.
- **Frontend** — the shipped web app is a small Vite MPA with vanilla ES
  modules; no framework migration is planned for the preview.

## Contributing

Development setup and repository layout live in
[`CONTRIBUTING.md`](CONTRIBUTING.md); working conventions (human or agent)
start at the root [`AGENTS.md`](AGENTS.md) router.

## License

Apache License 2.0 — see [`LICENSE`](LICENSE).
