# Agent identity and provenance: agent→engine resolver + AgentSpec/registry + AgentBound

## Context

A single long-lived worker can host many concurrent sessions at once. When it leases any given Task, it must drive that Task with **that Task's own** Agent (policy + tools + context + budget). This requires a resolution chain from Task to Engine: `agent_name` as the authoritative selector → a generic AgentSpec → durable provenance → backward compatibility with old data. The naming layer was later evolved by `library-sdk-architecture.md`, but the mechanism described here is unchanged.

The original design also included a deterministic digest based on `AgentSpec.fingerprint`: it was recorded on `AgentBound` and drove a three-state agent-drift check under verify. The verify/replay test machinery has since been removed, so **the fingerprint digest, the recorded `agent_fingerprint`, and the drift check no longer exist**. What still carries weight is described below: `agent_name` as the authoritative selector, `AgentSpec` as a frozen, serializable *identity* object (compared by structural equality), the pure wiring-factory contract, and the durable `AgentBound` record (which now carries only `agent_name`). An old recording that still contains an `agent_fingerprint` deserializes cleanly (the key is dropped by the tolerant restorer).

## Decision

### `agent_name` is the authoritative selector; a per-task agent→engine resolver

- **The L2 `resolve_engine(task) → Engine` seam**: fold the Task, read `TaskCreated.agent_name`, look the Agent up through the registry, and build the engine + policy via the `build_engine_for_agent` factory. **`Engine` stays single-policy**—the resolver picks the Engine at the host layer; a single Engine never swaps policy. The single-writer invariant and the Engine line budget are both untouched.

- **`agent_name` is promoted from "observable" to "authoritative"**: the resolver dispatches on it, so a new task must write a **resolvable** `agent_name`. An unknown name is a hard error at lease time (not a silent no-op). `policy_name` remains observable provenance only.

- **One driving primitive across every surface**: CLI and web both converge on `run_leased_task`; the woken branch carries its per-command differences through a typed **woken-command-prelude seam** (append-message / resolve-approval / none), rather than each surface growing its own resume machinery.

### Generic AgentSpec / AgentRegistry

- **`AgentSpec` is a frozen, fully serializable *identity* object; the wiring factory is separate.** The spec carries only declarative, canonically serializable identity (name / instructions / policy / composer / tools / skills / guards / observers / default_budget / capabilities / metadata) and has **no `Callable` fields**. Turning a ref into live components is done by **another** registry/builder that resolves the same `(name, version)` ref.

- **Identity is structural equality.** Two specs with equal declarative identity are `==`; component lists are normalized to sorted tuples at construction, so the order the author wrote them in never changes identity. (`metadata` / `default_model` are decorative / routing hints—not behavior-affecting identity.)

- **Factory purity is an explicit contract**: a wiring factory must be a pure function of `(name, version) ref + host config`; any behavior-affecting change **must** show up as a version bump and must never hide inside a closure. A `ToolRef` / `ComponentRef` therefore carries a **non-default** `version`—two behaviorally different components must never share one.

- **`AgentRegistry`**: `add` (duplicate name → error) / `resolve` (unknown name → `UnknownAgentError`) / `names`.

- **The low-level `noeta.agent`** carries an import-linter forbidden contract restricting it to importing only `noeta.protocols`, welding the package boundary shut so that server/worker cannot depend backward on the L3 code layer.

### `AgentBound` durable provenance

- **An incremental `AgentBound {agent_name}` event; never add a field to `TaskCreated`**: adding a field to genesis would drift the canonical bytes of **every** historical recording, whereas a new event type is simply **absent** from old recordings and folds with zero drift (the same byte-safety rule as `ModelBound` / `ConversationClosed`).

- **The Engine writes `AgentBound` atomically inside `create_task`, immediately after `TaskCreated`** (one trusted write point, not an easily forgotten second call), for every **named** task → the class of gap "named Task but no provenance" becomes structurally impossible. The Agent is immutable within a Task, so this happens **exactly once**, not re-emitted every turn. An `unnamed` task emits no `AgentBound`.

