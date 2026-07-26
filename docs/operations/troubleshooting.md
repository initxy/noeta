# Troubleshooting

Common issues and how to resolve them. Each entry follows **Symptom →
Cause → Resolution**.

## Task fails with "max_iterations exceeded"

**Symptom:** A task terminates with a budget denial reason like
`"max_iterations=5 exceeded"` or `"max_tool_calls=3 reached"`.

**Cause:** `BudgetGuard` denied the next action because a configured
budget axis (iterations, tool calls, cost, spawned subtasks) was crossed.
The task still ran and produced durable envelopes — it just terminated
unsuccessfully.

**Resolution:**
1. Inspect the task's EventLog to see which budget axis fired and why the
   task needed so many steps.
2. Raise the budget by passing a `BudgetSpec` via `Options.budget`.
3. Or trim the task's scope to require fewer steps.

## Tool call denied by PermissionGuard

**Symptom:** Your agent tries to use a tool and gets a `ToolCallDenied`
event; the trace shows the denial reason.

**Cause:** `PermissionGuard` rejected the tool call because the tool is
not in the agent's `allowed_tools` set, or the `permission_mode`
requires explicit approval for that risk level.

**Resolution:**
- Widen `allowed_tools` in your `Options` to include the tool.
- Or resolve the approval programmatically (`Options.can_use_tool`, or the
  `Client.approve` / `deny` verbs).

## Suspended task never wakes up

**Symptom:** A task is in `suspended` status but never transitions to
`running`, even though the condition it is waiting for seems to have
been met.

**Cause:** Several possibilities:
- The wake event has not been produced yet (e.g. a timer whose
  `fire_at` has not been reached, or a subtask that has not finished).
- The wake event was produced but does not match the suspended task's
  `WakeCondition` (projection mismatch on identity fields).
- No worker is draining the queue.

**Resolution:**
1. Check if the wake event exists: for timers, verify `fire_at` is in
   the past; for subtasks, verify the child reached a terminal state.
2. Inspect the task's raw trace — a task waiting on something that has not
   happened yet is working as designed.
3. Ensure a `WorkerLoop` is draining the dispatcher (see
   [Deploy a worker](../how-to/deploy-worker.md)) — nothing launches it for
   you.

## Provider returns 401 / authentication error

**Symptom:** Turns fail with an authentication or permission error from
the LLM endpoint.

**Cause:** The API key is missing, expired, or does not have access to
the requested model.

**Resolution:**
- Verify the key passed to the provider adapter.
- If using a corporate proxy, set `HTTPS_PROXY` in the environment.

## "Model not found" or provider error

**Symptom:** The provider returns a model-not-found or unknown-model
error.

**Cause:** The `model` you pass to `query` / `Client` is not an id the
endpoint serves.

**Resolution:**
- Make the `model` an exact model name your endpoint serves.
- Vendor naming gotchas: Anthropic model names include the date suffix
  (`claude-sonnet-4-5-20250929`); check your key's access tier.
- A model the SDK catalog does not know needs its `context_window` /
  `max_output_tokens` supplied so context compaction can engage.

## WorkerLoop: step abandoned on shutdown

**Symptom:** After sending SIGTERM, the worker log shows
`shutdown_abandoned` and `loop.abandoned = True`.

**Cause:** The in-flight step did not complete within
`shutdown_grace_s` (default 30 seconds). The loop abandoned it.

**Resolution:**
- **Exit the process.** Python cannot interrupt the abandoned step
  thread; it may still be writing to the EventLog. In-process reuse
  after abandon is unsupported.
- After the process exits, the lease expires and `requeue_stale()`
  reclaims the task on the next start.
- To avoid this, increase `shutdown_grace_s` when constructing the
  `WorkerLoop`, or set it to `None` for unbounded wait (then a truly
  stuck step needs `kill -KILL <pid>`).

## See also

- [Known limitations](limitations.md) — architectural boundaries that
  are not bugs
- [Wake & resume](../concepts/wake-resume.md) — how the wake machinery
  works
- [WorkerLoop reference](../reference/worker-loop.md) — constructor
  parameters and shutdown semantics
