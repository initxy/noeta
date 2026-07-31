# Troubleshooting

Things that go wrong in practice, and what to do about them. Every entry follows
the same shape — **symptom** (what you see), **cause** (what the runtime actually
did), **fix** (what to change).

If what you are hitting is a boundary of the design rather than a fault, it is
catalogued in [known limitations](limitations.md) instead.

Jump to the group that matches:

- [Something is blocked](#something-is-blocked) — budgets, permissions, the
  write fence
- [Something never happens](#something-never-happens) — a task that will not wake
- [Configuration is rejected](#configuration-is-rejected) — plugins, models,
  providers
- [Something degrades silently](#something-degrades-silently) — uncatalogued
  models
- [Workers misbehave](#workers-misbehave) — shutdown and lease symptoms

## Something is blocked

### Task terminates with a budget denial

**Symptom.** A task terminates with a reason like `max_iterations=5 exceeded` or
`max_tool_calls=3 reached`.

**Cause.** `BudgetGuard` denied the next action because a configured axis was
crossed: `max_iterations`, `max_tool_calls`, `max_cost_usd`,
`max_spawned_subtasks`, or `max_subtask_depth`. The task ran and produced durable
envelopes — it just terminated unsuccessfully.

**Fix.**

1. Read the task's event log to see which axis fired and why the task needed so
   many steps.
2. Raise the cap with a `BudgetSpec` through `Options.budget`.
3. Or narrow the task's scope so it needs fewer steps.

`max_cost_usd` only fires for models the catalog prices — see
[the silent-degradation entry](#a-long-conversation-never-compacts-and-cost-stays-0-00).

### Tool call denied by PermissionGuard

**Symptom.** A `ToolCallDenied` event whose reason is one of `tool 'X' denied by
policy`, `tool 'X' not in allowlist`, or `tool 'X' risk_level 'high' exceeds max
'medium'`.

**Cause.** `PermissionGuard` rejected the call. The policy's `denied_tools` set
(fed by `Options.disallowed_tools`) and the `allowed_tools` allowlist are checked
first, then the tool's declared `risk_level` against the ceiling this agent runs
under.

**Fix.** Widen `Options.allowed_tools` to include the tool, or drop it from
`disallowed_tools` — remembering that `allowed_tools` *replaces* the default set
rather than adding to it. If the denial is about risk level, the tool sits above
this agent's ceiling: give it its own agent rather than raising the ceiling for
everything.

### Tool call sits waiting for approval

**Symptom.** The task suspends instead of running the tool; the reason reads
`tool 'X' requires human approval`.

**Cause.** `permission_mode` selected an approval set. `default` gates every tool
whose declared `risk_level` is not `low`; `acceptEdits` applies the same rule but
exempts `edit`, `write`, and `apply_patch`; `bypassPermissions` gates nothing. A
`shell_run` whose command is outside the effective shell allowlist is gated per
call, independently of the mode.

**Fix.** Resolve it with `Client.approve` / `Client.deny`, or with a programmatic
`Options.can_use_tool` callback whose ruling is recorded as an ordinary approval
event. Change `permission_mode` if the whole class of calls should run
unattended.

### Write refused: path resolves outside the workspace

**Symptom.** `edit`, `write`, or `apply_patch` returns an error saying the path
resolves outside the workspace, or outside the writable allow-list.

**Cause.** Write tools resolve through the `WorkspaceRoot` fence. The target is
canonicalised — so `..` and symlink escapes are already collapsed — and must land
under the session workspace or under an extra root the host authorized.
Containment is component-wise, so `/srv/app-old` is not inside `/srv/app`. Reads
are not fenced; only writes are.

**Fix.** Write inside the workspace, or authorize the directory through
`HostConfig.write_roots`, a `task_id -> directories` resolver consulted per call.
Because it is per call, an authorization granted while a task is paused takes
effect on the resumed call without rebuilding the tool set.

## Something never happens

### Suspended task never wakes up

**Symptom.** A task stays `suspended` and never returns to `running`, even though
the condition it waits on looks satisfied.

**Cause.** One of three things:

- The wake event has not been produced yet — a timer whose `fire_at` is still in
  the future, or a subtask that has not reached a terminal state.
- The wake event was produced but does not match the task's `WakeCondition` (a
  projection mismatch on identity fields).
- No worker is draining the queue.

**Fix.**

1. Check the wake event exists: for timers verify `fire_at` is in the past; for
   subtasks verify the child is terminal.
2. Read the task's raw event stream. A task waiting on something that has not
   happened yet is working as designed.
3. Make sure a `WorkerLoop` is draining the dispatcher — see
   [Deploy a worker](../how-to/deploy-worker.md). Nothing launches one for you.

## Configuration is rejected

### Compilation fails with "unknown plugin activation"

**Symptom.** `compile_options` raises `ValueError: unknown plugin activation 'x'
on ... — not a built-in activation (...) and not in the loaded plugin set (...)`.

**Cause.** The name in `Options.plugins` or `AgentDefinition.plugins` is neither a
recognised built-in activation nor a plugin in the `PluginSet` handed to
`Client`. Activation names fail loudly by design, so a typo cannot silently turn
a capability off.

**Fix.** Correct the spelling, or load the plugin first with `load_plugins(...)`
and pass the result as `Client(options, plugins=...)`. The error message lists
both the recognised built-in names and the loaded set.

### Model rejected before the turn starts

**Symptom.** `ModelSelectorError` (`model_selector_rejected`) or
`ProviderSelectorError` (`provider_selector_rejected`) — and no task, no
`ModelBound`, no turn.

**Cause.** These are raised locally, before any durable write. Either the
selector is outside `principal.allowed_models ∩` the deployment allowlist, or the
`(provider, model)` pair names an unconfigured provider or a model that provider
does not declare.

**Fix.** Both errors carry an `allowed` / `available` list of what you could have
picked. Pick from it, or widen the host's allowlist and provider registry.

### Provider returns 401 or another authentication error

**Symptom.** Turns fail with an authentication or permission error from the LLM
endpoint.

**Cause.** The API key is missing, expired, or lacks access to the requested
model.

**Fix.** Verify the key passed to the provider adapter, or the environment
variable it falls back to. Behind a corporate proxy, set `HTTPS_PROXY` in the
environment — the adapters use `httpx`, which honours it.

### "Model not found" from the endpoint

**Symptom.** The provider itself returns a model-not-found or unknown-model
error.

**Cause.** The `model` you passed is not an id that endpoint serves.

**Fix.** Use an exact model name your endpoint serves. Anthropic ids carry a date
suffix (`claude-sonnet-4-5-20250929`); also check your key's access tier.

## Something degrades silently

### A long conversation never compacts, and cost stays $0.00

**Symptom.** Context grows until the provider rejects the request, and
`GovernanceState.cost` stays zero no matter how many turns run.

**Cause.** Both compaction and pricing derive from the model catalog. A model the
catalog does not describe gets `COMPACTION_OFF` and a price of `0.0` per
round-trip. Neither degradation raises, so nothing tells you.

**Fix.** Add a `ModelSpec` row for the model. `CATALOG` and `ModelSpec` are
re-exported from `noeta.sdk.providers`; the row's `context_window`,
`max_output_tokens`, and price fields are everything both derivations read. See
[Configure a provider](../how-to/configure-provider.md).

## Workers misbehave

### Step abandoned on shutdown

**Symptom.** After SIGTERM, the log shows `shutdown_abandoned` and
`loop.abandoned` is `True`.

**Cause.** The in-flight step did not finish within `shutdown_grace_s` — 30
seconds by default for `WorkerLoop`, 10 for `Client.start_workers` — so the loop
abandoned it.

**Fix.**

- **Exit the process.** Python cannot interrupt the abandoned step thread and it
  may still be writing to the event log. Reusing the loop in-process after an
  abandon is unsupported.
- Once the process exits, the lease expires and `requeue_stale()` reclaims the
  task on the next start.
- To avoid it, raise `shutdown_grace_s`, or set it to `None` for an unbounded
  wait — then a truly stuck step needs `kill -KILL <pid>`.

### A long step dies with InvalidLease

**Symptom.** A step that had been running for a long time fails when it next
writes to the event log; the worker emitted `heartbeat_invalid_lease`.

**Cause.** The dispatcher caps heartbeat extensions at `heartbeat_max` (360 by
default), so one step can hold a lease for at most
`heartbeat_interval × heartbeat_max`. Past the cap the lease is force-released
and the next lease-checked append fails.

**Fix.** Treat this as an operational-failure signal rather than a recovery
path — the loop moves on, but the task needs inspection. If the step is
legitimately that slow, raise `heartbeat_interval` or the dispatcher's
`heartbeat_max`; otherwise find what is hanging.

## Next steps

- [Known limitations](limitations.md) — boundaries of the design, not bugs
- [Deploy a worker](../how-to/deploy-worker.md) — the pool most of these symptoms
  come from
- [Wake & resume](../concepts/wake-resume.md) — how the wake machinery works
- [WorkerLoop reference](../reference/worker-loop.md) — constructor parameters
  and shutdown semantics
