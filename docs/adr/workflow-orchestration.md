# A model-written orchestration script is one Task plus one Policy that interprets it; every helper is a real Subtask

## Context

An agent needs to write a small orchestration script on the spot, dispatch a few
helpers, look at intermediate results and change direction — without adding a
runtime primitive. It builds on the "a workflow compiles into Task + Policy"
route (see task-as-only-primitive.md), the grouped fan-out join (see
subtask-fanout-and-durable-wake.md), and AgentSpec identity (see
agent-identity-and-provenance.md).

## Decision

- **One workflow run is one Task; the script is a Policy that interprets it.** A
  workflow run is exactly "a recorded, suspendable, resumable agent execution",
  which is what a Task is, so it is reused directly rather than wrapped in a new
  container. There is no `WorkflowRunner` and no `WorkflowPolicy` class —
  `scripts/lint-naming.py` rejects those names, holding the line that Workflow is
  not a first-class concept.

- **Every helper is a real Subtask.** Each `agent()` call dispatches a Subtask
  with its own EventLog stream, so inspect, fold and resume apply to it
  automatically.

- **Stop-and-go re-runs the script from the top and uses the EventLog as its
  journal.** Every Policy step re-executes the script from line one; each
  `agent()` call site is keyed by execution-order cursor (`wf-<i>`), so a call
  whose result is already recorded returns instantly from the recording, and the
  first one without a result emits a spawn decision and suspends on the join. No
  coroutine is frozen and no second journal exists.

- **The script sandbox guarantees determinism, not safety.** The script is parsed
  and executed into a controlled namespace holding only the orchestration API
  (`agent` / `parallel` / `log` / `args`) plus a builtin allowlist with no
  `import`, `open`, `eval`, `exec` or `__import__`. A static AST check runs at
  translation time and rejects imports, references to non-deterministic or IO
  modules, dunder/reflection access and IO builtins, pointing the error at the
  offending line; a violation yields a recoverable receipt and spawns no subtask
  at all. The model writing the script already holds the shell and file tools, so
  executing the Python it wrote adds no attack surface — what is needed is
  reproducibility, not isolation.

- **The entry point is a standalone `run_workflow` control tool that spawns a
  child task running the orchestration Policy.** It is the same family as
  `spawn_subagent` and shares that pipeline; the difference is only that the
  child runs the interpreter Policy rather than a catalogued agent. The tool must
  be the sole tool call in its turn, and it only submits the job — the job itself
  is that Task.

- **A helper failure is loud.** A subtask that terminates in failure raises out of
  `agent()` / `parallel()` rather than returning an empty result, so an
  untolerated failure fails the whole workflow with a deterministic reason; a
  script that wants to survive a dead helper says so with `try`/`except`.

- **Per-helper structured output injects a `structured_output` tool into that
  helper and steers it there.** `agent(goal, schema=...)` puts the schema on the
  child's inputs; a wrapper Policy intercepts the call before it reaches the
  ToolRuntime, validates its arguments against the schema, and turns a clean
  payload into that subtask's answer. A payload that misses the schema is acked
  as a failed tool result naming the violations, and both miss modes — never
  calling the tool, and calling it off-schema — share one bounded nudge budget
  before the subtask fails. The session-level `output_schema` is reserved for the
  shape of the top-level final answer (see unified-context-supply.md).

- **The orchestration primitives are `agent()` and `parallel()`.** Both stop and
  wait at the call site, so the workflow task never keeps running while subtasks
  are in flight and no new wake mechanism is needed. `parallel()` builds an
  all-of group on the barrier from subtask-fanout-and-durable-wake.md; its
  members run with wall-clock concurrency by default, and the authoritative
  account of that is subtask-parallel-execution.md.

## Rationale

- **Re-run plus the EventLog beats a frozen coroutine.** Python coroutine frames
  are hard to persist reliably, whereas the EventLog is already the source of
  truth; a second side-channel journal is duplication and two-source drift. This
  works precisely because the script is deterministic.

- **Determinism is the constraint the sandbox exists for.** Stop-and-go re-runs
  from the top and must derive the same decision every time. Isolation aimed at
  safety would be a cost mismatch against a model that already holds the shell.

- **`run_workflow` is its own tool.** `spawn_subagent`'s schema is deliberately
  stable so recordings keep folding and resuming cleanly; stuffing a `script`
  parameter into it would change that schema and jam two contracts into one tool
  description, which is the single source of truth for model-visible semantics.

- **The Engine stays workflow-agnostic.** Script interpretation lives entirely in
  the Policy, so the Engine carries no knowledge of orchestration.

## Alternatives considered

1. **An ad-hoc in-process orchestration layer above the Engine, with disposable
   helpers on a thread pool and a side-channel journal.** Rejected: it bypasses
   Task and the EventLog, so helpers escape the durable record and lose fold and
   resume, and it reintroduces the vocabulary the naming rule forbids. The
   durable substrate is the whole point of running orchestration here at all.
2. **Freeze a half-run coroutine and its locals to disk, thaw them later.**
   Rejected: Python coroutine frames are hard to persist reliably.
3. **A single-writer `journal.jsonl` beside the log.** Rejected: the EventLog is
   already the journal; a second one is duplication and two-source drift.
4. **RestrictedPython or subprocess isolation for safety.** Rejected: cost
   mismatch, and it cannot stop a model that already holds the shell; the
   requirement is determinism.
5. **Soft-block non-determinism — simply do not inject those names, and give up
   determinism for any script that reaches for them.** Rejected: too
   coarse-grained, and it erodes the determinism that re-run-from-the-top depends
   on. Non-deterministic constructs are hard-forbidden at translation time
   instead.
6. **Run the whole orchestration inside one synchronous run-to-completion tool
   call.** Rejected: a single tool call cannot survive stop-and-go plus a crash
   restart.
7. **Add a `script` parameter to `spawn_subagent` and reuse it.** Rejected: it
   breaks that tool's schema stability and muddies description-driven routing.
8. **Reuse the session-level `output_schema` for each helper.** Rejected: both
   the granularity and the semantics are wrong — one per session versus one per
   subtask receipt.
9. **A `pipeline()` primitive that keeps the workflow running while stages are in
   flight.** Rejected: it needs a wake-on-any mode and a stable per-call-site
   identity (a chain hash), and it is a throughput optimization rather than a new
   capability, so it is not offered.

## Consequences

- The interpreter Policy, its determinism sandbox and the `run_workflow` control
  tool all live in the `react` built-in (`noeta.builtins.react.impl` —
  `orchestration`, `workflow_sandbox`, and the `run_workflow` `control_tool`
  contribution). `noeta.policies` holds only the neutral translate mechanism and
  the shared control-tool names (see
  control-tool-contributions-and-activation-identity.md).
- The embedding host resolves an orchestration child to the interpreter Policy
  and wraps a helper's Policy with the structured-output decorator when the
  child's inputs carry a schema.
- The subtask drain that pairs helper results back to their spawning calls lives
  in `noeta.execution`, reusing the group spawn decision and group wake condition
  from subtask-fanout-and-durable-wake.md.
