# An orchestration script the model writes on the spot = one Task + one Policy that interprets it (helpers are real Subtasks)

## Context

We want Noeta to be able to "let the model write a small orchestration script on the spot, dispatch a few helpers, then look at intermediate results and change direction" — without adding any runtime primitive.

It builds on the "workflow → compiled into Task+Policy" route (see task-as-only-primitive.md), grouped fanout / join (see subtask-fanout-and-durable-wake.md), and AgentSpec / registry (see agent-identity-and-provenance.md).

## Decision

- **One workflow run = one Task; the orchestration script = one Policy that interprets it; zero new runtime primitives.** A workflow run is exactly "a recorded, suspendable, resumable agent execution" = a Task, so we reuse it directly rather than inventing a new container; the script is interpreted by a Policy. **There is no `WorkflowRunner` / `WorkflowPolicy` class** (holding the "Workflow is not a first-class concept" red line in CONTEXT.md).
- **Each helper = a real Subtask (with its own EventLog), not a disposable worker.** Every helper an `agent()` call dispatches is a real Subtask with its own EventLog stream, so inspect, fold, and resume all apply automatically.
- **Stop-and-go relies on "re-run from the top + use the EventLog as a journal to skip completed steps" — not on freezing coroutines.** Each Policy step re-runs the script from the first line; `agent()` calls whose result is already in the EventLog are replayed instantly from the recording, and the first one without a result yet → emits a spawn decision and suspends to await the join. **The EventLog itself is that journal.**
- **The script sandbox guarantees determinism only, not safety.** `compile()` + `exec()` into a controlled `globals`: only the orchestration API is injected (`agent` / `parallel` / `log` / `budget` / `args`), not `time` / `random` / `datetime`, and a static AST check hard-forbids importing these non-deterministic sources and external IO. Reasoning: the model writing the script already holds the shell / file tools, so exec-ing the Python it wrote adds no new attack surface; what we want is determinism, not a security sandbox.
- **The entry point is a standalone control tool `run_workflow`, which lands as "spawn a child task that runs the orchestration Policy."** It is the same family as `spawn_subagent → SpawnSubtaskDecision` and shares the same pipeline; the only difference is that the child task runs an orchestration Policy rather than a roster agent. The tool only "submits the job"; the job itself is that Task.
- **Per-helper structured output = inject a `structured_output` tool + steer that child task toward it**, rather than reusing the session-level `output_schema` (which is reserved for "the shape of the top-level final answer"; see unified-context-supply.md).
- **Concurrency is an explicitly reserved follow-up of fanout (v2).** `parallel()` reuses the grouped barrier from subtask-fanout-and-durable-wake.md; the only thing real concurrency would break is deterministic fold/resume, because grouped-completion events are recorded into the EventLog in arrival order — the cornerstone envisioned then was **canonical-sorting by `subtask_id`** (the fanout ADR pre-wrote that patch) so that re-running from the top derives the same state regardless of arrival timing. v1 wires the skeleton onto the existing single-worker serial drain with zero new infrastructure; v2 adds a bounded concurrent executor + one lease relaxation (opt-in groups only). *(v2 update: subtask-parallel-execution.md landed v2 and found the canonical sort unnecessary — committing arrival order to the log is authoritative — so it was removed as dead defensive code.)*
- **v1's orchestration primitives are only `agent()` + `parallel()`; `pipeline()` is deferred.** Both have the property that "every call site stops and waits" → the parent task never "keeps running itself while subtasks are still in flight" → zero new wake mechanism. `pipeline()` needs wake-on-any plus a stable identity per call site (chain-hash), and it is merely a throughput optimization, not a new capability, so v1 skips it.

## Rationale

