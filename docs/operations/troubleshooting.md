# Troubleshooting

Common issues and how to resolve them. Each entry follows **Symptom → Cause →
Resolution**.

## Task terminates with a budget denial

**Symptom:** A task terminates with a reason like `max_iterations=5 exceeded`
or `max_tool_calls=3 reached`.

**Cause:** `BudgetGuard` denied the next action because a configured axis was
crossed: `max_iterations`, `max_tool_calls`, `max_cost_usd`,
`max_spawned_subtasks`, or `max_subtask_depth`. The task ran and produced
durable envelopes — it just terminated unsuccessfully.

**Resolution:**

1. Read the task's EventLog to see which axis fired and why the task needed so
   many steps.
2. Raise the cap by passing a `BudgetSpec` through `Options.budget`.
3. Or narrow the task's scope so it needs fewer steps.

Note that `max_cost_usd` only fires for models the catalog prices — see
"a long conversation never compacts" below.

## Tool call denied by PermissionGuard

**Symptom:** A `ToolCallDenied` event whose reason is one of `tool 'X' denied
by policy`, `tool 'X' not in allowlist`, or `tool 'X' risk_level 'high'
exceeds max 'medium'`.

**Cause:** `PermissionGuard` rejected the call. `denied_tools` and the
`allowed_tools` allowlist are checked first, then the tool's declared
`risk_level` against the policy's ceiling.

**Resolution:**

- Widen `Options.allowed_tools` to include the tool, or drop it from
  `disallowed_tools`. Remember that setting `allowed_tools` *replaces* the
  default set rather than adding to it.
- If the denial is about risk level, the tool is above the ceiling this agent
  runs under; give it its own agent rather than raising the ceiling for
  everything.

## Tool call sits waiting for approval

**Symptom:** The task suspends instead of running the tool; the reason reads
`tool 'X' requires human approval`.

**Cause:** `permission_mode` selected an approval set. `default` gates every
tool whose declared `risk_level` is not `low`. `acceptEdits` applies the same
rule but exempts `edit`, `write`, and `apply_patch`. `bypassPermissions` gates
nothing. A `shell_run` whose command is outside the effective shell allowlist is
gated per call, independently of the mode.

**Resolution:** Resolve it — `Client.approve` / `Client.deny`, or a
programmatic `Options.can_use_tool` callback whose ruling is recorded as an
ordinary approval event. Change `permission_mode` if the whole class of calls
should run unattended.

## Suspended task never wakes up

**Symptom:** A task stays `suspended` and never returns to `running`, even
though the condition it waits on looks satisfied.

**Cause:** One of three things:

- The wake event has not been produced yet — a timer whose `fire_at` is in the
  future, or a subtask that has not reached a terminal state.
- The wake event was produced but does not match the task's `WakeCondition`
  (a projection mismatch on identity fields).
- No worker is draining the queue.

**Resolution:**

1. Check that the wake event exists: for timers verify `fire_at` is in the
   past; for subtasks verify the child is terminal.
2. Read the task's raw event stream. A task waiting on something that has not
   happened yet is working as designed.
3. Make sure a `WorkerLoop` is draining the dispatcher — see
   [Deploy a worker](../how-to/deploy-worker.md). Nothing launches one for you.

## Compilation fails with "unknown plugin activation"

**Symptom:** `compile_options` raises `ValueError: unknown plugin activation
'x' on ... — not a built-in activation (...) and not in the loaded plugin set
(...)`.

**Cause:** Activation names fail loudly by design, so a typo cannot silently
turn a capability off. The name in `Options.plugins` or
`AgentDefinition.plugins` is neither a recognised built-in activation nor a
plugin in the `PluginSet` handed to `Client`.

**Resolution:** Fix the spelling, or load the plugin first —
`load_plugins(...)` and pass the result as `Client(options, plugins=...)`. The
error message lists both the recognised built-in names and the loaded set.

## Provider returns 401 or another authentication error

**Symptom:** Turns fail with an authentication or permission error from the LLM
endpoint.

**Cause:** The API key is missing, expired, or lacks access to the requested
model.

