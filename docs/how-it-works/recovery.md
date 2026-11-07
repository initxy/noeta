# One writer and crash recovery

A task can be run by any worker on any machine, and workers die, freeze and lose their network.
This page explains how noeta makes sure only one worker ever writes to a task, and what the next
worker does with whatever the last one left half done: a model call, a tool call, a plugin's
event handler.

## One writer

Before running a task, a worker **claims** it from the store:

- `claim(worker, queues, lease_seconds)` takes one claimable task, sets it `running`, names the
  worker as owner, **adds one to the task's epoch**, and sets a lease (`lease_until`). Its
  `reclaimed` says whether the claim before still held a lease, never given back
  ([a task that keeps killing its worker](#a-task-that-keeps-killing-its-worker)).
- `renew(claim, lease_seconds)` extends the lease. The runner's lease keeper does it every
  third of the lease (default lease 30 s; `noeta worker --lease`).
- `release(claim)` gives the claim back without ending the task (a worker shutting down): a
  running task becomes `ready`.
- `requeue(root_id, queue, to)` moves a task tree from one queue to another, unless one of its
  tasks has a lease that has not run out (a server taking over a front end's conversation,
  [HTTP API](../reference/http-api.md#talking-to-a-task)).

A task is claimable when it is `ready` (and due), `waiting` with a due `ready_at`, or `running`
or kept with a lease that has run out (`noeta/core/store.py`, `Store.claim`).

Every append then carries two conditions, checked by the store in the same transaction as the
write:

1. the claim's **epoch** is still the task's `owner_epoch`, and the claim still has a lease;
2. the log's last seq is the **`expected_seq`** the runner names (the last one it saw).

If either fails, the store raises `Conflict` and writes nothing. The runner stops at once,
without further writes.

<Diagram name="core-claim-epochs" />

This is why leases are only about timing. A lease decides when a silent worker's task may be
taken by someone else; it is a guess, and a wrong guess (a worker that was only slow) costs a
retry, never a double write. The fence is the epoch. A worker that froze (a long GC pause,
`SIGSTOP`, a network partition) and wakes up after another worker has claimed the task holds an
old epoch; its first append is refused, and it stops. "One writer" is a property the store
enforces, not a convention a frozen process could break
([ADR-001](https://github.com/initxy/noeta/blob/main/docs/adr/001-task-is-an-event-log-with-one-writer.md)).

How each store makes these checks race-free:

- **Postgres** (`noeta/backends/stores/postgres.py`). An append first locks the task row
  (`SELECT ... FOR UPDATE`), then checks epoch and seq; any other append, claim, renew, release
  or post on that task waits for the lock and then sees the result. A claim picks a row with
  `FOR UPDATE SKIP LOCKED`, so two workers never take the same task and neither waits for the
  other, and the claim index covers only rows a claim can take, so idle waiting tasks are never
  scanned. Lease times use the database's clock, so workers with skewed clocks agree on who
  holds a lease. A deadlock (an append posting to a task that is posting back) aborts one
  transaction, which is run again with its checks.
- **sqlite** (`noeta/backends/stores/sqlite.py`). Every write is one `BEGIN IMMEDIATE`
  transaction with the checks inside, so several processes on one machine can share the file.

The store contract tests (`noeta/testing/store_contract.py`) hold any store to these rules: two
racing runners never both append, a lapsed lease is reclaimed and the old owner loses, a kept
wait stays with its claim.

### When a worker loses its claim

| What happens | What the runner does |
|---|---|
| A renewal returns nothing (the claim was taken) | Marks the claim lost and cancels the step in flight; the next write would fail anyway. |
| A renewal keeps failing (the store is unreachable) | Retries until the lease would have run out by its own clock, then gives the task up the same way. |
| An append raises `Conflict` | Stops with no further writes. |
| The store is unreachable during a step (`StoreUnavailable`) | Stops. The lease runs out and the task is claimed again once the store is back. |
| The connection drops while `COMMIT` is in flight (`CommitUnknown`) | Never retried blindly: the runner stops as if its claim were lost, and whoever runs the task next reads the log to see what landed. For a claim, the worker looks for a task the store says it owns and adopts it, rather than leaving it until the lease runs out. |
| The worker shuts down | `release` hands each running task back as `ready` at once; tasks waiting with a kept claim stay waiting ([kept claims](./waiting.md#kept-claims)). |

A runner that loses its claim also loses whatever it kept for the task: its kept claims are
marked lost, so a background job it owned is stopped.

## Recovery is folding the log

There is no separate recovery procedure. The next worker to claim the task folds its log like
any claim does, and `next_action` sees what the log shows. Two things can be half done: a
model call and a tool call.

<Diagram name="core-tool-recovery" />

### A model call cut short

`ModelCalled` is appended before the request is sent. A crash during the call leaves a
`ModelCalled` with no `ModelReplied`. The turn is still open, so the next claimer simply calls the
model again. The request is rebuilt from the same log, so it is the same bytes, and the
provider's prompt cache still applies. Partial streamed text was never recorded and is not
needed.

A call that kills its worker every time would loop. `TaskState.model_attempts` counts
`ModelCalled` events without a reply; at 3 the turn fails with
`llm_error: the model call kept being interrupted` (an interactive task then waits for the next
message, a one-shot task ends `failed`). Retrying a provider error inside one call is separate,
and does not count here ([the model step](./one-step.md#when-the-provider-fails)).

### A tool call cut short

`ToolStarted` is appended before a tool runs. A crash during the call leaves a `ToolStarted`
with no `ToolFinished`: the tool may have done all, part or none of its work. The next claimer's
`next_action` returns `Interrupted`, and `recover_calls` (`noeta/core/runner/calls.py`) decides
for each such call, in this order:

1. **The tool looks the outcome up.** A tool may define `recover(input, ctx)`. It gets the same
   `ctx.idempotency_key` the call had, `<task id>:<call id>`, the same on every worker, and
   returns the result if it can tell what happened, or `None`.
2. **The tool is safe to run again.** A tool whose `ToolFlags` say `retry_safe` (running it again
   changes nothing outside the call: `Read`, `WebFetch`, `WebSearch`, `ToolSearch`, the MCP
   resource readers, the todo tools...) is started again, up to `MAX_ATTEMPTS` (3) starts in
   all, so a call that kills its worker is not run forever.
3. **Otherwise the outcome is unknown.** The call gets an error result telling the model:
   "Interrupted before its result was recorded, so its outcome is unknown — it may have fully,
   partly, or not applied. Check its effect before running it again." The model, which can see
   the files and run commands, checks for itself.

Recovered calls are not gated again: each was allowed when it ran, and the log records the call
as it ran (after any rewrite). A parked call is not "cut short": it is waiting for its answer, and
carries on when the answer comes.

Side effects are therefore **at most once** unless a tool opts in. That is the safe default: a
blind re-run of `rm`, a payment or a deploy is worse than asking the model to look. The key is
for tools that can do better:

- Pass `ctx.idempotency_key` to an outside system that drops repeats (an HTTP
  `Idempotency-Key` header). The MCP plugin sends it on every `tools/call` in `_meta`.
- Use it as a dedupe key when posting to another task: `SubagentHandback`'s `recover` simply
  runs again, because its report is posted to the parent with the key
  `subtasks:handback:<key>`, and a repeat is dropped.
- Clean up instead: the `Agent` tool's `recover` removes a worktree a launch left behind (the
  launch itself is written with the call's result, so a cut-short call launched no agent).

Child tasks are safe by construction: a tool's children are created in the same append as its
`ToolFinished` (or its park), and their ids are derived from the call (`<task>.<call>.<n>`).
A crash before the append leaves no child; a crash after it leaves the result recorded.

### Plugin hooks and their records

Records and posts that a plugin makes during a hook are appended together with the event they
belong to: a tool's records with its `ToolFinished`, a notice a plugin enqueues with the record
that decided it (`TaskView.enqueue`). A crash keeps both or neither.

`on_event` runs after an event is committed, so a crash can lose a call. A plugin that cannot
afford that keeps a cursor in its own state and returns it from `delivered_through(view)`; every
claim then passes it the events after the cursor again, so it sees each one at least once (and
should make its side effect idempotent on the event's seq). A `Waiting` hands the task back only
after every `on_event` for it ran and its records landed, so a crash in between leaves the task
claimable, and the wait is not written twice.

### A task that keeps killing its worker

The bounds above are per call. A run can also kill its worker somewhere else: a plugin hook
that runs out of memory, a native library that segfaults while the request is built, a model
that calls a process-killing command again after reading "outcome unknown", a hang that stops
the lease from being renewed. Each pickup would then crash again, and with several workers
the task would take them down one after another.

The store tells a claim whether the claim before it was given back: `Claim.reclaimed` is true
when the row still had a lease (nobody released it and no append handed the task back), read in
the claim's own transaction. A release on shutdown is not counted. The worker that will run the
task (after the plugin check, so a worker that leaves the task to another writes nothing) records
`core/reclaimed` before the plugins' `on_start`. The count lives in the log, so it is written by
the next worker, never by the dying one, and its append is fenced by the claim's epoch like any
other: two workers racing for the task cannot both count.

The fold counts these records in `TaskState.reclaims` until the task **finishes a step after
one**: a model reply recorded after the latest `reclaimed` whose calls all have results (a reply
with no calls is finished at once). Finishing the call that was cut short does not count, since it
started before the crash. Parking does not reset the count either: a crash can come right after
the `Waiting` is written, before the claim is handed back. A healthy task that loses its machine
once records one `reclaimed` and resets at its next step.

A task gets `Runtime.restart_limit` such restarts in a row (default 3;
`noeta worker --restart-limit`, `0` no limit). The pickup after one more crash ends it `failed`
instead of running it, its open calls answered, with
`restart_limit: the task's run stopped without handing it back 4 times in a row ...` as its error.
That is an ordinary `TaskEnded`: front ends show it like any failure, and a sub-agent's parent
gets it as a failed `ChildEnded`. The default sits above the per-call bounds above, so a model
call or a `retry_safe` call that keeps dying is stopped by its own bound first and the model can
go on.

## Plugin pins

A task records the plugins it runs with when it first starts, and again whenever a pickup finds
them changed: `core/plugins`, each plugin's `registry.pin` (name, source, package version or
file hash, a hash of its arguments).

A worker that lacks a plugin the log records never runs the task without it: it might fold
differently, gate differently, or miss a tool the model was promised. It writes nothing and lets
its lease run out, so a worker that has the plugin can claim the task (a rolling install). After
`LOAD_ATTEMPTS` (3) such claims in a row on one worker with the log unchanged, it ends the task
`failed` with `plugin_missing`, naming the plugins, rather than leaving it to bounce forever
([ADR-023](https://github.com/initxy/noeta/blob/main/docs/adr/023-snapshots-per-fold-version-and-newer-logs-left-to-newer-workers.md)).

A plugin that *changed* (a new version, other arguments) is recorded again and the task runs on
with it from its next turn; the change is visible in the log, never silent. Reloading plugins on
a running system is covered on [The plugin system](./plugins.md) and
[Many machines](./many-machines.md).

## Snapshots and versions

A snapshot's version is a hash of the code that folds (the kernel's fold modules, the agent,
every plugin's pin and source). A worker whose version differs ignores it and folds the log from
the start; a snapshot can be missing, never wrong
([A task is a log](./event-log.md#snapshots)).

A log entry this version cannot decode because a newer version wrote it is left to a newer
worker: the claim writes nothing and its lease runs out. A log that cannot be read for any other
reason ends the task `failed` with `unreadable_log` after 3 claims in a row
([When a log cannot be read](./event-log.md#when-a-log-cannot-be-read)).

## Every retry is bounded

Recovery never loops forever. The bounds, all in `noeta/core/`:

| What repeats | Bound | Then |
|---|---|---|
| A provider error inside one model call | 9 attempts (`retry.DEFAULT_MAX_ATTEMPTS`), backoff up to 30 s | the turn fails `llm_error` |
| A model call whose worker dies | 3 `ModelCalled` without a reply (`texts.MAX_MODEL_ATTEMPTS`) | the turn fails `llm_error` |
| A `retry_safe` tool call whose worker dies | 3 starts (`tool.MAX_ATTEMPTS`) | "outcome unknown" |
| A bug in the runner or a plugin hook | 2 in a row | the first fails the turn `internal_error`, the second ends the task `failed` |
| A log that cannot be read, or a missing plugin | 3 claims in a row on one worker (`task.LOAD_ATTEMPTS`) | the task ends `failed` |
| Output cut at the limit, empty replies | 3 in a row each | the turn fails `llm_truncated` / ends without an answer |
| A restart after a claim that was never given back, with no step finished | 3 in a row (`Runtime.restart_limit`) | the next crash ends the task `failed` `restart_limit` |

## The proof: `make cluster`

The claims on this page are tested end to end by `make cluster` (`tests/cluster/`), which needs
Docker or a Postgres DSN (`NOETA_TEST_POSTGRES_DSN`). It starts several `noeta worker --lease 1`
processes on one Postgres store, with a scripted model (`tests/cluster/scripted`) that drives
tool calls with a journalled side effect, background `Bash` jobs, sub-agents, `ScheduleWakeup`
timers and cron jobs, and messages sent while they run. Meanwhile it `SIGKILL`s workers at
random and respawns them, kills every worker at once, and `SIGSTOP`s workers for one and a half
to three leases, then resumes them. Afterwards it checks:

- every task finishes its script within the deadline;
- no side-effect token is journalled twice, and a call the log shows finished was journalled
  exactly once;
- every message sent is taken by its task exactly once;
- every background job ends in the log as ended or lost, never missing, and the model is told
  exactly once;
- the log invariants hold (`tests/cluster/invariants.py`): model calls pair with replies, tool
  starts with finishes, no inbox item is taken twice, and a sampled task row's epoch never goes
  back and names one owner.

`NOETA_CLUSTER_SEED` replays a run; `NOETA_CLUSTER_TASKS` and `NOETA_CLUSTER_SECONDS` size it.
It is not part of `make check`.

## Related

- [A task is a log](./event-log.md): the fold that recovery relies on.
- [Waiting and waking](./waiting.md): kept claims and what happens when their worker dies.
- [Many machines](./many-machines.md): deploying workers on Postgres.
- [Troubleshooting](../operations/troubleshooting.md): `plugin_missing`, `unreadable_log` and other failures as an operator sees them.
