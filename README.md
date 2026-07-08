# Noeta

**English** · [简体中文](README.zh-CN.md)

**[Documentation](https://initxy.github.io/noeta/)** · [Quickstart](https://initxy.github.io/noeta/tutorials/quickstart/) · [SDK reference](https://initxy.github.io/noeta/reference/sdk/) · [Configure a provider](https://initxy.github.io/noeta/how-to/configure-provider/)

> Open-source, self-hostable runtime for AI agents — durable, inspectable, and provider-neutral.

Noeta runs the agent loop — tools, sub-agents, MCP, human-in-the-loop — on top
of a **durable event log**. That one design choice buys three things a normal
in-process agent library can't give you:

- **A task survives a crash** and resumes exactly where it left off.
- **A task can pause for hours or days** waiting on a human, a timer, or a
  sub-task, then wake exactly once when the condition is met.
- **Every step is recorded** — each LLM turn, tool call, and approval — so you
  can inspect, audit, and replay what the agent actually did.

It talks to Anthropic and any OpenAI-compatible model behind one internal
protocol, so you're never locked to a vendor. And it runs the whole stack
**offline with no API key**, so you can try it in thirty seconds.

<p align="center">
  <img src="docs/assets/web-app.png" alt="Noeta coding-agent web app" width="820">
  <br>
  <em>The bundled coding-agent web app — one command (<code>python -m noeta.agent</code>) boots the agent and this UI.</em>
</p>

<p align="center">
  <img src="docs/assets/trace.png" alt="Noeta per-task trace view" width="820">
  <br>
  <em>Every task has a full trace — each event, LLM turn, and token/cache stat, read straight from the event log.</em>
</p>

<p align="center">
  <img src="docs/assets/crash-resume.gif" alt="A running agent is killed mid-task; a fresh process rebuilds it from the log and finishes the work" width="820">
  <br>
  <em><strong>Crash safety, demonstrated.</strong> A running agent is killed with <code>kill -9</code> mid-task — no cleanup, no flush. A fresh process reopens the same store, rebuilds the task's exact state from its event log, and runs it to completion, exactly once. Nothing was held in memory to lose. — <a href="examples/crash_resume.py">examples/crash_resume.py</a>, fully offline.</em>
</p>

## Why Noeta

- **Survives crashes** — a task's state is never held in memory across runs. It
  is rebuilt (*folded*) from an append-only event log on demand. Kill the
  process mid-task; a fresh one folds the log back to the exact point and
  finishes the work — exactly once.
- **Fully inspectable** — every event, LLM turn, tool call, and token/cache
  stat is a recorded event. The trace view (and the raw log) answers *why* a
  step happened — which tool ran on whose authority, what got compacted away —
  not just *what*.
- **Long-horizon by design** — a task can suspend to wait on a human approval,
  a structured question, a timer, or a sub-task, and is woken exactly once when
  the condition fires. Waiting costs nothing while it sleeps.
- **Provider-neutral** — Anthropic and any OpenAI-compatible endpoint sit
  behind one internal protocol. Swapping vendors is wiring, not a rewrite, and
  the recorded history isn't bound to any vendor's shape.
- **Bring your own agent** — the runtime hosts and schedules; you supply the
  policy, tools, and context. A ReAct policy and a full coding agent ship
  in-tree, but nothing forces you to use them.
- **Runs offline out of the box** — a deterministic `stub` provider runs the
  whole stack with no API key and no network, so install, storage, and wiring
  are provable on a fresh checkout (and in CI).

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

Next steps: the [quickstart tutorial](https://initxy.github.io/noeta/tutorials/quickstart/)
walks the guided path (install → run → open the web UI → read a trace). To wire
a real Anthropic or OpenAI-compatible model, see
[configure a provider](https://initxy.github.io/noeta/how-to/configure-provider/).
To build your own agent on the SDK — define a `@tool`, assemble `Options`, call
`query()` — start with
[your first agent](https://initxy.github.io/noeta/tutorials/first-agent/) and the
runnable [`examples/`](examples/).

## How it works

One idea sits underneath everything: **state is a fold over a log, not a thing
held in memory.**

Every step an agent takes — each LLM turn, tool call, approval, suspend — is
appended to a per-task **event log**. The task's current state is *folded*
(replayed) from that log whenever it's needed. Nothing durable lives in process
memory between runs.

Because the log is the single source of truth, the hard parts stop being
separate features and become the *same* mechanism:

- **Resume** is just a re-fold — reopen the log, fold it, keep going.
- **Crash recovery** is a re-fold by a different process.
- **Suspend / wake** is a task parked on a condition, matched and re-enqueued
  exactly once.
- **Compaction** is a recorded event — a summary is overlaid at compose time;
  the original messages stay in the log, so it's auditable and reproducible.

Large objects (tool outputs, files, snapshots) live in a content-addressed
store the log points into. Tool side effects can run on the host or, when you
don't trust the agent, inside a sandboxed container — the log looks the same
either way. See [event sourcing](https://initxy.github.io/noeta/concepts/event-sourcing/)
and [wake & resume](https://initxy.github.io/noeta/concepts/wake-resume/) for the full picture.

## Use only the layer you need

Noeta ships as three packages, each pulling in the ones below it:

| Package | You get | Analogous to |
| --- | --- | --- |
| `noeta-runtime` | The pure engine — event log, fold, scheduler, tools, policies. Embed it in-process. | — |
| `noeta-sdk` | The client facade you import: `query()`, `Client`, `Options`, `@tool`. | Claude Agent SDK |
| `noeta-agent` | The batteries-included coding agent + web UI + HTTP/SSE server. | Claude Code |

Install `noeta-sdk` to build your own agent (`import noeta.sdk`); install
`noeta-agent` to run the bundled product. The only public surface is
`noeta.sdk` — the engine underneath is a transitive dependency you never touch.

## How it compares

Both Noeta and the Claude Agent SDK give you an agent loop, tools, MCP, and
sub-agents. The difference is the spine underneath: the SDK records a
*conversation*; Noeta records *events*, and state is folded from them. That
ledger is what makes crash recovery, durable wake, reversible compaction, and
full audit land on one mechanism instead of four.

See the [full comparison](https://initxy.github.io/noeta/reference/comparison/)
against the Claude Agent SDK, LangGraph, and Temporal.

## Documentation

Full documentation is rendered at **[initxy.github.io/noeta](https://initxy.github.io/noeta/)**. The same files live under [`docs/`](docs/) for source browsing.

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
stable — but some edges are intentionally bounded:

- **Concurrency & recovery are shipped, with limits.** Single-host
  multi-worker pools, multi-host coordination on shared Postgres
  (lease fencing, database-clock expiry), durable exactly-once wake, and
  mid-step crash recovery all work today. Still bounded: multi-host fencing is
  Postgres-only (SQLite / in-memory stay single-host), and a crashed step's
  side effects are surfaced for review, not automatically undone — see
  [known limitations](https://initxy.github.io/noeta/operations/limitations/).
- **Human-in-the-loop is end-to-end** — approvals, structured questions, and
  timer wake all work; what's missing is out-of-band notification (webhook /
  inbox) when a task starts waiting on a human.
- **The web app is a small Vite MPA** with vanilla ES modules; no framework
  migration is planned for the preview.

## Contributing

Development setup and repository layout live in
[`CONTRIBUTING.md`](CONTRIBUTING.md); working conventions (human or agent)
start at the root [`AGENTS.md`](AGENTS.md) router.

## License

Apache License 2.0 — see [`LICENSE`](LICENSE).