**Resolution:** Verify the key passed to the provider adapter. Behind a
corporate proxy, set `HTTPS_PROXY` in the environment.

## Model rejected before the turn starts

**Symptom:** `ModelSelectorError` (`model_selector_rejected`) or
`ProviderSelectorError` (`provider_selector_rejected`) — and no task, no
`ModelBound`, no turn.

**Cause:** These are raised locally, before any durable write. The selector is
outside `principal.allowed_models ∩` the deployment allowlist, or the
`(provider, model)` pair names an unconfigured provider or a model that
provider does not declare.

**Resolution:** Both errors carry an `allowed` / `available` list of what you
could have picked. Pick from it, or widen the host's allowlist and provider
registry.

## "Model not found" from the endpoint

**Symptom:** The provider itself returns a model-not-found or unknown-model
error.

**Cause:** The `model` you passed is not an id that endpoint serves.

**Resolution:** Use an exact model name your endpoint serves. Anthropic ids
carry a date suffix (`claude-sonnet-4-5-20250929`); check your key's access
tier.

## A long conversation never compacts, and cost stays at $0.00

**Symptom:** Context grows until the provider rejects the request, and
`GovernanceState.cost` stays zero no matter how many turns run.

**Cause:** Both compaction and pricing derive from the model catalog. A model
the catalog does not describe gets `COMPACTION_OFF` and a price of `0.0` per
round-trip. Neither degradation raises, so nothing tells you.

**Resolution:** Add a `ModelSpec` row for the model. `CATALOG` and `ModelSpec`
are re-exported from `noeta.sdk.providers`; the row's `context_window`,
`max_output_tokens`, and price fields are everything both derivations read.

## Write refused: path resolves outside the workspace

**Symptom:** `edit`, `write`, or `apply_patch` returns an error saying the path
resolves outside the workspace, or is outside the writable allow-list.

**Cause:** Write tools resolve through the `WorkspaceRoot` fence. The target is
canonicalised — so `..` and symlink escapes are already collapsed — and must
land under the session workspace or under an extra root the host authorized.
Containment is component-wise, so `/srv/app-old` is not inside `/srv/app`.
Reads are not fenced; only writes are.

**Resolution:** Write inside the workspace, or authorize the directory through
`HostConfig.write_roots`, a `task_id -> directories` resolver consulted per
call. Because it is consulted per call, an authorization granted while a task
is paused takes effect on the resumed call without rebuilding the tool set.

## WorkerLoop: step abandoned on shutdown

**Symptom:** After SIGTERM, the log shows `shutdown_abandoned` and
`loop.abandoned` is `True`.

**Cause:** The in-flight step did not finish within `shutdown_grace_s`
(30 seconds by default for `WorkerLoop`, 10 for `Client.start_workers`), so the
loop abandoned it.

**Resolution:**

- **Exit the process.** Python cannot interrupt the abandoned step thread and
  it may still be writing to the EventLog. Reusing the loop in-process after an
  abandon is unsupported.
- Once the process exits, the lease expires and `requeue_stale()` reclaims the
  task on the next start.
- To avoid it, raise `shutdown_grace_s`, or set it to `None` for an unbounded
  wait — then a truly stuck step needs `kill -KILL <pid>`.

## A long step dies with InvalidLease

**Symptom:** A step that had been running for a long time fails when it next
writes to the EventLog; the worker emitted `heartbeat_invalid_lease`.

**Cause:** The dispatcher caps heartbeat extensions at `heartbeat_max` (360 by
default), so one step can hold a lease for at most
`heartbeat_interval × heartbeat_max`. Past the cap the lease is force-released
and the next lease-checked append fails.

**Resolution:** Treat this as an operational-failure signal rather than a
recovery path — the loop moves on, but the task needs inspection. If the step
is legitimately that slow, raise `heartbeat_interval` or the dispatcher's
`heartbeat_max`; otherwise find what is hanging.

## See also

- [Known limitations](limitations.md) — boundaries of the design, not bugs
- [Wake & resume](../concepts/wake-resume.md) — how the wake machinery works
- [WorkerLoop reference](../reference/worker-loop.md) — constructor parameters
  and shutdown semantics
