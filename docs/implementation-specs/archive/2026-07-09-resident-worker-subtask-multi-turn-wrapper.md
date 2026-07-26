# Fix: under the resident multi-worker path a subtask gets wrapped by the multi-turn wrapper, causing a deadlock

> **Status: Shipped** — the fix is live: `resolver` detects a subtask build and drops the
> multi-turn `policy_wrapper` for it, so a child engine can no longer be wrapped
> into the deadlock this spec describes.

## Goal

Make `resolve_engine` stop wrapping subtasks (depth > 0 / has a
`parent_task_id`) in `MultiTurnReActPolicy(final=False)`, so that a subtask
claimed by an idle worker emits `TaskCompleted` when it finishes instead of
suspending on `noeta-code-next-goal`; and route a `__workflow__` child caught in
the same race to the `OrchestrationPolicy` engine instead of raising
`UnknownAgentError`.

## Non-goals

- No rearchitecting of the drain / worker claim race (no forcing subtasks to
  "only go through drain"). A worker claiming a subtask is an existing
  fault-tolerance path (`run_leased_task` already defends it with goal-seeding);
  it stays.
- No change to the in-process `CodeSessionRunner` CLI path (it goes through drain
  + `set_turn_final` and is unaffected as-is).
- No change to `multi_turn_policy_wrapper` itself, nor to `MultiTurnReActPolicy`'s
  semantics. The wrapper should only ever wrap a top-level session; that division
  of responsibility is unchanged. What changes is **who decides whether to wrap**.

## Context

### Root cause

`MultiTurnReActPolicy(final=False)` swaps a `FinishDecision` for a
`YieldForHumanDecision(prompt=NEXT_GOAL_WAKE_HANDLE)` (`multi_turn.py:81-91`), so
the task suspends "waiting for the next message" instead of genuinely finishing.
That wrapper **should only wrap a top-level session** (it is what supports
multi-turn conversation).

A subtask is a one-shot errand and should end at `TaskCompleted`. Two paths drive
a subtask:

1. **The drain path (correct).** `_build_subtask_engine` passes
   `policy_wrapper=None` explicitly (`resolver.py:795`), with a comment spelling
   out "children are one-shot, never multi-turn wrapped". After the parent steps,
   `_settle_subtasks` (`worker.py:1761`) drives the subtask synchronously.
2. **The worker-claim path (the bug).** With multiple workers
   (`num_workers ≥ 2`, `lifecycle.py:119`) or multiple processes sharing a
   dispatcher, the subtask enters the ready queue and an idle worker's `tick()`
   takes a non-targeted lease on it first, driving it through
   `run_leased_task → resolve_engine → _engine_for_agent`. `_engine_for_agent`
   applies `self.policy_wrapper` **unconditionally** (`resolver.py:1018`), with no
   depth/parent guard for subtasks. So the finished subtask is intercepted by the
   wrapper, suspends, and never emits `TaskCompleted`.

`ChildLifecycleObserver` wakes the parent only on
`TaskCompleted` / `TaskFailed` / `TaskCancelled` (`observers.py:93-136`) and does
not react to `TaskSuspended`. The parent, parked on the `SubtaskGroupCompleted`
barrier, is never woken → deadlock. A single worker never triggers it, because
after the parent steps it drains synchronously **on the same thread**, leaving no
second worker to claim the child.

### A second symptom of the same root cause

`resolve_engine` (`resolver.py:367-444`) special-cases only `"unnamed"` and has
**no `WORKFLOW_AGENT_NAME` branch**, whereas drain's `_build_subtask_engine`
(`resolver.py:763`) does. Under the same multi-worker race, a `__workflow__` child
claimed by a worker goes
`resolve_engine → _lookup_agent("__workflow__")` → `UnknownAgentError`.

### A trap that must be fixed alongside: the engine cache

`_engine_for_agent`'s cache key (`resolver.py:979-982`) is
`(agent_name, model, ask_user_question_enabled, workspace, provider,
permission_mode, mcp_aliases, effort, exec_env_ref)` — it carries **no "is this a
subtask / is the wrapper applied" dimension**.

If the root and the subtask share agent + model and have the same
`ask_user_question` value (an explorer subagent is typically `ask=False`, and the
root may be `ask=False` too), they hit the same cache entry. Whichever was built
first decides the contents: a root engine carrying the wrapper leaks to the
subtask, so the "subtasks are not wrapped" fix is masked by the cache. The drain
path dodges the cache by calling the uncached `_build_engine` directly for
subtasks (the comment at `resolver.py:813-815`); `resolve_engine` goes through
`_engine_for_agent` (cached) and has no such dodge.

### Precedent: `ask_user_question` already has the same depth mask

