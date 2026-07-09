# Noeta — the server-side agent runtime

**English** · [简体中文](README.zh-CN.md)

**[Documentation](https://initxy.github.io/noeta/)** · [Quickstart](https://initxy.github.io/noeta/tutorials/quickstart/) · [SDK reference](https://initxy.github.io/noeta/reference/sdk/) · [Configure a provider](https://initxy.github.io/noeta/how-to/configure-provider/)

> **Built to host AI agents on a server — not in a notebook.** Durable event-sourced execution, multi-worker / multi-host scheduling, per-session sandbox containers, full audit & replay, and provider-neutral LLM wiring. Self-hostable, no vendor lock-in.

Noeta is a **runtime for running AI agents server-side**. It hosts, records,
schedules, and replays agent execution on top of a **durable event log** — the
same spine production systems use for exactly-once delivery and crash safety.
That one design choice buys you what a normal in-process agent library can't:

- **Crash-safe execution.** A task survives a process kill and resumes exactly
  where it left off — state is folded from the log, never held in memory.
- **Long-horizon tasks.** A task can pause for hours or days waiting on a human,
  a timer, or a sub-task, then wake *exactly once* when the condition fires.
- **Full audit & replay.** Every LLM turn, tool call, and approval is a recorded
  event, so you can inspect *why* a step happened, not just *what*.

Because it's engineered for the **server** — multi-tenant, long-running,
untrusted-code — it adds what that demands:

- **Multi-worker / multi-host** scheduling on shared Postgres (lease fencing,
  database-clock expiry) — scale out by adding workers.
- **Per-session sandboxing** — flip one switch and every session runs in its own
  throwaway Docker container; all fs / shell / skill / web tool calls execute
  *inside* it, never on the host.
- **Human-in-the-loop end-to-end** — approvals, structured questions, timer wake
  all survive restarts.
- **Provider-neutral** — Anthropic and any OpenAI-compatible model behind one
  internal protocol; swap vendors without rewriting history.

And the whole stack runs **offline with no API key** via a deterministic `stub`
provider, so you can try it in thirty seconds.

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

## Why Noeta — server-side strengths

Noeta is built for the realities of hosting agents on a server: crashes
happen, tasks outlive a single request, you run code you don't fully trust, and
you need to scale across workers. These aren't afterthoughts — they're the
foundation.

### Durable, crash-safe execution

- **Survives crashes** — a task's state is never held in memory across runs. It
  is rebuilt (*folded*) from an append-only event log on demand. Kill the
  process mid-task; a fresh one folds the log back to the exact point and
  finishes the work — exactly once.
- **Long-horizon by design** — a task can suspend to wait on a human approval,
  a structured question, a timer, or a sub-task, and is woken exactly once when
  the condition fires. Waiting costs nothing while it sleeps.

### Scale-out scheduling

- **Multi-worker, multi-host** — tasks are leased from a shared Dispatcher
  backed by Postgres. Add workers to scale out; lease fencing + database-clock
  expiry keep exactly-once semantics across machines. SQLite and in-memory
  backends stay single-host for local dev.

### Per-session isolation

- **One container per session** — turn on the sandbox and each session gets its
  own fresh Docker container; every fs / shell / skill / web tool call runs
  *inside* it, never on the host, and one session's files and processes are
  invisible to another. Built for hosting agents — and running code — you don't
  fully trust.
- **Reconnect-safe** — the container is recorded in the log by address; a
  worker that folds the task back after a crash reconnects to the *same*
  container and keeps its working files.
- **Credentials off the command line** — the container key is handed to `docker`
  by name, never as an argv value.

### Full observability

- **Fully inspectable** — every event, LLM turn, tool call, and token/cache
  stat is a recorded event. The trace view (and the raw log) answers *why* a
  step happened — which tool ran on whose authority, what got compacted away —
  not just *what*.
- **Replayable** — compaction is a recorded overlay; the original messages stay
  in the log, so history is auditable and reproducible.

### Provider-neutral, no lock-in

- **Anthropic or any OpenAI-compatible endpoint** sit behind one internal
  protocol. Swapping vendors is wiring, not a rewrite, and the recorded history
  isn't bound to any vendor's shape.
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
don't trust the agent, inside a per-session sandboxed container (below) — the
log looks the same either way, so crash-recovery and replay are unchanged. See
[event sourcing](https://initxy.github.io/noeta/concepts/event-sourcing/)
and [wake & resume](https://initxy.github.io/noeta/concepts/wake-resume/) for the full picture.

## Per-session sandbox

When you host agents on a server — for other people, or just for code you don't
fully trust — you don't want tool calls touching the host. Flip one switch and
Noeta provisions a **fresh Docker container per session** and routes *every*
side-effecting tool into it: file read / write / edit / patch, foreground
`shell_run`, skill discovery and skill scripts, and `webfetch` / `web_search`
all execute inside that container, not on the host.

- **One container per session.** Each root task (a conversation) gets its own
  named container, provisioned when the session starts and torn down when it
  ends. Two concurrent sessions get two separate containers — one's files and
  processes are invisible to the other.
- **Everything runs through the box.** fs, shell, skills, and web egress all go
  through the container over the same HTTP seam, so the durable event log is
  byte-identical whether a tool ran on the host or in the container — resume and
  crash-recovery don't care which.
- **Reconnect-safe.** The container is recorded in the log by address; a worker
  that folds the task back after a crash reconnects to the *same* container
  (same host) and keeps its working files.
- **Credentials stay off the command line.** The container key is handed to
  `docker` by name, never as an argv value; third-party keys (e.g. the web-search
  key) are delivered to in-container tools out-of-band, not on a process command
  line the container's process table would expose.

Enable it — needs a local Docker daemon and the AIO Sandbox image
([`agent-infra/sandbox`](https://github.com/agent-infra/sandbox)):

```bash
export SANDBOX_API_KEY=$(openssl rand -hex 16)   # the container's API key
NOETA_AGENT_SANDBOX=1 python -m noeta.agent       # per-session containers, on
```

Tune the image and caps with `NOETA_AGENT_SANDBOX_IMAGE` /
`NOETA_AGENT_SANDBOX_MEMORY` / `NOETA_AGENT_SANDBOX_CPUS`. Global user **memory**
and **MCP** servers deliberately stay on the host. The isolation is
process-plus-mounted-FS, not a full jail; see
[known limitations](https://initxy.github.io/noeta/operations/limitations/) for
the mount-isolation level, idle-container cost, and the cross-machine reconnect
boundary.

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

Pi, the **Claude Agent SDK** (a.k.a. the Claude Code SDK), and the **Codex SDK**
are all *in-process harnesses*: they give you an agent loop, tools, MCP, and
sub-agents — then leave storage, distribution, and sandboxing to you. Their
"persistence" is a local transcript file (the Claude Agent SDK ships an
in-memory session store its own docs mark *not for production*; Codex keeps
threads under `~/.codex/sessions`), resume is single-host and path-bound, and
scaling across tenants is your problem to build.

**Noeta is the server-side foundation underneath that harness** — the part you'd
otherwise write yourself:

| | **Noeta** | Claude Agent SDK | Codex SDK | Pi |
| --- | --- | --- | --- | --- |
| Agent loop · tools · MCP · sub-agents | ✅ | ✅ | ✅ | ✅ |
| Durable state | **Event-sourced log**, folded to resume — Postgres / SQLite / memory | Local transcript; default store *not for production* — bring your own DB | Local thread files | Local session files |
| Crash-safe, exactly-once resume | ✅ any process re-folds the log | Same host only, `cwd` must match — durability is on you | Resume one thread locally | Reopen a transcript |
| Multi-worker / multi-host | ✅ lease fencing on shared Postgres | ✗ one long-lived process per agent | ✗ one process per thread | ✗ |
| Per-session sandbox | ✅ throwaway container per session, reconnect-safe | Your infra to build (or vendor-hosted) | Local OS sandbox only | ✗ |
| Ships as | **Self-hostable server base** | Harness you host + deploy | Local CLI + SDK wrapper | In-process harness |

Same idea underneath: the harnesses record a *conversation*; Noeta records
*events*, and state is folded from them. That one ledger is what turns crash
recovery, durable multi-host wake, per-session sandbox reconnect, reversible
compaction, and full audit into one mechanism instead of five bespoke systems
you bolt on later. Provider-neutral and self-hosted — no vendor lock-in.

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
- **The per-session sandbox is opt-in** — off by default (it needs a local
  Docker daemon + the AIO image). It provisions one container per session and
  isolates process + mounted FS, not a full jail; warm pools / pause-resume and
  cross-machine reconnect are future work (a distributed / NAS-backed provider
  seam is already carved out for them).
- **The web app is a small Vite MPA** with vanilla ES modules; no framework
  migration is planned for the preview.

## Contributing

Development setup and repository layout live in
[`CONTRIBUTING.md`](CONTRIBUTING.md); working conventions (human or agent)
start at the root [`AGENTS.md`](AGENTS.md) router.

## License

Apache License 2.0 — see [`LICENSE`](LICENSE).
