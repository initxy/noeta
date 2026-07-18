# The noeta-agent platform (`python -m noeta.agent`)

The official Noeta product is a **deployable multi-user agent service**: a
FastAPI backend plus a React/TypeScript SPA, shipped as one process, that
provisions one sandbox container per session for agent execution. People log
in, collaborate in **spaces**, and hold **sessions** with an agent whose
skills, knowledge, memory, MCP connectors, and configuration are scoped to
the space. The governing decision is
[ADR: server-platform product](https://github.com/initxy/noeta/blob/main/docs/adr/server-platform-product.md).

## Boot

The **only** entry point is `python -m noeta.agent` — zero arguments, all
configuration through `apps/noeta-agent/.env` and environment variables
(see [Configuration](configuration.md)). It serves the REST + SSE API under
`/api/v1/*` and the built SPA from `apps/web/dist` on one port (default
8000). From a checkout, the Makefile wraps the common flows:

```bash
make install   # first time: uv sync + frontend deps
make run       # build the SPA + python -m noeta.agent   → http://127.0.0.1:8000
make dev       # hot reload: backend on 8000 + vite dev server on 5273 (proxied)
```

### Boot modes

- **Zero-credential (default).** Everything left empty: the deterministic
  **mock provider** (a scripted FakeLLM demo — question, skill activation,
  written answer), **dev-login** (any username), SQLite storage, sandbox
  off. Fully offline; this is also what the test suite and CI run.
- **Real gateway.** Set `LLM_BASE_URL` + `LLM_API_KEY` to any
  OpenAI-Responses-compatible gateway (`/responses` is appended); define the
  model menu in `models.json`; optionally add a secondary gateway for
  per-model routing. See [Connect an OpenAI-compatible gateway](../how-to/configure-provider.md).
- **Sandbox on.** `SANDBOX_ENABLED=true` + a local Docker daemon + the stock
  [AIO Sandbox image](https://github.com/agent-infra/sandbox). Each session
  gets its own container with live Browser / Terminal / Code preview panels.

## Architecture

A **modular monolith**: one process, one deployable unit, seams as
interfaces rather than services.

```text
apps/web (React SPA)  ──  /api/v1 REST + per-session SSE
        │
noeta.agent.api        routers (auth, sessions, spaces, skills, knowledge,
        │              mcp, templates, memories, feedback, channels, admin)
noeta.agent.auth       the AuthProvider seam (dev-login reference impl)
noeta.agent.host       the engine host: AgentService (embedded noeta.sdk
        │              Client + worker pool), the envelope→UI translator,
        │              provider assembly, the Docker sandbox provider
noeta.agent.store      application SQLite (users, spaces, sessions, …)
noeta.agent.services   knowledge sync/resolve, channels, feedback analysis
        │
     noeta.sdk         the only crossing into the engine
```

Key structural decisions:

- **Sessions and spaces are app-layer indexing only.** A session groups one
  or more engine tasks (a workflow session owns one root task per node) and
  owns one workspace directory and one sandbox. Below the application layer
  the engine knows only Tasks; every state change flows through `noeta.sdk`
  `Client` verbs and the EventLog stays the single source of truth.
- **The wire is translated, not raw.** The backend translates canonical
  engine events into a flat UI-event vocabulary through a deterministic,
  stateless pure function (`noeta/agent/host/translator.py`), streamed over
  **one SSE stream per session**. Replay is **re-derivation** from the
  EventLog via a `since_seq` cursor — there is no stored UI projection that
  can drift. Token deltas ride the stream as ephemeral frames, never
  persisted, never replayed. Raw envelopes are served only on the admin
  trace surface. Full vocabulary: [HTTP API reference](http-api.md).
- **Execution is sandbox-only.** Agent shell and file side effects happen
  only inside the per-session container; the host exposes no shell tools and
  there is **no per-call approval flow** (host execution with approvals was
  a single-user affordance; on a shared server it is a privilege-escalation
  surface). The session workspace mounts read-write; space knowledge and
  skills mount read-only. Without Docker the platform degrades to pure
  conversation mode with shell execution disabled.
- **Auth is a seam, not a feature.** Every request authenticates through the
  `AuthProvider` interface (`noeta/agent/auth/provider.py`); the open-source
  distribution ships `DevLoginProvider` (any username, signed session
  cookie) and keeps the seam open for OIDC/SSO. No vendor identity system
  lives in the core.

## The session / space model

- Every user gets a **personal space**; **team spaces** have owner-managed
  membership (roles: owner / member). Session visibility = space membership.
- A space scopes the agent's working material: **skills** (builtin +
  space-uploaded), **knowledge sources** (`git_repo` / `local_dir` sync),
  **long-term memory**, **MCP connectors** (per-space aliases with tool
  subsets, resolved into the host per turn), **agent-config** (persona
  prompt, default model/effort, knowledge selection, memory toggle), and
  **templates / workflow templates**.
- Sessions are plain conversations, template starts, or **multi-node
  workflow sessions** — each node is its own root task, advanced through a
  generated, user-confirmed handoff document.
- A **feedback loop** turns member ratings into owner-gated suggestions
  (adopt into memory, apply a skill patch, or export a markdown report).

## Admin console

Admin is a **role, not a deployment**: usernames listed in `ADMIN_USERS` get
the console on the same server (everyone else sees 404 on `/api/v1/admin/*`).
It provides usage stats (users / spaces / sessions / skills / knowledge),
cross-space listings and drilldowns, builtin-skill management, dynamic
config (e.g. hot-toggling dev-login), and the **raw event trace** — the
untranslated envelope stream for any session, folded client-side in the
trace UI. The trace surface is a diagnostics tool, deliberately separate
from the product wire.

## Honest limits (v1)

- **Single process, single instance** — app state is SQLite; horizontal
  scaling is future work.
- **Dev-login is the default auth**; real deployments must plug in an
  identity provider.
- **No rate limiting or quotas.**
- **Sandbox isolation is process + mounted FS**, not a full jail.

## Deployment

The platform runs fine bare: `uv` + a writable `DATA_DIR` + (optionally)
Docker for the sandbox. [`examples/deployment/`](https://github.com/initxy/noeta/tree/main/examples/deployment)
ships an optional docker-compose packaging (app container + sandbox access +
data volume).

## See also

- [HTTP API reference](http-api.md) — every route and the SSE vocabulary
- [Configuration](configuration.md) — every `.env` key and default
- [How-to: use the platform](../how-to/use-the-coding-agent.md) — the UI walkthrough
- [Connect an OpenAI-compatible gateway](../how-to/configure-provider.md)
