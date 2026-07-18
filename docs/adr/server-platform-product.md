# The official product is a multi-user agent service: app-layer sessions and spaces, a translated UI-event stream, sandbox-only execution; the single-user local app is retired

## Context

`docs/adr/runtime-sdk-app-restructure.md` fixed the library boundary — noeta-runtime is the pure engine, `noeta.sdk` the only public surface, HTTP/SSE lives only in the application — and shipped an official app shaped as a **single-user, local, no-auth** coding agent whose frontend folds the raw envelope stream.

That app form undersells the runtime. The engine's differentiating properties — durable event-sourced tasks, crash-safe resume, worker leases, replayable history — only become visible in a **long-lived service shared by multiple people**, not in a single-user local tool. A full production application with exactly that service shape has since been built downstream on `noeta-sdk` (a FastAPI backend with multi-user auth, collaboration spaces, skill/knowledge management, an admin console, and a React/TypeScript SPA) and validated against the current runtime release. Adopting its generic core as the official product is cheaper and more honest than maintaining two divergent application layers with incompatible wire protocols.

This decision **supersedes the product-form parts** of `runtime-sdk-app-restructure.md`: the "single-user, local, no auth / tenancy" hard constraint; "the wire carries `EventEnvelope` as-is" and "the frontend folds the raw envelope stream" as the product's UI protocol; and "no separate sessionId". It **preserves everything that ADR decided about the libraries**: the three-wheel layout, `noeta.sdk` as the sole public surface, HTTP/SSE only in the app, the app-uses-only-sdk boundary, and the five engine invariants. Task remains the engine's only first-class primitive (`docs/adr/task-as-only-primitive.md`); nothing below the application layer changes shape because of this decision.

## Decision

### Product form

The official application is a **deployable multi-user agent service**: a FastAPI backend plus a React/TypeScript SPA, shipped as one process (a modular monolith) that provisions one sandbox container per session for agent execution. The dist name (`noeta-agent`), the directory (`apps/noeta-agent`), and the entrypoint (`python -m noeta.agent`) are unchanged; what changes is what they contain. The frontend replaces `apps/web` in place.

### Identity and tenancy

- **Auth is a pluggable seam, not a feature of the core.** The backend authenticates every request through an auth-provider interface; the open-source distribution ships a dev-login reference implementation (any username, signed session cookie) and keeps the seam open for real identity providers. No credential, SSO endpoint, or vendor identity system lives in the core.
- **The space is the unit of collaboration and scoping.** Users belong to spaces; a space scopes skills, knowledge sources, agent memory, templates, and agent configuration. Every user gets a personal space; additional spaces have owner-managed membership.
- **Admin is a role, not a deployment.** An allowlisted admin set gets a console (usage stats, users, sessions, spaces, dynamic config, raw-event trace) on the same server.

### Session model

- The application introduces a **session** entity: the unit of conversation the UI lists, resumes, and deletes. A session groups **one or more engine tasks** (a multi-node workflow session owns one root task per node) and owns one workspace directory and one sandbox.
- The session is **app-layer indexing only**, persisted in the application database next to users and spaces. The engine is untouched: every state change still flows through `noeta.sdk` `Client` verbs, the EventLog remains the single source of truth, and the Engine remains the sole writer. "Session" never appears below the application layer.

### Wire protocol: translated UI events over per-session SSE

- The frontend talks to a versioned REST surface plus **one SSE stream per session**. The backend translates canonical `EventEnvelope`s into a **flat UI-event vocabulary** (user message, assistant text, thinking, tool call/result, skill activation, todo update, subtask lifecycle, question, compaction, turn markers) through a **deterministic, stateless, pure function** over the envelope stream.
- **Replay is re-derivation, not a materialized projection.** Reconnects pass a `since_seq` cursor; the backend re-reads the EventLog through the `Client` read surface and re-runs the same translation. There is no stored per-UI projection that can drift; the only durable truth is still the EventLog.
- **The token-streaming principle carries over** from `docs/adr/token-streaming-projection.md`: token deltas ride the stream as ephemeral frames with no SSE id, are never persisted and never replayed, and the durable record stays the appended message event.
- **Raw envelopes remain available, scoped to diagnostics**: the admin trace surface serves the untranslated stream, and the trace UI folds it client-side. The raw-envelope wire is a debugging surface, not the product contract.

### Execution: sandbox-only

Agent shell execution happens **only inside a per-session sandbox container**; the host exposes no shell tools. The session workspace is bind-mounted read-write; space knowledge and activated skills are mounted read-only. A no-sandbox degraded mode exists for offline tests and credential-free evaluation, with shell execution disabled.

### App-managed content

Skills (a database-backed registry: builtin skills managed by admins, space skills managed by space members, both uploadable), knowledge sources (pluggable sync adapters; the open-source core ships git-repository and local-directory sources), space-scoped agent memory, prompt and workflow templates, a feedback loop (per-message ratings feeding an analysis agent whose suggestions are owner-gated), and dynamic runtime configuration are all owned by the application layer and stored in the application database. None of this touches engine identity: it enters agent behavior only through `Options` and host config at session start.

