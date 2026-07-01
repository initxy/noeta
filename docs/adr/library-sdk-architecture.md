# noeta is a library-style SDK, and the product is a thin shell on top: Options compile into a frozen AgentSpec, the official roster ships with the SDK

## Context

noeta aims to match the official **Claude Agent SDK** (library-style: `query` / `Client` / `Options` / `@tool`) and Claude Code (a product built on top of the library). Early noeta took the form of a "product monolith": the execution machinery was stuck in the product layer, HTTP was nailed to a specific layer, and the library couldn't run an agent on its own. The direction this decision establishes is—**noeta is a library, and the product is a thin shell built on top of it**.

This document originally also proposed a physical three-layer split by **mechanism vs material** plus a `NoetaServer` host boundary. That physical layering was later corrected by `docs/adr/runtime-sdk-app-restructure.md` (see "Consequences"). What this document retains is the library direction itself, plus the Options / identity / presets / builder / `TaskHostBound` decisions that survived the subsequent restructure.

## Decision

### noeta is a library, the product is a thin shell

Distribution converges into three wheels: `noeta-runtime` (the pure engine + all agent-authoring material), `noeta-sdk` (a thin in-process client, public surface `noeta.sdk`, no engine, no HTTP), and `noeta-agent` (the official product, built on the sdk + the `apps/web` frontend + HTTP/CLI entrypoints; it **defines no agents**). The model is in-process (like LangChain), not forcing a subprocess or HTTP between the caller and the engine.

### Options are sugar that compiles into a frozen AgentSpec (additive, not a replacement)

Expose a Claude-Code-feel `Options`, with an internal **pure function that compiles it into a frozen `AgentSpec`**, burned into `AgentBound` / `TaskHostBound` as usual. The identity kernel (`docs/adr/agent-identity-and-provenance.md`) doesn't move an inch—`Options` is just a humanized input layer added at the very front of the loading chain. The parameter table aligns with Claude's shape:

- `agents` = a **flat dict**: name → `AgentDefinition` (description required, prompt, tools, model); `description` is auto-rendered into the dispatch control-tool's description (fixing the "the model doesn't know who to dispatch to" defect). No recursively nested subagents.
- `allowed_tools` / `disallowed_tools` replace the positively-named `tools`: unset = the official full default set, set = an adjustment; custom tool objects (produced by `@tool`) are mixed directly into the list. The precise semantics of tool resolution (`allowed_tools` is now replace-style, and the default set is 11 tools) follow `docs/adr/runtime-sdk-app-restructure.md`, which revised this document's original additive wording.
- `permission_mode` (default / acceptEdits / plan / bypassPermissions), `can_use_tool` (a programmatic approval callback whose verdict is recorded as the existing approval events), `max_turns`, `cwd` (not part of identity), `system_prompt` (a single string, or "the official main preset + an append").
- noeta-specific reservations: `provider` (provider neutrality is bedrock), `skills`; `name` / `budget` / `capabilities` are demoted to advanced fields.

### provider / model are binding + wiring, not part of identity

The same agent (same prompt / tools / behavior) must be able to run on different models and different vendors. So: **identity** = prompt + tools + policy + composer + capabilities + budget; **binding** (recorded per task as `ModelBound`) = which model this run uses; **wiring** (chosen at startup) = which vendor's provider adapter to attach. Each agent may carry a **not-part-of-identity "default model"** (subagent defaults to haiku, main defaults to opus), pinned into the subtask's `ModelBound` at spawn. Swapping providers doesn't threaten state rebuild—rebuilding a task's state from the EventLog relies on the recorded `LLMResponseRecorded` and never re-calls the provider.

### Official agents ship with the SDK (noeta.presets), and the roster aligns with Claude Code

The four-piece set **main + explore + plan + general-purpose** ships as SDK factory content, kept separately in `noeta.presets`; import-linter enforces "no other SDK module may import `noeta.presets`" (neutrality is guarded by an in-package boundary). Every agent (including subagents) **must be an `AgentSpec` registered in the registry with its own identity**—spawning a subtask resolves by name in the registry, and a purely inline, unregistered subagent cannot be dispatched. **Spawning is in-process only**; remote / cross-service agent references are left for future extension.

### A single canonical construction point + TaskHostBound host provenance

There is only **one canonical code path** (the builder) for assembling an agent's components and building the Engine and its components, no longer a pair of hand-synchronized wiring modules. A task that used custom components must be rebuilt in an environment where those components are registered (a loud error if any is missing); **component source is not stored**—the builder resolves from the currently-registered components, and a mismatch (structural mismatch) errors out, to keep a drifted component from silently substituting for the recorded one.

`TaskHostBound` is written by the product's creation path when the task opens, ordered after `AgentBound` and before `ModelBound`, recording which host this task is bound to.

## Rationale

