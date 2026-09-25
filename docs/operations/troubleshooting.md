# Troubleshooting

Each entry is a fault you can see, what the runtime did, and what to change. If you
hit a boundary of the design rather than a fault, see
[known limitations](limitations.md).

## Something is blocked

### Task ends with a budget denial

- **Symptom:** the task terminates with a reason like `max_iterations=5 exceeded` or
  `max_tool_calls=3 reached`.
- **Cause:** `BudgetGuard` denied the next action because an axis was crossed:
  `max_iterations`, `max_tool_calls`, `max_cost_usd`, `max_spawned_subtasks` or
  `max_subtask_depth`.
- **Fix:** read the task's event log to see which axis fired, then raise the cap with a
  `BudgetSpec` in `Options.budget`, or narrow the task. `max_cost_usd` only fires for
  models the catalog prices ([see below](#an-uncatalogued-model-warns-and-costs-0-00)).

### Tool call denied

- **Symptom:** a `ToolCallDenied` event with `tool 'X' denied by policy`,
  `tool 'X' not in allowlist`, or `tool 'X' risk_level 'high' exceeds max 'medium'`.
- **Cause:** `PermissionGuard` checks `denied_tools` (from `Options.disallowed_tools`)
  and the `allowed_tools` allowlist first, then the tool's `risk_level` against the
  agent's ceiling.
- **Fix:** add the tool to `Options.allowed_tools` (it *replaces* the default set) or
  drop it from `disallowed_tools`. For a risk-level denial, give the tool its own agent
  instead of raising the ceiling for everything.

### Tool call waits for approval

- **Symptom:** the task suspends with `tool 'X' requires human approval` (or
  `tool 'X' call requires human approval` for a per-call gate).
- **Cause:** `permission_mode`. `default` gates every tool whose `risk_level` is not
  `low`; `acceptEdits` does the same but exempts `Edit` and `Write`;
  `bypassPermissions` gates nothing. A `Bash` command outside the shell allowlist is
  gated per call in any mode.
- **Fix:** resolve it with `Client.approve` / `Client.deny`, or rule on it in code with
  `Options.can_use_tool`. Change `permission_mode` if the whole class should run
  unattended.

### Write refused: path outside the workspace

- **Symptom:** `Edit` or `Write` errors that the path resolves outside the workspace or
  the writable allow-list.
- **Cause:** writes are fenced to the task workspace. The path is canonicalised first
  (`..` and symlinks collapsed), and containment is per path component, so
  `/srv/app-old` is not inside `/srv/app`. Reads are not fenced.
- **Fix:** write inside the workspace, or grant the directory through
  `HostConfig.write_roots` (a `task_id -> directories` resolver, consulted per call, so a
  grant made while the task is paused applies when it resumes).

## Something never happens

### Suspended task never wakes

- **Symptom:** the task stays `suspended` although its condition looks satisfied.
- **Cause:** one of three: the wake event has not happened yet (a timer's `fire_at` is
  in the future, a subtask is not terminal); it happened but does not match the task's
  `WakeCondition`; or no worker is draining the queue.
- **Fix:** check the timer's `fire_at` or the child's status, read the task's raw event
  stream, and make sure a worker is running — nothing starts one for you
  ([Deploy](../guides/deploy.md)).

## Configuration is rejected

### "unknown plugin activation"

- **Symptom:** `compile_options` raises `ValueError: unknown plugin activation 'x' on ...`.
- **Cause:** a name in `Options.plugins` or `AgentDefinition.plugins` is neither a
  built-in activation nor in the `PluginSet` handed to `Client`. Typos fail loudly so a
  capability cannot silently turn off.
- **Fix:** fix the spelling, or `load_plugins(...)` and pass the result as
  `Client(options, plugins=...)`. The error lists both valid sets.

### Model or provider rejected before the turn

- **Symptom:** `ModelSelectorError` (`model_selector_rejected`) or
  `ProviderSelectorError` (`provider_selector_rejected`); no task is written.
- **Cause:** the model is outside `principal.allowed_models` ∩ the deployment
  allowlist, or the `(provider, model)` pair names an unconfigured provider or a model it
  does not declare.
- **Fix:** pick from the `allowed` / `available` list on the error, or widen the host's
  allowlist and provider registry.

### 401 or another authentication error

- **Symptom:** turns fail with an auth or permission error from the LLM endpoint.
- **Cause:** the API key is missing, expired, or lacks access to the model.
- **Fix:** check the key passed to the provider, or the environment variable it reads.
  Behind a proxy set `HTTPS_PROXY`; the adapters use `httpx`, which honours it.

### "Model not found" from the endpoint

- **Symptom:** the provider returns an unknown-model error.
- **Cause:** `model` is not an id that endpoint serves.
- **Fix:** pass an exact id the endpoint serves (for example `claude-sonnet-5`;
  see `catalog_models()` for the ids Noeta knows) and check your key's access tier.

## Something degrades silently

### An uncatalogued model warns and costs $0.00

- **Symptom:** a one-time `noeta` warning naming the model; compaction assumes a 128K
  window even if the model is larger; `GovernanceState.cost` stays zero.
- **Cause:** compaction, the output cap and pricing all come from the model catalog. An
  unknown model gets a 128,000-token window, a 16,384-token output cap and a price of
  `0.0`, each announced once in the log.
- **Fix:** register a `ModelSpec` row — `HostConfig(extra_models={...})` or
  `register_models` from `noeta.sdk.providers`. See [Connect a model](../guides/models.md).

## Workers misbehave

### Step abandoned on shutdown

- **Symptom:** after SIGTERM the log shows `shutdown_abandoned` and `loop.abandoned` is
  `True`.
- **Cause:** the in-flight step outlived `shutdown_grace_s` (30 s for `WorkerLoop`,
  10 s for `Client.start_workers`).
- **Fix:** exit the process — Python cannot stop the abandoned thread, and reusing the
  loop in-process is unsupported. The lease then expires and `requeue_stale()` reclaims
  the task on the next start. To avoid it, raise `shutdown_grace_s`, or set `None` to
  wait forever (a stuck step then needs `kill -KILL <pid>`).

### A long step fails with `InvalidLease`

- **Symptom:** a long-running step fails on its next event-log write; the worker
  emitted `heartbeat_invalid_lease`.
- **Cause:** heartbeat extensions are capped at the dispatcher's `heartbeat_max` (360),
  so one step holds a lease for at most `heartbeat_interval × heartbeat_max`.
- **Fix:** treat it as a signal to inspect the task, not a recovery path. If the step
  is legitimately that slow, raise `heartbeat_interval` or `heartbeat_max`; otherwise
  find what hangs.

## Next

- [Known limitations](limitations.md)
- [Deploy workers](../guides/deploy.md)
- [`WorkerLoop` reference](../reference/worker-loop.md)
