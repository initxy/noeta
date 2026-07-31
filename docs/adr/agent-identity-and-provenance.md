# `agent_name` selects the Agent, `AgentSpec` is its closure-free identity, `AgentBound` is its durable provenance

## Context

One long-lived worker hosts many concurrent Tasks. When it leases a Task it must
drive that Task with **that Task's own** Agent — its policy, tools, context and
budget. That requires a resolution chain from a leased Task back to an Engine,
and a durable record of which Agent a Task is bound to, so a resume on another
process rebuilds the same wiring from the recording alone.

## Decision

**`agent_name` is the authoritative selector.** Genesis records it in
`TaskCreated`; the per-task agent→engine seam folds the Task, reads that name,
looks the Agent up in the registry, and builds the Engine. An unresolvable name
is a hard error at lease time, never a silent fallback to a default Agent.
`policy_name` alongside it is observable provenance only.

**The Engine is single-policy.** The resolver picks the Engine at the host
layer; one Engine never swaps policy mid-flight. A worker runtime driving a
single Agent has no resolver and uses its one Engine, so the seam is purely
additive. Every surface converges on one driving primitive (`run_leased_task`);
per-command differences on the woken branch ride a typed woken-command-prelude
(append a message, resolve an approval, or nothing) rather than each surface
growing its own resume machinery.

**`AgentSpec` is a frozen, fully serializable identity object with no
`Callable` fields.** It carries name, instructions, policy and composer refs,
tools, skills, guards, observers, default budget, the `plugins` activation
tuple, the `spawnable` set, metadata and a preferred model. Turning those refs
into live components is a separate builder keyed by the same `(name, version)`.

**Identity is structural equality.** Component lists normalize to sorted tuples
at construction, so author ordering never changes identity. The `plugins`
activation tuple is behaviour-shaping identity — feature gating is a membership
test against it, in one shared derivation. `metadata` and `default_model` are
routing/display hints and are not identity. Deserializing a spec demands an
explicit `plugins` key: a plugin-free agent must say so, never default silently.

**Factory purity is an explicit contract.** A wiring factory must be a pure
function of a `(name, version)` ref plus host config. Any behaviour-affecting
change bumps the ref's version and must never hide inside a closure, so
`ComponentRef.version` and `ToolRef.version` carry behaviour, not a release tag.

**`AgentBound` is a separate event, written atomically inside `create_task`.**
The Engine appends `TaskCreated` then `AgentBound` in one call — one trusted
write point, so a named Task cannot exist without its identity record. The Agent
is immutable within a Task, so this happens exactly once and is never re-emitted.
A host/session binding, when supplied, follows as `TaskHostBound`.

**`unnamed` is a reserved sentinel with no identity.** It is the default of the
low-level `create_task` for kernel-level tests, emits no `AgentBound`, and the
registry rejects it like any other unknown name. A host that wants it to resolve
opts in explicitly by supplying a fallback spec.

## Rationale

Because the recording self-describes the Agent, dispatching on `agent_name` lets
every surface converge on one `task → Engine` function and reconstruct the
wiring from the recording instead of from ambient host state.

A single-policy Engine keeps host concerns out of the execution core: the core
stays within a tight line budget, and "one Engine = one Policy" is the shape the
single-writer invariant and the Engine's budget accounting rely on.

Closures can be neither compared nor serialized. A `policy_factory` on the spec
would make identity depend on an opaque object, and two behaviourally different
wirings could then share one identity. The versioned-ref contract is what makes
a behaviour change visible.

Provenance travels as its own event type, never a field on genesis. A field on
`TaskCreated` would drift the canonical bytes of every recording; a separate
event type is simply absent from a recording that does not carry it and folds
with zero drift.

## Alternatives considered

1. **Make the Engine per-task-policy-aware** — hold a resolver inside it, or let
   the step entry point accept a policy. Weighed and rejected: it bloats the
   execution core and pushes host concerns into it.
2. **Put a `policy_factory` / `composer_factory` closure on `AgentSpec`.**
   Rejected: identity would depend on a non-serializable, non-comparable object.
3. **Keep the only agent abstraction inside a product-specific coding layer and
   generalize it in place.** Rejected: the worker would depend backward on
   product code, and non-coding agents could not be hosted at all. The identity
   layer is instead welded shut by an import-linter contract limiting
   `noeta.agent.spec` / `noeta.agent.registry` to `noeta.protocols`.
4. **Carry the bound identity as a field on the genesis payload, or re-emit
   `AgentBound` every turn.** Rejected: a genesis field drifts all history, and
   re-emitting is redundant bytes that falsely implies the Agent can change
   mid-stream.
5. **Auto-register a built-in `unnamed` → default Agent.** Rejected: a typo in
   an agent name would silently fall through to default behaviour instead of
   failing at lease time.

## Consequences

- Identity and lookup live in `noeta.agent.spec` and `noeta.agent.registry`;
  the generic per-task resolver skeleton in `noeta.execution.resolver`; the
  resolver seam and `run_leased_task` in `noeta.runtime.worker`; the shared
  conversation-command driver and its typed preludes in `noeta.execution.driver`.
- The provenance write point is `create_task` in `noeta.core.engine`; the
  payload types are in the protocols layer.
- Provenance always arrives as its own event type, so folding a recording that
  lacks it stays byte-clean.
- Factory purity is a standing constraint: a behaviour change that does not bump
  a ref's version breaks the guarantee that `(name, version)` uniquely names a
  behaviour, and nothing detects it automatically.
