# Noeta — a multi-user agent platform on a durable runtime

**English** · [简体中文](README.zh-CN.md)

**[Documentation](https://initxy.github.io/noeta/)** · [Quickstart](https://initxy.github.io/noeta/tutorials/quickstart/) · [Platform reference](https://initxy.github.io/noeta/reference/noeta-agent/) · [SDK reference](https://initxy.github.io/noeta/reference/sdk/)

> **A self-hostable agent service for teams** — multi-user sessions and
> collaboration spaces, per-session sandbox containers, space-scoped skills /
> knowledge / memory / MCP connectors, an admin console — all on top of a
> **durable event-sourced runtime** with full audit and replay. Runs fully
> offline with zero credentials, and speaks to any OpenAI-Responses-compatible
> gateway when you wire one in.

Noeta ships two things in one repo:

- **The platform** (`noeta-agent`) — a deployable multi-user agent service: a
  FastAPI backend plus a React SPA, shipped as one process. People log in,
  work in **spaces** (personal or team), and hold **sessions** with an agent
  that executes only inside a per-session Docker sandbox. Spaces carry the
  agent's skills, knowledge sources, long-term memory, MCP connectors, and
  configuration; admins get a console with usage stats and a raw event trace.
- **The runtime + SDK** (`noeta-runtime`, `noeta-sdk`) — the library
  underneath: durable event-sourced task execution, crash-safe exactly-once
  resume, suspend/wake for humans and timers, worker leases, full audit and
  replay. `noeta.sdk` is the one public import surface for building your own
  agents in-process.

## Quickstart — zero credentials, 60 seconds

No API key, no Docker, no accounts. From a fresh checkout (Python 3.11+ with
[uv](https://docs.astral.sh/uv/), Node 20+):

```bash
git clone https://github.com/initxy/noeta && cd noeta
make install   # uv sync + frontend deps
make run       # build the SPA + boot the platform on http://127.0.0.1:8000
```

Open <http://127.0.0.1:8000>, log in with **any username** (dev-login), and
send a message. With no LLM configured the platform runs the deterministic
**mock provider**: you get a scripted conversation that exercises the real
machinery end-to-end — a clarifying question, a skill activation, a written
answer — fully offline. Prefer explicit steps over `make`?

```bash
uv sync
cd apps/web && npm ci && npm run build && cd ../..
uv run python -m noeta.agent
```

The same assembly, as a program:

<!-- runnable: smoke -->
```python
from noeta.agent.main import create_app

# Fully offline defaults: the deterministic mock LLM, SQLite app storage,
# dev-login. create_app assembles the FastAPI application without serving it.
app = create_app()
assert "/api/v1/health" in app.openapi()["paths"]
```

## Connect a real model

The platform talks to any **OpenAI-Responses-compatible gateway**. Configure
it in `apps/noeta-agent/.env` (copy `.env.example`):

```dotenv
LLM_PROVIDER=auto            # auto = use the gateway when configured, else the offline mock
LLM_BASE_URL=https://your-gateway.example.com/v1
LLM_API_KEY=sk-…
```

`LLM_BASE_URL` is the gateway root — the provider appends `/responses`. The
model menu users pick from lives in `apps/noeta-agent/models.json` (ids,
labels, reasoning-effort levels); an optional second gateway
(`SECONDARY_LLM_BASE_URL` / `SECONDARY_LLM_API_KEY`) serves models tagged
`"gateway": "secondary"` there. See
[`examples/openai-compatible/`](examples/openai-compatible/) for a
copy-paste setup and the
[configuration reference](https://initxy.github.io/noeta/reference/configuration/)
for every key.

## Turn on the sandbox

Execution is **sandbox-only by design**: agent shell and file side effects
happen inside a per-session Docker container, never on the host — there are
no host shell tools and no per-call approval flow. Without Docker the
platform degrades to pure conversation mode (shell execution disabled), which
is exactly what the zero-credential quickstart uses.

```dotenv
SANDBOX_ENABLED=true
```

That's the whole switch — it needs a local Docker daemon and pulls the stock
[AIO Sandbox image](https://github.com/agent-infra/sandbox)
(`ghcr.io/agent-infra/sandbox`). Each session then gets its own container:
the session workspace is bind-mounted read-write, space knowledge and skills
mount read-only, and the web UI streams live **Browser / Terminal / Code**
panels from that same container. Idle containers are reclaimed in two stages
(stop, then remove); a resumed session re-attaches to its container.
[`examples/deployment/`](examples/deployment/) has a docker-compose wrapper.

## What a space gives your agent

Everything the agent brings to a session is scoped to the space it lives in,
managed in the UI (or over the [HTTP API](https://initxy.github.io/noeta/reference/http-api/)):

- **Skills** — uploadable `SKILL.md` packs the model activates on demand;
  platform-wide builtins are admin-managed, space skills are owner-managed.
- **Knowledge sources** — synced `git_repo` / `local_dir` content, mounted
  read-only into the sandbox, with citation resolution back to sources.
- **Agent memory** — a file-based long-term memory pool per space, written by
  the agent's own tools, browsable and editable by members.
- **MCP connectors** — per-space MCP servers (`http` or `stdio`) with
  per-connector tool subsets; credentials never leave the server.
- **Agent-config** — persona prompt, default model and reasoning effort,
  knowledge selection, memory toggle.
- **Templates & workflows** — reusable prompt templates and multi-node
  workflow sessions with generated handoffs between nodes.
- **Feedback loop** — members rate messages; an analysis agent turns ratings
  into suggestions the owner can adopt (into memory or as a skill patch) or
  export as a markdown report.

## The runtime underneath

The platform is the official exercise of the engine it ships on — every
session turn is a durable, event-sourced engine task:

- **Crash-safe, exactly-once execution.** State is folded from an append-only
  event log, never held in memory — kill the process mid-task and a fresh one
  resumes at the exact point, exactly once.
- **Long-horizon tasks.** A task can suspend for hours or days waiting on a
  human answer, a timer, or a sub-task, then wake *exactly once* when the
  condition fires — waiting costs nothing while it sleeps.
- **Full audit & replay.** Every event, LLM turn, tool call, and token/cache
  stat is recorded; compaction is a reversible overlay. The admin trace view
  answers *why* a step happened, not just *what*.
- **Provider-neutral.** Anthropic and OpenAI-compatible adapters sit behind
  one internal protocol — recorded history isn't bound to any vendor's shape.
- **Deterministic offline mode.** The mock provider and dev-login run the
  whole stack with no network, so install, storage, and wiring are provable
  on a fresh checkout (and in CI).

The wire between backend and frontend is deliberately **not** the raw event
log: the backend translates engine events into a stable, versioned UI-event
vocabulary over one SSE stream per session, and replays by re-deriving from
the log (`since_seq`) rather than storing a projection. Raw envelopes stay
available on the admin trace surface. See the
[platform reference](https://initxy.github.io/noeta/reference/noeta-agent/)
for the architecture and the
[server-platform ADR](docs/adr/server-platform-product.md) for the decision.

## Honest limits

The platform's first release draws its boundaries explicitly:

- **Single process, single instance.** App state is SQLite; horizontal
  scaling is future work.
- **Dev-login is the default auth** — any username, signed cookie. It is a
  development affordance; real deployments plug an identity provider into the
  pluggable `AuthProvider` seam.
- **No rate limiting or quotas** yet.
- **Sandbox isolation is process + mounted FS**, not a full jail.

## Use only the layer you need

| Package | You get | Analogous to |
| --- | --- | --- |
| `noeta-runtime` | The pure engine — event log, fold, scheduler, tools, policies. Embed it in-process. | — |
| `noeta-sdk` | The client facade you import: `query()`, `Client`, `Options`, `@tool`. | Claude Agent SDK |
| `noeta-agent` | The multi-user agent platform: FastAPI backend + web SPA + sandbox host. | — |

Install `noeta-sdk` (`uv pip install noeta-sdk`) to build your own agent —
`import noeta.sdk` is the only public surface; the engine underneath is a
transitive dependency you never touch. Run the platform from a checkout as
above. The runnable [`examples/`](examples/) cover both.

## Documentation

Full documentation is rendered at **[initxy.github.io/noeta](https://initxy.github.io/noeta/)**. The same files live under [`docs/`](docs/) for source browsing.

| Layer | Start at | Read it when |
| --- | --- | --- |
| Tutorials | [Quickstart](https://initxy.github.io/noeta/tutorials/quickstart/) | You're new and want it running. |
| How-to guides | [Use the platform](https://initxy.github.io/noeta/how-to/use-the-coding-agent/) | You have a specific task to get done. |
| Concepts | [Event sourcing](https://initxy.github.io/noeta/concepts/event-sourcing/) | You want to understand the design. |
| Reference | [Platform reference](https://initxy.github.io/noeta/reference/noeta-agent/) · [HTTP API](https://initxy.github.io/noeta/reference/http-api/) · [Configuration](https://initxy.github.io/noeta/reference/configuration/) · [SDK](https://initxy.github.io/noeta/reference/sdk/) | You need exact facts. |

Deeper cuts: the [architecture overview](https://initxy.github.io/noeta/architecture/overview/),
[troubleshooting](https://initxy.github.io/noeta/operations/troubleshooting/), and the
[ADRs](https://initxy.github.io/noeta/adr/) recording why each cross-module decision is the way it is
(vocabulary lives in [`CONTEXT.md`](CONTEXT.md)).

## Contributing

Development setup and repository layout live in
[`CONTRIBUTING.md`](CONTRIBUTING.md); working conventions (human or agent)
start at the root [`AGENTS.md`](AGENTS.md) router. `make check` is the local
CI gate; `make e2e-web` runs the opt-in browser e2e suite.

## License

Apache License 2.0 — see [`LICENSE`](LICENSE).
