# Troubleshooting

Common issues and how to resolve them. Each entry follows **Symptom →
Cause → Resolution**.

## Server exits at boot: "needs NOETA_AGENT_API_KEY"

**Symptom:** `python -m noeta.agent` prints
`NOETA_AGENT_PROVIDER='openai' needs NOETA_AGENT_API_KEY` and exits.

**Cause:** You set `NOETA_AGENT_PROVIDER` to a real provider (`openai`,
`anthropic`, `openai-responses`) but did not provide credentials.

**Resolution:**
- Set `NOETA_AGENT_API_KEY=sk-…` in the environment.
- For `openai`, also set `NOETA_AGENT_BASE_URL=https://api.openai.com/v1`
  (or your OpenAI-compatible endpoint).
- Or use `NOETA_AGENT_PROVIDER=stub` (the default) for a fully offline
  smoke test — no key needed.

## Task fails with "max_iterations exceeded"

**Symptom:** A session terminates with a budget denial reason like
`"max_iterations=5 exceeded"` or `"max_tool_calls=3 reached"`.

**Cause:** `BudgetGuard` denied the next action because a configured
budget axis (iterations, tool calls, cost, spawned subtasks) was crossed.
The task still ran and produced durable envelopes — it just terminated
unsuccessfully.

**Resolution:**
1. Inspect the trace to see which budget axis fired and why the task
   needed so many steps.
2. Raise the budget: the coding agent's default lives in
   `noeta.agent.host.session.default_coding_budget()`. Programmatic
   callers pass a `BudgetSpec` via `Options.budget`.
3. Or trim the task's scope to require fewer steps.

## Tool call denied by PermissionGuard

**Symptom:** The agent tries to use a tool and gets a `ToolCallDenied`
event. The trace shows the denial reason.

**Cause:** `PermissionGuard` rejected the tool call because the tool is
not in the agent's `allowed_tools` set, or the `permission_mode`
requires explicit approval for that risk level.

**Resolution:**
- Widen `allowed_tools` in your `Options` to include the tool.
- Or change `permission_mode` to `"bypassPermissions"` for low-risk
  tools (not recommended for `edit`, `write`, or `shell_run`).
- Or, if using the web UI, click **Approve** on the pending approval
  prompt.

## Suspended task never wakes up

**Symptom:** A task is in `suspended` status but never transitions to
`running`, even though the condition it is waiting for seems to have
been met.

**Cause:** Several possibilities:
- The wake event has not been produced yet (e.g. a timer whose
  `fire_at` has not been reached, or a subtask that has not finished).
- The wake event was produced but does not match the suspended task's
  `WakeCondition` (projection mismatch on identity fields).
- The worker is not running (`WorkerLoop` is not draining the queue).

**Resolution:**
1. Check if the wake event exists: for timers, verify `fire_at` is in
   the past; for subtasks, verify the child reached a terminal state.
2. Inspect the task's folded detail (`GET /tasks/{id}`) — look for the
   `suspended_without_wake_event` diagnostic. If present, the task is
   simply waiting for something that has not happened yet.
3. Ensure a `WorkerLoop` is running and draining the dispatcher. The
   `python -m noeta.agent` server does **not** run a `WorkerLoop` — it
   drives turns inline. If you enqueued a wake externally, you need a
   worker to pick it up. See [Deploy a worker](../how-to/deploy-worker.md).

## Provider returns 401 / authentication error

**Symptom:** The agent fails with an authentication or permission error
from the LLM provider.

**Cause:** The API key is missing, expired, or does not have access to
the requested model.

**Resolution:**
- Verify `NOETA_AGENT_API_KEY` is set and correct.
- For Anthropic, keys start with `sk-ant-`; for OpenAI, `sk-`.
- Check the model name — some models require specific access or
  permissions.
- If using a corporate proxy, set `HTTPS_PROXY` in the environment.

## "Model not found" or provider error

**Symptom:** The provider returns a model-not-found or unknown-model
error.

**Cause:** The model name is wrong or the provider does not recognize
it.

**Resolution:**
- Anthropic model names include the date suffix:
  `claude-sonnet-4-5-20250929`, not `claude-sonnet`.
- OpenAI model names: `gpt-5.5`, `gpt-4o`, etc.
- Verify the model is available for your API key tier.

## Shell command rejected by allowlist

**Symptom:** `shell_run` returns a rejection even though
`NOETA_AGENT_SHELL_MODE=allowlist`.

**Cause:** The command is not in the structural allowlist. Only `git
status`, `git diff`, `pytest`, `uv run pytest`, `npm test`, and `pnpm
test` are allowed by default. Shell metacharacters (pipes, redirects)
are rejected before tokenization.

**Resolution:**
- Restructure the command to match an allowlisted form.
- Or set `NOETA_AGENT_SHELL_MODE=off` to disable `shell_run` entirely
  (safer than widening the allowlist).
- The allowlist is structural (argv-pattern based), not string-based —
  you cannot add custom commands via env vars. To extend it, modify the
  allowlist in code.

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