- **The three wheels are split this way to make noeta a library that can genuinely run agents on its own, rather than "a product you must import as a monolith."** When the execution machinery is stuck in the product layer, any consumer who wants to "run an agent in-process" has to depend on the whole product; an SDK that can't run an agent makes "aligning with Claude Code's library/product layering" a misnomer.
- **`Options` compiling into `AgentSpec` rather than replacing it is noeta's only hard difference from the Claude Agent SDK.** Replacing `AgentSpec` with a mutable config bag would be the most Claude-like, but it loses identity / provenance / resume—exactly noeta's moat. The additive approach keeps the moat while getting the Claude feel.
- **provider / model not being part of identity is what makes the concept of "the same agent" hold up.** If swapping models counted as "a new agent," then "the same agent running on a different vendor" couldn't be expressed at the identity layer at all. Splitting off binding / wiring keeps identity stable and the environment swappable.
- **Merging the execution builder into a single construction point is so that every task is assembled by the same canonical code path.** Two hand-synchronized construction points are a long-term hazard—"a fresh run" and "rebuilding a task's state" each building their own set of components would drift; a single path eliminates a whole class of drift.
- **`TaskHostBound` recorded per task rather than per host is so that a task's own EventLog can answer "which host is this task bound to."** A host-dimension stream would need a separate host stream + a durable task→host link + read-model exclusion + a cross-stream lookup, far heavier than a host-API slice; without a per-task link, provenance attribution can't be answered from the task alone.

## Alternatives considered

1. **Replace `AgentSpec` with a mutable `Options` config bag (replacement-style).** Rejected: loses identity / provenance / resume—noeta's only hard difference.
2. **Copy Claude's async + pure message stream.** Rejected: impedance-mismatched with the synchronous, event-sourced, single-writer engine, and it demotes the envelope stream (a selling point) to an internal detail.
3. **Keep the roster in the product layer as pure data / keep the recursive `subagents` nesting sugar.** Rejected: a pure SDK user wouldn't get the official agents, discounting "runs out of the box"; Claude itself doesn't nest, and a flat `agents` dict trades `description` for a bigger structural gain.
4. **Keep a default `ThreeSegmentComposer` in the kernel for convenience.** Rejected: keeping that upward dependency means `context` can never leave the runtime band and the boundary can't be cut clean; an injected composer keeps the kernel purer (`noeta.core` retains a protocols-only pass-through fallback).
5. **Keep a separate CLI launcher layer.** Rejected: the product form is "an agent with a frontend"; the operational commands (run / inspect / resume) are just argparse wrappers over runtime capabilities—drop the wrapper, keep the capability (which really lives in the `noeta.policies` / `noeta.tools` / `noeta.providers` / `noeta.context` libraries), and shrink the entrypoint to a very thin `python -m noeta.agent`.
6. **A host-dimension `ServerHostStarted` stream as the primary provenance / not persisting host config at all.** Rejected: heavier than per-task and unable to answer attribution (see "Rationale"); not persisting leaves the gap "the same spec behaves differently under different host configs with no provenance signal."

## Consequences

- **The physical layering has been corrected by `docs/adr/runtime-sdk-app-restructure.md`.** This document originally split layers by mechanism vs material (the sdk held the concrete implementations of policies / tools / providers / context, the runtime held only protocol contracts, and the execution machinery was "lifted up" into the sdk with the server placed in the sdk). The current form is the reverse: **material sinks into `noeta-runtime`** (policies / tools / providers / context / execution / agent.spec / registry / presets are all in the runtime wheel, import paths unchanged, only the wheel ownership changed); **`noeta-sdk` is a thin client** (public surface `noeta.sdk`, no engine, no HTTP); **HTTP/SSE belongs to `noeta-agent`** (the old `noeta.server` monolith is deleted, and serving is now borne by the in-app backend `noeta.agent.backend`). The execution machinery (runner / driver / resolver / multi_turn / subtask_drain / builder) **sinks into runtime** (`noeta.execution.*`), not lifted up into the sdk. The boundary is now drawn by "the outer wheel + the public surface," and the original worry that "the sdk would become an empty shell" is answered by the public-surface boundary (the `noeta.sdk` facade + import-linter).
- **Two host-side provenance contracts were removed along with verify/replay.** This document originally designed a host-config / registry fingerprint for `TaskHostBound` (D4) and paired it with a tool-version guard (schema-hash ↔ author-declared version, D5); both fed the verify/replay test machinery, and after verify/replay was removed they were deleted too. `TaskHostBound` is **kept**, but now carries only `host_id` (plus the per-session `workspace_dir`, see `docs/adr/workspace-and-session-path.md`); an old recording still carrying the retired fingerprints deserializes cleanly (the tolerant restorer drops those keys). Identity is now the structural equality of a frozen `AgentSpec`, and the original deterministic fingerprint digest is retired (see `docs/adr/agent-identity-and-provenance.md`).
- Still-in-force load-bearing landings: `noeta.client.options` (the `Options`→`AgentSpec` compilation), `noeta.execution.*` (the runner / driver / resolver / multi_turn / subtask_drain / builder single construction point, resolving registered components and erroring on missing or drifted ones), `noeta.presets` (the official four-piece set), `noeta.core.engine` (`ContextComposer` must be injected, the kernel doesn't hard-code a concrete composer), and `noeta.agent.spec` (each agent's not-part-of-identity default-model field).
- Constraints: a subagent must be an `AgentSpec` registered in the registry, and a purely inline one cannot be dispatched; spawning is in-process only; component source is not stored, and a rebuild must be done in an environment where the components are registered, or it errors.
