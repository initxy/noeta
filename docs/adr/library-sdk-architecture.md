# Noeta is a library: the engine wheel runs agents, the SDK wheel is a thin door, and `Options` compiles into a frozen `AgentSpec`

## Context

Noeta is embedded by a host program that owns its own process, transport, and deployment shape. Two forces pull against each other: the engine must be able to run an agent by itself, or "library" is a misnomer and every consumer depends on a product; and the surface a user codes against must stay small and stable, so internals can be reorganized without breaking anyone.

## Decision

### Two distributions, one public door

`noeta-runtime` is the engine: it carries and records a Task, folds the EventLog, schedules Workers, and owns the agent-authoring material (`context`, `policies`, `tools`, `execution`, and the `agent.spec` / `agent.registry` identity layer). `noeta-sdk` carries the client, the built-in plugin catalogue, the official presets, and the `noeta.sdk` facade. A user installs `noeta-sdk` and imports `noeta.sdk`; `noeta-runtime` is a transitive dependency whose modules are not part of the API surface. `noeta.sdk` is re-export only — implementations live in `noeta.client` and in the runtime bands beneath it.

The engine runs in the caller's process. Nothing sits between the caller and the engine, and neither library carries a transport: HTTP, SSE, sockets, and any wire protocol belong to the host. `examples/reference-host` is the reference embedding, assembled from the public surface alone.

### `Options` compiles into a frozen `AgentSpec`

`Options` is a human-friendly recipe; a pure function compiles it into a frozen `AgentSpec` plus the flat list of descendant specs a host registers. Compilation is additive — it fills in SDK defaults rather than overriding user intent — and sits at the front of the loading chain as sugar, not as a substitute for the identity type.

- `agents` is a flat `name → AgentDefinition` dict. Deep trees are expressed by declaring every agent at the top level; there is no recursive nesting. Each definition's `description` is what the delegation control tool renders, so the model can tell the children apart.
- `allowed_tools` is replace-style: setting it means only those; leaving it unset yields the full built-in set of eleven tools. `disallowed_tools` always subtracts. Tools authored with `@tool` mix into the same list as objects carrying their own ref.
- Identity-bearing fields are `system_prompt`, `name`, the resolved tool list, `policy`, `skills`, `plugins`, and `budget`. Wiring fields — `provider`, `model`, `cwd`, `metadata`, `can_use_tool`, `guards`, `observers`, `content_channels`, `output_schema`, `thinking`, `effort` — are ignored by compilation and excluded from `Options` equality, so two recipes differing only in wiring are the same agent.
- Host-level wiring lives on `HostConfig`, not `Options`: the storage triple or a single `storage_path`, the app preview gateway, MCP resolution, the filesystem write mode, and out-of-workspace write roots. Compilation never sees it.

Identity, binding, and wiring are three different things because the same agent must run on different models and different vendors. Identity is the structural equality of the frozen `AgentSpec`. The model a task runs on is a binding recorded per task as `ModelBound`; an agent may carry a preferred model as a routing hint outside its identity. The provider adapter is wiring the host chooses. `TaskHostBound`, written once at task creation between `AgentBound` and any `ModelBound`, records host provenance in the task's own stream: which host bound the task, and the absolute workspace directory welded as that session's filesystem root.

### Official agents ship with the SDK

The roster — `main`, `explore`, `plan`, `general-purpose` — is factory content in `noeta.presets`, with prompt text in `presets/prompts/*.md`. An import contract keeps SDK core from importing `noeta.presets`, so neutrality is a boundary rather than a convention. Every agent, subagents included, is an `AgentSpec` registered under a name: spawning resolves by name, an inline unregistered subagent cannot be dispatched, and spawning is in-process.

### One construction point, and a narrow extension band

`noeta.execution.builder` is the only path that assembles an agent's components and builds the Engine. It resolves the registered components by ref and fails loudly when one is missing or structurally mismatched; component source is never stored, so a rebuild must happen where those components are registered.

