# Troubleshooting

Common issues and how to resolve them. Each entry follows **Symptom →
Cause → Resolution**.

## The platform answers with the mock script instead of a real model

**Symptom:** `GET /api/v1/health` returns `{"provider": "mock"}` (and every
session plays the same scripted demo) even though you configured a gateway.

**Cause:** `LLM_PROVIDER=auto` resolves to the offline mock unless **both**
`LLM_BASE_URL` and `LLM_API_KEY` are set — one empty value silently falls
back.

**Resolution:**
- Set both keys in `apps/noeta-agent/.env` (environment variables override
  the file; make sure a stale exported variable isn't blanking one).
- Remember `LLM_BASE_URL` is the gateway **root** — `/responses` is appended
  by the provider.
- `LLM_PROVIDER=openai` makes the fallback loud: boot fails instead of
  degrading to mock.

## Task fails with "max_iterations exceeded"

**Symptom:** A session terminates with a budget denial reason like
`"max_iterations=5 exceeded"` or `"max_tool_calls=3 reached"`.

**Cause:** `BudgetGuard` denied the next action because a configured
budget axis (iterations, tool calls, cost, spawned subtasks) was crossed.
The task still ran and produced durable envelopes — it just terminated
unsuccessfully.

**Resolution:**
1. Inspect the trace (admin console → Trace) to see which budget axis fired
   and why the task needed so many steps.
2. Programmatic (SDK) callers can raise the budget by passing a
   `BudgetSpec` via `Options.budget`.
3. Or trim the task's scope to require fewer steps.

## Tool call denied by PermissionGuard (SDK)

**Symptom:** Your SDK agent tries to use a tool and gets a
`ToolCallDenied` event; the trace shows the denial reason.

**Cause:** `PermissionGuard` rejected the tool call because the tool is
not in the agent's `allowed_tools` set, or the `permission_mode`
requires explicit approval for that risk level.

**Resolution:**
- Widen `allowed_tools` in your `Options` to include the tool.
- Or resolve the approval programmatically (`Options.can_use_tool`, or the
  `Client.approve` / `deny` verbs).
- Note the platform itself has **no per-call approval flow** — execution is
  sandbox-only by design, so this entry applies to library users.

## Suspended task never wakes up

**Symptom:** A task is in `suspended` status but never transitions to
`running`, even though the condition it is waiting for seems to have
been met.

**Cause:** Several possibilities:
- The wake event has not been produced yet (e.g. a timer whose
  `fire_at` has not been reached, or a subtask that has not finished).
- The wake event was produced but does not match the suspended task's
  `WakeCondition` (projection mismatch on identity fields).
- No worker is draining the queue (embedded-library deployments).

**Resolution:**
1. Check if the wake event exists: for timers, verify `fire_at` is in
   the past; for subtasks, verify the child reached a terminal state.
2. Inspect the task's raw trace — a task waiting on something that has not
   happened yet is working as designed.
3. Embedded-library users: ensure a `WorkerLoop` is draining the
   dispatcher (see [Deploy a worker](../how-to/deploy-worker.md)). The
   platform runs its own resident worker pool (`AGENT_NUM_WORKERS`), so
   this only applies to your own hosts.

## Provider returns 401 / authentication error

**Symptom:** Turns fail with an authentication or permission error from
the LLM gateway.

**Cause:** The API key is missing, expired, or does not have access to
the requested model.

**Resolution:**
- Platform: verify `LLM_API_KEY` (primary gateway, `api-key` header) or
  `SECONDARY_LLM_API_KEY` (secondary gateway, `Authorization: Bearer`).
- SDK: verify the key passed to the provider adapter.
- If using a corporate proxy, set `HTTPS_PROXY` in the environment.

## "Model not found" or provider error

**Symptom:** The provider returns a model-not-found or unknown-model
error, or the composer rejects the model with a 422.

**Cause:** The platform's model menu comes from
`apps/noeta-agent/models.json`, not from the gateway — a menu entry whose
`id` the gateway does not serve fails at the gateway; a model the menu does
not list fails validation before the turn starts.

**Resolution:**
- Make each `models.json` `id` an exact model name your gateway serves.
- Vendor naming gotchas: Anthropic model names include the date suffix
  (`claude-sonnet-4-5-20250929`); check your key's access tier.

## The agent has no shell / files panel is empty

**Symptom:** The agent answers but cannot run commands or produce files;
`GET /sessions/{id}/files` returns an empty list.

**Cause:** The sandbox is off. Execution is **sandbox-only**: shell and
file side effects happen only inside a per-session Docker container. With
`SANDBOX_ENABLED=false` (the default) the platform runs in pure
conversation mode — shell execution disabled, no file surface.

**Resolution:**
- Set `SANDBOX_ENABLED=true` in `apps/noeta-agent/.env`, with a local
  Docker daemon and the stock AIO Sandbox image reachable
  (`ghcr.io/agent-infra/sandbox`).
- Check the backend log for container provisioning errors (image pull,
  port allocation).

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
- [Configuration](../reference/configuration.md) — every platform key
- [Wake & resume](../concepts/wake-resume.md) — how the wake machinery
  works
- [WorkerLoop reference](../reference/worker-loop.md) — constructor
  parameters and shutdown semantics