`resolve_engine` already disables `ask_user_question_enabled` for subtasks
(`resolver.py:432-436`: forced `False` when depth > 0 / a parent exists), and
`ask` is one dimension of the cache key. That is exactly the pattern the wrapper
fix should copy.

## Decisions

1. **The fix lands in `resolve_engine`, not in `_engine_for_agent`.** Isomorphic
   to the `ask_user_question` mask: once `resolve_engine` has determined "this is a
   subtask", it passes `policy_wrapper=None` through. `_engine_for_agent` gains a
   `policy_wrapper` parameter (defaulting to `self.policy_wrapper`) so this one
   override takes effect; every other caller is untouched.

   *Rationale*: the judgement "subtask identity decides whether to wrap" belongs
   in `resolve_engine` (which already holds the task and is already doing the
   depth check), not in `_engine_for_agent` (which has only the agent, with no
   parent/child relationship). Same location as the existing `ask_user_question`
   mask — readable, testable, minimal change.

2. **The subtask test is `parent_task_id is not None`**, exactly matching the
   existing `ask_user_question` mask (`resolver.py:434-435`:
   `getattr(task, "parent_task_id", None) is not None and subtask_depth == 0`). No
   new criterion is introduced.

3. **`__workflow__` routing: add a branch at the top of `resolve_engine`**,
   matching drain's `_build_subtask_engine` (`resolver.py:763`) — when
   `agent_name_of(...) == WORKFLOW_AGENT_NAME`, call
   `_build_orchestration_engine(task_id, allowed_subtask_agents=...)`.
   `allowed_subtask_agents` takes the parent's root agent's spawnable set (the
   same source as `_build_drain_host`'s `inherited_subtasks`); a workflow child
   does not itself recurse, so passing either the inherited set or the empty set
   works — take whichever matches drain, as the minimal implementation.

   *Rationale*: same race, same root cause, so close it in passing.
   `_build_orchestration_engine` is an existing abstract seam that SdkHost already
   implements, so the worker path can reuse it with no new code path.

4. **Add a subtask dimension to the cache key.** Append one dimension —
   `is_subtask: bool` (or the equivalent `policy_wrapper is None` flag) — to the
   end of `_engine_for_agent`'s cache key, so a "root engine with a wrapper" and a
   "subtask engine without one" never share an entry even when every other
   dimension matches.

   *Rationale*: without this, decision 1's fix is masked by the cache whenever
   root and subtask share agent + model. Adding a key dimension is the minimal
   change and matches the style of the existing 9-dimension key.

5. **The drain path is unchanged.** `_build_subtask_engine` already passes
   `policy_wrapper=None` and behaves correctly.

## Implementation plan

### 1. Give `_engine_for_agent` a `policy_wrapper` parameter

`resolver.py:890`. Add
`policy_wrapper: Optional[Callable[[Policy], Policy]] = None` to the signature,
compute
`effective_wrapper = policy_wrapper if policy_wrapper is not None else self.policy_wrapper`
internally, and pass it to `_build_engine` (`resolver.py:1018`).

Append a 10th dimension to the cache key (`resolver.py:979-982`):
`effective_wrapper is None` (a bool) — keying on "is the wrapper None" rather than
on the wrapper object itself (which is unhashable), and whose meaning is exactly
"is this a subtask engine".

### 2. `resolve_engine` passes `policy_wrapper=None` for subtasks

`resolver.py:367-444`. Reuse the `is_subtask` the `ask_user_question` mask already
computed (or an equivalent test); at both `_engine_for_agent` call sites (the
`unnamed` branch at line 413 and the main branch at line 429) pass
`policy_wrapper=None` when `is_subtask`, and otherwise pass nothing (defaulting to
`self.policy_wrapper`).

### 3. Add the `__workflow__` branch at the top of `resolve_engine`

In `resolve_engine` (around `resolver.py:386`, after
`name = agent_name_of(...)`): if `name == WORKFLOW_AGENT_NAME`, directly
`return self._build_orchestration_engine(task_id, allowed_subtask_agents=<inherited set>)`,
matching drain's `_build_subtask_engine` (`resolver.py:763-766`). Note that the
engine `_build_orchestration_engine` returns is not cached (it is built uncached
by design, as in drain), so the cache needs no change.

### 4. No change to worker / drain / multi_turn

`worker.py`, `subtask_drain.py`, `multi_turn.py` and `driver.py` are all
untouched.

## Task breakdown

- **T1** Add the `policy_wrapper` parameter to `_engine_for_agent` + add the
  `wrapper is None` cache-key dimension. (No prerequisite.)
- **T2** `resolve_engine` passes `policy_wrapper=None` for subtasks. Depends on T1
  (it uses the new parameter).
- **T3** Add the `__workflow__` branch at the top of `resolve_engine`. Could run
  in parallel with T2 (different positions in the same file), but since both edit
  `resolve_engine`, doing them serially is safer.
- **T4** Tests at all three levels (see Acceptance). Depends on T1–T3.

T1 → (T2, T3 serially) → T4.

## Dependencies / sequencing

T1 is the foundation (T2 needs its new parameter). T2 and T3 both edit
`resolve_engine`, so run them serially to avoid conflicts. T4 last. No external
dependencies.

## Acceptance criteria

1. **Multi-worker subtask completion (behaviour):** in a deployment with
   `num_workers ≥ 2`, an explorer subtask dispatched by the main agent emits
   `TaskCompleted` when it finishes (visible in the event stream), **not**
   `TaskSuspended(wake_on=HumanResponseReceived(handle="noeta-code-next-goal"))`.
   The parent is woken by `SubtaskGroupCompleted` and proceeds to `terminal`. —
   reproduce the original deadlock scenario and assert it no longer hangs.
2. **Workflow routing (behaviour):** with `num_workers ≥ 2`, when a
   `__workflow__` child is claimed by a worker, `resolve_engine` routes it to the
   `OrchestrationPolicy` engine and drives it normally, and does **not** raise
   `UnknownAgentError`.
3. **Cache isolation (unit test):** construct a scenario where root and subtask
   share agent + model + workspace and have the same `ask_user_question` value;
   assert the engine returned by `resolve_engine(root_task)` carries the
   `MultiTurnReActPolicy` wrapper, the one returned by
   `resolve_engine(child_task)` does **not**, and the two do not share a cache
   entry (assert on the `_engines` key set or on the wrapper type).
4. **Regression:** the single-worker (`num_workers=1`) path is unchanged; the
   drain path (in-process `CodeSessionRunner`) is unchanged; existing multi-turn /
   delegation / workflow tests all green.

## Risks

- **Compatibility of widening the cache key:** the key goes from 9 to 10
  dimensions. The cache is an in-process LRU (`_MAX_CACHED_ENGINES`), empty on
  restart, so there is no persistence-compatibility problem. But any test that
  asserts on the key's shape directly must be updated in step.
- **What `allowed_subtask_agents` should be in the `__workflow__` branch:** drain
  takes the root agent's spawnable set (`inherited_subtasks`). `resolve_engine`
  holds the subtask, so it must walk back to the root agent to obtain that set —
  confirm whether `_build_drain_host`'s set-collection logic can be reused in
  `resolve_engine`, or fall back to the empty set (whether a workflow child may
  spawn again depends on `run_workflow` semantics). Verify during implementation:
  if a workflow child never recursively spawns, the empty set is fine; take the
  inherited set if that is what makes the behaviour equivalent to drain.
- **The boundary of the subtask test:** the subtask test is
  `parent_task_id is not None`. Confirm whether a background subagent
  (`spawn_subagent(background=True)`) also goes through `resolve_engine` — if it
  takes an independent driver path (the background-subagent ADR) it is unaffected;
  if it does go through `resolve_engine`, the answer for it is likewise "do not
  wrap" (a background subtask is also one-shot). Verify the background path during
  implementation so the conclusion is consistent.

## Files / areas to inspect

- `packages/noeta-runtime/noeta/execution/resolver.py` — `resolve_engine` (:367),
  `_engine_for_agent` (:890), the cache key (:979), `_build_subtask_engine` (:758,
  for comparison), `_build_orchestration_engine` (:252, the abstract seam),
  `_build_drain_host` (:666, the `inherited_subtasks` collection logic).
- `packages/noeta-runtime/noeta/execution/multi_turn.py` —
  `MultiTurnReActPolicy` (semantics; unchanged).
- `packages/noeta-runtime/noeta/core/observers.py` — `ChildLifecycleObserver` (why
  a suspend does not wake the parent; unchanged, read to understand the deadlock).
- `packages/noeta-runtime/noeta/runtime/worker.py` — `run_leased_task` (:688), the
  subtask goal-seeding (:800, the evidence of worker claiming), `_settle_subtasks`
  (:1811).
- `packages/noeta-sdk/noeta/client/client.py:250` — the `policy_wrapper` wiring
  (confirm it is non-`None` for the resident path).
- `apps/noeta-agent/noeta/agent/backend/lifecycle.py:119` — the `num_workers`
  default.
- `docs/adr/engine-policy-dataflow.md` — the Decision / single-writer boundary
  (constraint reference).
- `docs/adr/subtask-fanout-and-durable-wake.md`,
  `docs/adr/worker-lease-model.md` — background on the race and the wake.
- Tests: `tests/test_code_multi_turn.py`, the delegation / subtask tests (the
  regression baseline).