### Boundary discipline is unchanged

The application still crosses into the runtime **only through `noeta.sdk`**. Host-side wiring material (sandbox adapters, storage backends) keeps the same pinned-exemption ratchet regime as before: every legitimate direct import is listed explicitly and the list may only shrink. Whatever the downstream implementation reached into runtime internals for is either promoted onto the `noeta.sdk` surface or pinned in the ratchet — never silently widened.

## Rationale

- **A service is where the runtime earns its keep.** Event-sourced durability, resume, leases, and multi-worker dispatch are invisible in a single-user local tool and load-bearing in a shared service. The official product should exercise the properties the engine exists to provide.
- **Server-side translation does not create a second source of truth.** The old objection to backend projections was drift. A pure, stateless translation function re-run from the log on every replay cannot drift — it is the same fold discipline, executed server-side. What it buys: a **stable, versioned UI contract** decoupled from engine event schemas (engine internals can evolve without breaking every client), and one fold implementation shared by all client types (web, bots, API consumers) instead of one hand-written fold per client.
- **Multi-user changes who may see what.** Raw envelopes carry everything — tool arguments, subtask internals, workspace paths. A multi-user product needs an authorization boundary between the log and the wire; the translator is where scoping and filtering live. Shipping raw envelopes to arbitrary authenticated clients would make every schema detail load-bearing and every future redaction a breaking change.
- **The session entity is an index, not a rival primitive.** Listing, ownership, authorization, and titling need a queryable row keyed to a user and a space — folding every root stream on every list request does not scale past a handful of tasks and cannot express ownership at all. Keeping the session strictly app-layer preserves task-as-only-primitive below the boundary.
- **Sandbox-only is the only sane execution model for a shared server.** Host execution with per-call approval is a single-user affordance; on a multi-user server it is a privilege-escalation surface. Making the container the only file and shell surface removes the entire class.
- **A pluggable auth seam keeps the core vendor-free.** Identity is the part of a platform most coupled to its deployment environment. A seam with a dev-login reference implementation lets the open-source core stay credential-free while real deployments plug in their own provider.
- **Retiring beats forking.** Two official apps means two wire protocols, two frontends, and two test suites drifting apart under one maintainer. The retired app's genuinely differentiated capabilities (MCP connector management, image input, the app preview gateway) port onto the platform; its product form does not.

## Alternatives considered

1. **Keep both applications** (local single-user + multi-user service). Rejected: double maintenance of incompatible protocols and frontends for one maintainer. The local developer story is already served by `noeta-sdk` itself plus the platform's credential-free mock mode; a second product shell adds form, not capability.
2. **Evolve the existing app into the multi-user service.** Rejected: its three hard constraints — no auth, raw-envelope wire, client-side fold — are each load-bearing in its design. Retrofitting tenancy, sessions, and a translated contract rewrites all three plus the frontend; that is a replacement wearing an evolution costume, paid for twice.
3. **Keep the raw-envelope wire and add auth around it.** Rejected: it couples every client to engine event schemas, forces each non-web client to reimplement the fold, and still needs server-side folding for session lists, titles, and admin views — the projection cost is paid anyway, without gaining a stable contract or a redaction point.
4. **Build the platform in a separate product repository.** Rejected: after retiring the old app the noeta repo would ship no official product, the app-uses-only-sdk ratchet cannot be enforced across repositories, and the platform's offline test suite is the best end-to-end exercise of the runtime — splitting repos splits that verification loop.
5. **Start as microservices** (separate auth / knowledge / execution services). Rejected for now: a single deployable unit with modular internals is the right cost profile until real scaling pressure exists. The seams (auth provider, sync adapters, sandbox provider, storage) are already interfaces; extraction later is mechanical.

## Consequences

- The previous application (`apps/noeta-agent`'s backend and `apps/web`'s frontend) is deleted and replaced. The web-surface decisions that described it (`docs/adr/web-task-creation.md`, `docs/adr/web-file-panel-and-app-preview.md`, `docs/adr/web-image-attach.md`) become historical for the product UI; the protocol section of `docs/adr/runtime-sdk-app-restructure.md` is superseded as described above, while its library sections stay in force.
- The retired app's MCP connector management (re-scoped from a global registry to per-space configuration), composer image input, and app preview gateway are ported onto the platform rather than lost.
- The platform's first release is honest about its limits: single-process single-instance (horizontal scaling is future work), dev-login as the default auth, no rate limiting or quotas. These are documented product boundaries, not accidents.
- `CONTEXT.md`'s product vocabulary (session, space, UI event, skill registry, knowledge source) and the README's product narrative are rewritten to match; import-linter contracts are re-scoped to the new module layout with a re-pinned exemption list.
- Migration sequencing, the per-file port/adapt/drop inventory, and open-source hygiene rules live in the implementation spec, not here.