The open extension points are `Options` fields — tools, `policy`, `guards`, `observers`, `content_channels`, `provider` — plus manifest plugins. Two seams are closed to the user. The `ContextComposer` is injected into the Engine through a Protocol and wired by the builder, with a protocols-only pass-through as the kernel's own fallback, but no user-facing replacement point. The Engine main loop, `Dispatcher`, `Worker`, and lease handling expose concurrency and lease duration as configuration and no code-replacement seam.

## Rationale

- **The engine wheel must be self-sufficient, or the SDK is not a library.** Keeping the execution machinery inside the engine is what lets the SDK be a door rather than a fat material library, and lets a consumer run an agent in-process without pulling in a product.
- **A public-surface boundary encapsulates better than a material boundary.** The facade *is* the API: internal modules can be reorganized freely as long as `noeta.sdk` keeps its export set, whereas a boundary drawn by "which kind of code lives where" leaks the moment a class needs a different home.
- **Compiling `Options` into `AgentSpec` rather than replacing it preserves identity, provenance, and resume.** With a mutable config bag a task's agent could not be pinned, compared, or rebuilt from the task's own log.
- **Keeping provider and model out of identity is what makes "the same agent" hold up.** If swapping a model produced a different agent, running one agent across two vendors would be inexpressible at the identity layer. Rebuilding state folds the recorded response and never re-calls a provider, so the swap is safe.
- **A single construction point eliminates a class of drift.** Two hand-synchronized assembly paths — one for a fresh run, one for a rebuild — diverge silently; one path cannot.
- **The composer is closed because stable-prefix prompt caching is a hard constraint.** A user-supplied composer can reorder the prefix and destroy cache hits on every turn with no visible failure.
- **Per-task host provenance answers attribution from the task alone.** A host-dimension stream would need a durable task→host link and a cross-stream lookup to answer the same question.
- **Transport is a deployment choice.** A library that bundles a server imposes its shape on every embedder; leaving the wire to the host keeps one library usable from a service, a script, or a test.

## Alternatives considered

1. **Replace `AgentSpec` with a mutable `Options` config bag.** Rejected: it trades away identity, provenance, and resume — the properties the event log exists to provide.
2. **An async, pure message-stream API.** Rejected: impedance-mismatched with a synchronous, event-sourced, single-writer engine, and it demotes the event envelope stream from the canonical record to an internal detail.
3. **One package, no wheel split.** Rejected: without a distribution boundary nothing stops host or user code from reaching into engine internals, and the public surface erodes into "import whatever you want".
4. **A mandatory process boundary, with the engine behind a socket.** Rejected: an in-process embedder would pay serialization, lifecycle, and IPC costs for a boundary it does not need.
5. **A transport seam or a server inside the library.** Rejected: it re-fattens the SDK and makes the library dictate a deployment shape. A remote, cross-process form stays open as a future extension.
6. **Keep the roster outside the SDK as host-layer data, and let subagents nest recursively.** Rejected: an SDK-only user would get no official agents, which guts "runs out of the box"; and a flat `agents` dict buys a `description` per child plus a far simpler compile.
7. **A default three-segment composer inside the kernel.** Rejected: it makes the kernel depend upward on the context band, which no contract could then cut; an injected composer keeps the kernel to protocols.
8. **A separate CLI launcher layer.** Rejected: run, inspect, and resume are SDK verbs, so a launcher would only wrap argument parsing around capabilities the library exposes.
9. **A host-dimension provenance stream, or no host provenance at all.** Rejected: the first is heavier and cannot answer attribution from a task's own log; the second leaves one spec behaving differently under different hosts with no recorded signal.

## Consequences

- A subagent must be a registered `AgentSpec`; spawning is in-process; a task rebuilds only where its components are registered, and errors loudly otherwise.
- The load-bearing modules are `noeta.client.options` (compilation), `noeta.execution` (the single construction point), `noeta.presets` (the roster), and `noeta.core.engine` (which takes an injected composer and is the sole writer to the event log).
- The in-repo half of the boundary is mechanical — the import contracts in `package-layout.md`. The "user code imports only `noeta.sdk`" half rides on wheel packaging, because an import contract cannot reach across a distribution boundary.
