# Task fork and turn interrupt (2026-07-31)

> **Status: Active** — two capability gaps from the owner's backlog (B1, B2).
> A third item (B3, "settings file layer") was dropped during shaping; the
> rationale is recorded under [Dropped: B3](#dropped-b3-settings-file-layer).

## Goal

Two conversation-control verbs the libraries do not have today, both of which
turn out to be small because the event-sourced ledger already carries the
machinery:

- **B1 — task fork.** Branch a conversation at a chosen user message: a *new*
  task that inherits the history up to that point, leaving the source
  conversation untouched. "Edit this message and try again, keeping both."
- **B2 — turn interrupt.** Stop an in-flight turn without killing the
  conversation. The task lands at its next-goal suspend and stays resumable —
  what pressing Esc in an interactive client should do.

Neither introduces a new execution primitive. B1 is `rewind` writing to a new
stream instead of re-basing its own; B2 is an entry point for a landing path
the worker already implements.

## Naming constraint (read first)

`CONTEXT.md` bans "session" as an **identity**, and `scripts/lint-naming.py`
fails any compound identifier containing it inside `packages/` and `examples/`.
The backlog called B1 "session fork"; the feature is **task fork** — it mints a
`task_id`, and the vocabulary for "the root of a conversation tree" is
`root_task_id`. No identifier introduced here may say `session`.

## What already exists

Established by reading the tree, not assumed:

| Piece | Where | Relevance |
| --- | --- | --- |
| `InteractionDriver.rewind` | `execution/driver.py:1674` | Folds to a point, serialises the 4-slice body, appends `TaskRewound{target_seq, state_ref}`. **Not exposed on `Client`.** |
| `BoundedEventLog` | `core/fold.py:75` | Point-in-time fold over a truncated prefix. |
| `TaskRewoundPayload` | `protocols/events.py:247` | The snapshot-shaped re-base marker B1 copies. |
| `_on_task_rewound` | `core/fold.py:301` | Overwrites the working `Task`'s slices in place so a from-scratch fold lands byte-equal to the accelerated one. |
| `SNAPSHOT_BASELINE_EVENT_TYPES` | `protocols/event_log.py:61` | The fold-baseline set; **growing it requires a SQL partial-index migration** on both backends. |
| `_settle_stopped_turn` | `runtime/worker.py:842` | Already lands an interrupted turn on a next-goal suspend — "reopenable by simply typing again". |
| `CancellationRegistry` | `runtime/cancellation.py` | Thread-safe cooperative-cancel marks; the Engine polls at both turn boundaries (`engine.py:910`, `:936`). |
| `_restore_dispatcher_to_baseline` | `execution/driver.py:1729` | Re-aligns dispatcher state to a folded baseline; `restore_task` upserts, so it works for an id the dispatcher has never seen. |

The gap in both cases is a **verb**, not a mechanism.

## B1 — task fork

### Scope

`InteractionDriver.fork(task_id, *, message_seq) -> DriveOutcome` and
`Client.fork(...)`. Also exposes the pre-existing `rewind` on `Client`, since
fork and rewind are the same anchor with opposite retention and shipping one
without the other is an arbitrary hole.

### Semantics

`message_seq` is the seq of a user-goal `MessagesAppended` — **the same anchor
`rewind` takes**, validated by the same `_rewind_keep_through`. The fork point
is the turn boundary *before* that message: the child inherits everything the
source had when it was resting just before that turn opened, and the caller
then sends whatever goal it wants down the new branch.

Reusing rewind's anchor is what makes the child guaranteed-resumable: the
baseline at a turn boundary is a next-goal suspend, so the forked task is
immediately live. A "fork at the tip" variant is deliberately **not** in v1 —
the tip may be mid-turn or terminal, and neither yields a coherent child
without synthesising a lifecycle the source never had.

### Mechanics

Read-only on the source. Nothing is written to the source stream, so a fork
can never perturb the conversation it branched from.

1. Read the source events; compute `keep_through` via `_rewind_keep_through`
   (rejects a `message_seq` that is not a real user message on this stream).
2. `baseline = fold(BoundedEventLog(events, keep_through), store, task_id)`.
3. Mint `child_id`; `engine.create_task(...)` writes the child's
   `TaskCreated` → `AgentBound` (→ `TaskHostBound`), carrying the source's
   `agent_name`, goal, and host binding.
4. Rewrite identity into the baseline body (`task_id=child_id`) and append
   `TaskForked{source_task_id, source_seq, state_ref}` to the **child** stream.
5. `_restore_dispatcher_to_baseline(child_id, baseline)`.

### Key decisions

- **`TaskForked` is a first-class re-base event**, registered in
  `SNAPSHOT_BASELINE_EVENT_TYPES`, `_REBASE_EVENT_TYPES` and fold's handler
  table, with the same `state_ref` shape as `TaskRewound`. The cheaper
  alternative — write a plain `TaskSnapshot` as the child's baseline and keep
  `TaskForked` as inert provenance — avoids the SQL migration but breaks the
  invariant that a from-scratch fold reproduces the accelerated fold: a forked
  task's inherited state is not derivable from its own genesis. Paying the
  migration keeps that invariant intact.
- **A fork is a sibling, not a subtask.** `parent_task_id` stays `None`;
  `parent_task_id` means delegation, and setting it would make
  `ChildLifecycleObserver` enqueue the fork as a child and wake a parent that
  never spawned it. Lineage lives in `TaskForked.source_task_id` and is
  discovered by scanning streams, the way `_genesis_parent` discovers subtasks.
- **Only a root task may be forked.** Forking a subtask would inherit a
  `parent_task_id` whose parent never spawned the fork. Refused loudly in v1.
- **Governance state is inherited, cost included.** The branch really did
  consume that context; a budget guard counting it is correct, not a leak.
- **Fork branches the conversation, not the workspace.** Both branches keep the
  source's `workspace_dir` and hit the same disk. Unlike `rewind`, there is no
  file restore — two live branches cannot both own one working tree. This is a
  documented limitation, not an oversight.

### Acceptance criteria

1. `Client.fork(task_id, message_seq=N)` returns a new `task_id` whose folded
   messages equal the source's messages as of the turn boundary before `N`.
2. The source stream is byte-identical before and after the fork.
3. `send_goal` on the fork drives a turn; the source stays independently
   resumable and the two diverge.
4. A forked task's from-scratch fold (`ignore_snapshots=True`) equals its
   accelerated fold.
5. Forking a non-root task, an unknown task, or a `message_seq` that is not a
   user message on that stream each fail loudly with a typed error.
6. Both durable backends round-trip `TaskForked` and return it from
   `find_latest_snapshot`; the widened partial index is exercised.
7. `Client.rewind` is exposed and covered.

## B2 — turn interrupt

### Scope

`InteractionDriver.interrupt(task_id, *, reason) -> DriveOutcome` and
`Client.interrupt(...)`.

### Semantics

Halt the in-flight turn at the next turn boundary; leave the conversation
suspended on its next-goal handle, reopenable by typing again. Distinct from
the two neighbours it sits between:

| Verb | Terminal? | Durable marker | Resumable |
| --- | --- | --- | --- |
| `cancel` | yes | `TaskCancelled` | no |
| `close` | no | `ConversationClosed` (archived) | yes |
| **`interrupt`** | **no** | **`TurnInterrupted`** | **yes** |

Today the interrupt landing is reachable only as a side effect of `close`
(which also archives) or by poking `request_cancellation` directly — and a bare
poke leaves **no durable trace** that a human stopped the turn.

### Mechanics

1. Fold; refuse a terminal task (`TaskAlreadyTerminalError`, matching
   `cancel` / `close` / `reopen`).
2. Write `TurnInterrupted{reason, interrupted_by}` via `system_emit` — a
   control-plane write, no lease, does not race the Engine's single
   `RuntimeState` writer. Written **before** marking the registry, the ordering
   `cancel` and `close` both use, so the re-fold in `_settle_stopped_turn`
   always sees it.
3. `request_cancellation(task_id)` → the Engine's next boundary poll raises
   `TaskCancellationRequested` → `_settle_stopped_turn` re-folds, sees no
   terminal, and suspends on the next-goal handle.
4. Cascade the same teardown `close` does: background shells killed, background
   sub-agents forgotten, per-turn carriers freed. **Not** `forget_turn_carriers`
   of the whole conversation — the task stays live.

### Key decisions

- **A new L0 control event, not a reused one.** `TaskCancelled` is terminal by
  fold; `ConversationClosed` means archived. Neither is "the human stopped this
  turn". Additive and absent from every historical recording, so byte-safe —
  the rule `ModelBound` / `TaskCancelled` / `TaskRewound` all followed.
- **The suspend reason reuses A1's channel.** `45a8061` added
  `YieldForHumanDecision.suspend_reason` → `TaskSuspended.reason`. An interrupt
  records `"interrupted"` there, so the ledger distinguishes an interrupted
  park from a failed one (`"turn_failed: …"`) and from an ordinary
  `waiting_human`. Nothing branches on the tag, so no protocol bump.
- **Interrupt is not a rewind.** The partial turn's events stay on the stream
  as real history — the model said what it said and the tools ran what they
  ran. Discarding it is `rewind`'s job, and the two compose.
- **No new engine seam.** The cooperative-cancel poll already sits at both
  turn boundaries. An interrupt cannot abort a tool call mid-execution, and v1
  does not pretend to: the boundary is the granularity.

### Acceptance criteria

1. `interrupt` on a task with an in-flight turn lands it `suspended` with a
   next-goal `wake_handle`; a following `send_goal` resumes the conversation.
2. `TurnInterrupted` is on the stream with its reason; `TaskSuspended.reason`
   is `"interrupted"`.
3. The task is **not** terminal and **not** marked closed.
4. `interrupt` on a terminal task raises `TaskAlreadyTerminalError`.
5. `interrupt` on an idle (already suspended) task is a clean no-op landing —
   no spurious suspend event, still resumable.
6. Background shells started by the interrupted turn are killed.
7. The partial turn's events remain on the stream (interrupt ≠ rewind).

## Dropped: B3 (settings file layer)

Dropped during shaping, with the owner's agreement, for lack of a demand these
libraries can actually serve:

- Configuration today is `Options(...)` + `HostConfig(...)`, passed in Python
  at `Client` construction. A settings file only adds value when someone must
  change behaviour without editing code — and `CONTEXT.md` is explicit that
  there is **no operator CLI** here. The only reader would be an embedding
  host, which is already writing Python.
- Most of `HostConfig` is not file-expressible anyway: `write_roots`,
  `mcp_server_resolver`, `sandbox_provider`, `delta_sink` and the rest are
  callables and live objects.
- The one durable motive — per-project config that travels with a repo —
  already exists, spread across `<workspace>/.noeta/skills`,
  `.noeta/shell-allowlist.json`, `.noeta/plugins`, `~/.noeta/memories` and
  `~/.noeta/trust.json`. Consolidating those is a tidying project with a
  different shape and a different justification.
- A workspace-tier settings file is config arriving from a cloned repository.
  If it could set `permission_mode` or `allowed_tools` it would be a privilege
  escalation on `git clone` — which is exactly why `plugin_set.py:16` already
  treats workspace `.noeta/plugins` as untrusted. Any real version of B3 must
  start from that constraint.

If B3 comes back it needs its own motivating scenario first.

## Verification

`make check` (**unpiped** — piping breaks it) after each of B1 and B2, plus the
storage-migration test for the widened partial index. Note that the working
tree carries a concurrent, unrelated content-store batching change; a failure
must be attributed before it is treated as this work's.