- `AgentBound` is the durable record "Agent X was bound to this Task"; resume rebinds the task to its Agent via `agent_name`.

### Legacy `unnamed` compatibility

- **`"unnamed"` is a frozen legacy sentinel** that new tasks **must not** use; it exists only as the default value of `create_task` for low-level tests and old demos.

- **Resolving `"unnamed"` is an opt-in fallback, never silent**: the registry treats it as a hard error like any other unknown name; support for old data is an explicit host choice (`unnamed_fallback`; passing `None` keeps the hard error).

- **Unnamed recordings emit no `AgentBound`** → they fold cleanly with zero drift.

## Rationale

- **`agent_name` was promoted to authoritative because the recording already self-described the Agent's identity—nobody was reading it.** Genesis has always recorded `agent_name`; having the resolver actually dispatch on it lets CLI and web converge on a single `task → Engine` function and reconstruct the wiring self-describingly from the recording.

- **The Engine stays single-policy to keep host concerns out of the execution core.** Having the Engine hold a resolver, or having `run_one_step` accept a policy, would bloat that ≤500-line core and blur the "one Engine = one Policy" shape. Picking the Engine at the host layer is purely additive.

- **`AgentSpec` carries no closures so that identity stays declarative.** Closures can be neither compared nor serialized—putting a `policy_factory` on the spec would make identity depend on an opaque object. Separating identity from wiring gives a closure-free, structurally comparable identity object; the pure-factory + versioned-ref contract is exactly what makes behavioral changes visible as a version bump.

- **`unnamed` compatibility exists so that migration doesn't invalidate all of history overnight.**

## Alternatives considered

1. **Make `Engine` per-task-policy-aware (Engine holds a resolver, or `run_one_step` accepts a policy).** Rejected: bloats the ≤500-line core and pushes host concerns into the execution core.

2. **Have `AgentSpec` carry a `policy_factory` / `composer_factory` closure.** Rejected: identity would depend on a non-serializable, non-comparable closure.

3. **Generalize `CodingAgent` in place.** Rejected: the only agent abstraction would stay in L3, server/worker would have to depend backward on `noeta.code`, and non-code agents could not be hosted.

4. **Add the bound identity as a field on `TaskCreatedPayload`** / **re-emit `AgentBound` every turn.** Rejected: adding a field to genesis drifts all of history (which is the entire reason `ModelBound` exists); the Agent is immutable within a Task, so re-emitting every turn is redundant bytes that also falsely implies "the Agent can change mid-stream."

5. **Auto-register a built-in `"unnamed"` → default Agent.** Rejected: it would silently revive a retired field (a typo would fall through to unnamed behavior).

## Consequences

- Identity and registry land in: `AgentSpec` in `noeta.agent.spec`, `AgentRegistry` + `UnknownAgentError` in `noeta.agent.registry`.

- Resolution and driving land in: the agent→engine resolver + `unnamed_fallback` in `noeta.execution.resolver`, the `run_leased_task` convergence + prelude seam in `noeta.execution.driver` / `noeta.execution.runner`.

- The provenance write point: the `AgentBound` for a named task is written atomically inside `create_task` in `noeta.core.engine`; the event payload lives in the protocols layer.

- Byte safety is the lifeline of this design: new provenance always goes through "add a new event type" rather than "add a field to genesis," to guarantee zero-drift folding of old recordings.

- After the removal of verify/replay, the fingerprint digest, `agent_fingerprint`, and drift check no longer exist; a leftover `agent_fingerprint` key in an old recording is dropped by the tolerant restorer and still deserializes cleanly. Do not rely on fingerprints for identity comparison anymore—identity now rests solely on structural equality of `AgentSpec`.

- Factory purity is a constraint to guard long-term: any behavior-affecting wiring change must show up as a version bump and must never hide inside a closure, or the `(name, version)` ref can no longer uniquely identify a behavior.