- **The first draft's copy of Claude Code's "an ad-hoc orchestration layer above the engine / a shadow execution engine" is backwards.** CC makes subagents disposable (running on a thread pool, discarded when done, written only to a side-channel journal, never entering the EventLog) because it has no durable substrate — ad-hoc is its ceiling. Noeta has that substrate (it records, schedules, and resumes agent execution from a single EventLog, and that durable substrate is the project's moat), so copying the ad-hoc model amounts to voluntarily surrendering the moat in the busiest multi-agent scenario.
- **Stop-and-go uses "re-run from the top + the EventLog as the journal" rather than frozen coroutines**: Python coroutine frames are hard to persist reliably, whereas the EventLog is already the source of truth — writing a second side-channel journal is duplication + two-source drift. This works precisely because the script is deterministic.
- **The sandbox only guarantees determinism**: stop-and-go re-runs from the top and must derive the same decision every time, which needs determinism; it can't stop a model that already holds the shell, so we don't add RestrictedPython / subprocess isolation for "safety" (cost mismatch).
- **`run_workflow` is a standalone tool rather than reusing `spawn_subagent`**: the latter's `{agent, goal}` schema is deliberately kept stable so old recordings still fold/resume cleanly; stuffing `script` in would change that schema, and would also jam two contracts into one tool, muddying `description` (the single source of truth for model-visible semantics).
- **The Engine is untouched**: script interpretation lives in the Policy, not the Engine, keeping the Engine ≤500 lines and workflow-agnostic.

## Alternatives considered

1. **An ad-hoc in-process orchestration layer above the engine (first draft / copy of CC).** Rejected: it bypasses Task / EventLog, subagents escape the durable record (no fold/resume), and it violates the vocabulary red line.
2. **Freeze a half-run coroutine and its locals to disk, thaw them later.** Rejected: Python coroutine frames are hard to persist reliably.
3. **A single-writer `journal.jsonl` (first draft's D5).** Rejected: the EventLog is already the journal; a second one is duplication + two-source drift.
4. **Add RestrictedPython / subprocess isolation for "safety."** Rejected: cost mismatch, and it can't stop a model that already holds the shell; what we want is determinism.
5. **"Soft-block (just don't inject) + give up determinism for any workflow that used non-determinism."** Rejected: too coarse-grained, it would slowly erode the determinism that stop-and-go re-run-from-the-top depends on; changed to hard-forbidding non-deterministic constructs outright.
6. **Stuff the entire orchestration into a synchronous run-to-completion tool-call body.** Rejected: a single tool call can't survive stop-and-go + crash restart.
7. **Add a `script` parameter to `spawn_subagent` to reuse it.** Rejected: it breaks the tool's byte stability + muddies description routing.
8. **Reuse the session-level `output_schema` per helper.** Rejected: both the granularity and the semantics are wrong (one per whole session vs. one per subtask receipt).
9. **Hard-land `pipeline()` in v1 / open leases globally in v1.** Rejected: prematurely introducing a new wake mode + in-flight tracking for a pure speedup feature is bad risk / reward; the lease relaxation applies only to opt-in groups and must ship together with the canonical sort. *(v2 update: subtask-parallel-execution.md removed that canonical sort — the recorded arrival order is authoritative — while keeping the per-group lease relaxation.)*

## Consequences

- The Policy that interprets orchestration scripts lands in `noeta.policies`; the deterministic script host (compile/exec + controlled namespace + AST guard) lands in `noeta.policies._workflow_sandbox`; the orchestration API lands in `noeta.policies.orchestration`.
- The `run_workflow` control tool → spawn-subtask translation lands in `noeta.policies.control_tools` / `noeta.policies.control_semantics`.
- The `structured_output` tool injection and subtask drain land in `noeta.execution`, reusing `SpawnSubtasksDecision` / `SubtaskGroupCompleted` from subtask-fanout-and-durable-wake.md.
- Follow-up note: `pipeline()` is still not done; when needed it will require wake-on-any and a per-call chain-hash stable identity; the authoritative implementation of concurrency is in subtask-parallel-execution.md.
