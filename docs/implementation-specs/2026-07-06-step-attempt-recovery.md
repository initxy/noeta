# Step-attempt recovery (partial-step-orphan crash edge)

P4 round 2. Shaped 2026-07-06 with the maintainer; this spec is the input to
`implement` and the checklist for `review`.

> **Implementation addendum (2026-07-06).** Refinements discovered while
> implementing — the ADR (`docs/adr/step-attempt-recovery.md`) is canonical
> where they differ from the Decisions below:
> 1. Seal reasons are `auto_redrive` / `unsafe_tool_activity` /
>    `interrupted_approval` / `abandon_cap` (not the earlier enumeration).
> 2. New covered corner: an **interrupted approval execution** (the
>    drive-side approval prelude crashed mid-tool) leaves a plan-less
>    activity window; the scanner anchors it on the first activity event
>    (`InterruptedAttempt.anchored_on_plan=False`), recovery always parks
>    it, and the park re-suspends on the approval's own handle — the seal
>    restores folded `pending_approvals`, so the ordinary approve/deny
>    verbs re-run it.
> 3. Park needs **zero operator verbs** (spec anticipated re-drive/close
>    verbs): typing resumes, close/cancel work as-is.
> 4. Hoists: `NEXT_GOAL_WAKE_HANDLE` → `noeta.protocols.wake`;
>    `BoundedEventLog` → `noeta.core.fold`.
> 5. `SeededTurn` pins the pre-prelude Engine so a seed-written
>    `ModelBound` keeps drive-the-next-turn `/model` semantics; a crash
>    re-drive of a model-switch turn runs on the new binding (accepted).
> 6. `PartialStepOrphan` is deleted outright (nothing re-raises it).

## Goal

A worker crash mid-step (`kill -KILL`, power loss) no longer strands or
silently terminates the task: recovery classifies the interrupted attempt by
side-effect risk, automatically abandons and re-drives it when that is safe,
and durably parks the task for a human decision when it is not. The
prelude-command durability gap closes: a `202`-acked `send_goal` / `answer`
can never lose the user's input.

## Non-goals

- Multi-worker / multi-host anything (rounds 3a / 3b). This design must not
  block them: the fencing ADR will reference attempt semantics, and everything
  here stays valid under N workers because recovery always runs under a lease.
- Resuming the interrupted attempt *in place* (replaying a half-finished tool
  call). The unit of recovery is the whole decide→act iteration; a partial
  iteration is never continued, only abandoned or left for a human.
- Compensation / undo of side effects a crashed tool already produced. The
  design only prevents *silent duplicate* execution; inspecting what a crashed
  `shell_run` actually did remains the operator's job (the parked question
  says exactly that).
- Changing any dispatcher schema or wake-delivery semantics. D1–D6 of
  `docs/adr/subtask-fanout-and-durable-wake.md` are untouched.
- `pipeline()` / join-policy work (dropped from the roadmap).

## Context

Established facts (verified in code 2026-07-06, `main` @ `e969067`):

- Detection already exists on the woken path: D4 case 5 in
  `noeta/runtime/worker.py:618` raises `PartialStepOrphan` when events follow
  a durable `TaskWoken` while the folded status is still `running`.
- The documented behavior ("worker raises a typed error; inspect manually") is
  NOT the real behavior: `WorkerLoop._execute_step` catches it in the generic
  exception branch → `dispatcher.fail(retryable=True)` → the task is re-leased,
  hits case 5 again, and after `max_fail_attempts` goes **silently terminal**
  (`worker.py:1051-1087`).
- The drained path is worse: an opening-turn crash (no `TaskWoken` exists)
  re-leases into `run_leased_task`'s drained branch (`worker.py:446`), which
  re-drives **directly on the dirty window** — no detection at all.
- The prelude gap (`worker.py:597-609`, D4 case 2): the seed writes wake +
  lease durably and returns `202`, but `TaskWoken` + the prelude events
  (user message / answer) are written later on the drive thread. A crash
  between the two writes loses a `202`-acked command.
- One step is a loop of decide→act iterations, and every iteration's first
  durable emit is `ContextPlanComposed` (`core/engine.py:843`). **The
  attempt journal already exists implicitly**: `ContextPlanComposed` is the
  attempt-start record (attempt identity = its seq; attempt intent = the
  plan + the decision events that follow it). The only missing piece is a
  durable attempt-*abandon* record.
- Reusable machinery: `TaskRewound` fold-reset semantics
  (`core/fold.py:841`), the `{TaskSnapshot, TaskRewound}` baseline set in
  `find_latest_snapshot` (sqlite/postgres/memory + `_BoundedEventLog` in
  `execution/driver.py:386`), bounded fold for point-in-time state
  (the rewind path), `suspend_on_human_handle` (`core/engine.py:1310`),
  `to_canonical_bytes` + ContentStore for the baseline snapshot.
- `risk_level` is a required `Tool` protocol field already carrying the
  approval semantics we need: read/glob/grep/webfetch/web_search/shell_poll/
  memory_read = `low`; memory_write = `medium`; edit/write/apply_patch/
  shell_run/shell_kill/skill_script and the MCP default = `high`.
  `PermissionGuard` gates approvals on the same field.

## Decisions

All confirmed with the maintainer 2026-07-06 (D1–D4 explicitly; the rest are
consequences worked out during shaping, to be recorded in the ADR).

**D1 — Recovery posture: classified automatic.**
An interrupted attempt whose recorded activity is provably side-effect-safe is
abandoned and re-driven automatically, no human involved. An attempt with
unprovable side effects parks the task durably for a human decision. Never
silently terminal, never silently duplicated.

**D2 — Classification rule: "whatever could run without a human approval
gate may be re-driven without a human."**
The classifier scans only the interrupted iteration (see D4) and derives
safety from the same surface `PermissionGuard` uses:

- Pure record events (`ContextPlanComposed`, LLM request records, assistant
  `MessagesAppended`, `TaskStatePatched`, compaction events) — always safe.
- Tool activity (started *or* completed — a re-drive re-decides, so completed
  calls in the dead window may be re-executed too): safe iff the call would
  not require human approval under the agent's permission config. Concretely:
  `permission_mode=bypassPermissions` ⇒ everything safe (maintainer's explicit
  call); otherwise `risk_level=low` safe, and whatever the mode auto-approves
  (e.g. edit-class under `acceptEdits`) safe; a configured `can_use_tool`
  callback is consulted with the recorded arguments.
- `SubtaskSpawned` in the window ⇒ park (spawned children are real).
- Unknown tool name (no longer in the agent's toolset) ⇒ park (conservative).

**D3 — Sealing mechanism: a new marker event + snapshot re-base; no journal
table.**
New EventLog event `StepAttemptAbandoned`, payload:
`state_ref: ContentRef` (canonical snapshot of the baseline state),
`abandoned_from_seq: int` (the interrupted iteration's `ContextPlanComposed`
seq), `reason: str` (`"crash_recovery"` / `"operator_redrive"` /
`"abandon_cap"`). Fold treats it exactly like `TaskRewound`: an in-stream
state reset to `state_ref`, and a member of the `find_latest_snapshot`
baseline set. The dead iteration's events stay in the log for audit. No
dispatcher change of any kind. Old recordings never contain the new type ⇒
zero drift; the payload is ~150 bytes ⇒ far under the 4 KB cap.

**D4 — Sealing granularity: only the interrupted iteration dies.**
Baseline = the folded state *just before the last `ContextPlanComposed`* of
the wake window (bounded fold, same trick as rewind). Completed iterations
before it — plan + decision + fully paired tool starts/completions — remain
live history: their side effects are not re-executed and their results are
not re-computed. Prelude events precede the first `ContextPlanComposed`, so
the user's message survives automatically.

**D5 — Case-machine sentinel: `ContextPlanComposed`, not "any event".**
D4-case semantics in `_run_woken` change from "any event after `TaskWoken`"
to "a `ContextPlanComposed` after `TaskWoken`":

- case 2′ (no `ContextPlanComposed` after `TaskWoken`): run the bare step.
  This is now *correct by construction* for every caller: timer/subtask wakes
  (as today), and seeded command wakes whose prelude is already durable (D6).
- case 5′ (`ContextPlanComposed` present): the attempt-recovery machine —
  classify (D2) → seal (D3/D4) → re-drive under the same lease, or park (D7).
  `PartialStepOrphan` stops being raised on this path (the exception class
  can remain as an internal signal if convenient, but nothing routes it to
  `fail(retryable=True)` anymore).

The **drained branch** gets the same treatment: folded status `running` with
a `ContextPlanComposed` after the latest turn boundary (`TaskStarted` /
`TaskWoken` / `TaskRewound`) ⇒ same classify → seal → re-drive-or-park,
fixing the silent dirty re-drive.

**D6 — Prelude durability: append-type preludes move into the seed.**
`seed_send_goal` / `seed_answer` write `note_woken` + the prelude events
synchronously on the request thread, before returning; the `202` then means
"your input is durable". `SeededTurn.prelude` becomes `None` on those paths
and the drive runs only `run_one_step` (entering at case 2′). Event order and
bytes are unchanged — only the wall-clock moment of the write moves.
`ResolveApprovalPrelude` stays in the drive (it *executes* the approved tool —
cannot block the request thread); its narrower loss mode is benign and gets
documented in the ADR: a crash before the resolution lands re-suspends the
task on the same approval, and the operator approves again. The
`worker.py:600-609` CONTRACT LIMITATION comment and
`test_case2_crash_after_taskwoken_runs_bare_step_dropping_prelude` are
rewritten to pin the new, narrower contract.

**D7 — Parking: seal first, then rest as a stopped conversation.**
Park = (under the recovery lease) emit `StepAttemptAbandoned` → append one
`origin="system"` notice message stating what was interrupted (tool names +
whether each start had a completion, so the model/human can verify half-applied
effects) → `suspend_on_human_handle` → `release(suspended, wake_on=…,
consumed_wake_event=lease.wake_event on the woken path)`. The parked task is
exactly a stopped conversation: the web UI shows it suspended with the notice;
typing a message resumes it from the clean baseline; `close` / `cancel` work
as-is. **Zero new API verbs, zero new UI forms.** The handle is derived the
same way `_settle_stopped_turn` would (the session's next-goal handle when
available; otherwise a deterministic recovery handle — `send_goal` matches
whatever `wake_on` holds).

**D8 — Repeated crashes: natural recursion + a cap.**
The marker is *not* a wake-window boundary for `_find_matching_woken_index`
(unlike `TaskRewound`), so a crash during a re-drive re-enters case 5′ and
recovery recurses. A per-window abandon cap (count of `StepAttemptAbandoned`
since the last turn boundary; default **3**, mirroring `reclaim_max`) forces a
park regardless of classification, preventing a crash loop from burning LLM
calls forever.

**D9 — Auto re-drive does not notify the model; park does.**
A safe re-drive (reads only) just re-runs — injecting a notice would perturb
every recovered transcript for no benefit. The park path's system notice (D7)
is the only transcript change, and only on new recordings.

## Implementation plan

1. **Protocol + fold.** `StepAttemptAbandonedPayload` (dataclass, canonical
   tag, `register()`); fold handler mirroring `_on_task_rewound`; add the type
   to the baseline set in `storage/sqlite/eventlog.py`,
   `storage/postgres/eventlog.py`, `storage/memory.py`, and
   `_BoundedEventLog.find_latest_snapshot`.
2. **Recovery machine** (`runtime/worker.py`). Extract the attempt scanner
   (window boundary → last `ContextPlanComposed` → paired/unpaired tool calls
   from the tail) as a pure function over the event list. Classifier per D2 —
   needs a "would this call be auto-approved?" predicate resolved from the
   engine's guard/permission config (small seam on the engine or resolver;
   conservative park on any resolution failure). Seal: bounded fold to the
   baseline seq → `to_canonical_bytes` → ContentStore → emit marker under the
   lease. Re-drive: refold (cheap — marker is a baseline) → `run_one_step` →
   normal release discipline (`consumed_wake_event` on the woken path). Park
   per D7. Cap per D8. Wire into case 5′ *and* the drained branch. New
   `ReliabilityEvent` kinds: `attempt_abandoned`, `attempt_parked`.
3. **Seed-time preludes** (`execution/driver.py`). Move `note_woken` + prelude
   application into `seed_send_goal` / `seed_answer`; `SeededTurn` carries
   `prelude=None` there; approval/deny paths unchanged. Update the case-2
   comment + pinned test per D6.
4. **Observability.** OTLP exporter (`observers/otlp.py`): treat
   `StepAttemptAbandoned` like the existing rewind/restart segmentation
   (seq-suffixed segment span for the re-drive) or, minimally, an event
   annotation on the task span — decide in-code, keep the segment-span
   convention. Web trace timeline: render the marker ("attempt abandoned —
   crash recovery").
5. **Docs.** New ADR `docs/adr/step-attempt-recovery.md` (decision +
   rationale + alternatives: dispatcher journal table, whole-window abandon,
   wake-carried command intent — all considered and rejected during shaping;
   note the interaction contract for the round-3b fencing ADR: recovery always
   runs under a lease, so a fencing epoch fences it automatically). Rewrite
   the partial-step-orphan section of `docs/operations/limitations.md`;
   update `docs/concepts/wake-resume.md` if it states the old case-2/case-5
   behavior; add **Attempt** to `CONTEXT.md` vocabulary (one decide→act
   iteration within a Step; starts at `ContextPlanComposed`; the unit of
   crash recovery).
6. **Release.** Patch bump (runtime/sdk/agent lockstep) per
   `docs/releasing.md` after merge.

## Task breakdown

| # | Task | Depends on |
|---|------|-----------|
| T1 | ADR draft (can be written first — decisions above are final) | — |
| T2 | Protocol payload + canonical registration + fold reset handler + baseline-set additions (4 sites) | — |
| T3 | Attempt scanner + classifier (pure functions + approval predicate seam) | — |
| T4 | Recovery machine rework: case 2′/5′, drained branch, seal/re-drive/park, cap, reliability kinds | T2, T3 |
| T5 | Seed-time preludes + case-2 test/comment rewrite | T4 (case 2′ semantics) |
| T6 | OTLP + web trace rendering of the marker | T2 |
| T7 | Docs: limitations.md, CONTEXT.md, ADR finalization | T4, T5 |
| T8 | Test suite (see acceptance) + full-suite/ruff/import-linter verification | T4, T5 |

T1/T2/T3 can run in parallel; T4 is the integration point; T5 and T6 can run
in parallel after their deps.

## Dependencies / sequencing

- No storage-schema migration; no dispatcher contract change ⇒ the storage
  contract tests must pass **unmodified** (that is itself an acceptance
  criterion).
- Round 3a (multi-worker) builds directly on this: recovery under a lease is
  already worker-count-agnostic. Round 3b's fencing ADR must cite the marker +
  lease discipline. Nothing here waits on either.

## Acceptance criteria

1. Crash simulation (kill between event writes, as the existing case tests
   do), woken path, safe window (reads only / plan only) ⇒ task completes with
   one `StepAttemptAbandoned`, no duplicate side-effectful tool execution, no
   human input; completed pre-crash iterations' tool calls are **not**
   re-executed (assert via a counting fake tool).
2. Same, unsafe window (unpaired `shell_run` / completed `edit`) ⇒ task parked:
   suspended on a human handle, system notice message names the interrupted
   calls; typing resumes from the clean baseline; `cancel`/`close` work. Under
   `bypassPermissions` the same window auto-re-drives (maintainer's rule).
3. Opening-turn (drained-path) crash gets identical treatment — the silent
   dirty re-drive is gone (regression test pinning classify-before-step on the
   drained branch).
4. `202`-acked `send_goal`/`answer` survives a crash immediately after seed:
   the user message is durable and the bare re-drive consumes it (the old
   dropping-prelude test is replaced by one pinning the new guarantee, plus
   the benign approval-loss re-suspend case).
5. Crash during re-drive recurses; the 4th consecutive abandon in one window
   parks (cap test).
6. Normal seeded turns (no crash) enter case 2′ and produce byte-identical
   event sequences to today's case-1+prelude path (golden-bytes comparison).
7. Old recordings: full existing test suite (2943+) passes; fold/resume of
   pre-change fixtures byte-identical; storage contract tests unmodified;
   ruff + import-linter (16 contracts) clean.
8. `PartialStepOrphan` no longer reaches `fail(retryable=True)`; the
   silent-terminal path is demonstrably closed (test asserting the task never
   goes terminal without either completion or an explicit human close).
9. ADR merged; limitations.md no longer lists partial-step-orphan as an open
   edge (moves to "recovered automatically / parked" description);
   CONTEXT.md defines Attempt.

## Risks

- **Riskiest area of the repo** (worker/dispatcher recovery). Mitigation: no
  dispatcher change at all; every new behavior is log-derived + lease-guarded;
  the D1–D6 invariants are pinned by existing tests that must stay green.
- Baseline snapshot correctness: bounded fold must reproduce exactly the
  pre-iteration state (reuses the rewind machinery — covered by golden tests).
- Classifier false-safe (a side-effectful tool marked `low`): same trust
  surface as `PermissionGuard` today (a `low` tool already runs unattended);
  documented in the ADR, not a new exposure.
- Seed-time writes lengthen the HTTP request path slightly (two appends +
  one `TaskWoken`); bounded and consistent with what seed already does
  (create/wake/ModelBound/lease). Approval latency unchanged (stays async).
- OTLP segment semantics for abandoned attempts need a deliberate choice to
  avoid confusing traces (T6 decides; both options are backward-compatible).

## Files / areas to inspect

- `packages/noeta-runtime/noeta/runtime/worker.py` — case machine
  (`_run_woken`, `_find_matching_woken_index`), drained branch, WorkerLoop
  exception policy, ReliabilityEvent kinds.
- `packages/noeta-runtime/noeta/core/fold.py` (`_on_task_rewound` at :841),
  `core/snapshot.py`, `core/engine.py` (`_emit_context_plan` :843,
  `suspend_on_human_handle` :1310).
- `packages/noeta-runtime/noeta/execution/driver.py` — `SeededTurn` :356,
  `_BoundedEventLog` :386, `seed_send_goal` / `seed_answer` / `drive_seeded`.
- `packages/noeta-runtime/noeta/protocols/` — canonical registration pattern,
  `events.py` payloads, 4 KB cap.
- `packages/noeta-runtime/noeta/storage/{sqlite,postgres}/eventlog.py`
  (`find_latest_snapshot`), `storage/memory.py`.
- `packages/noeta-runtime/noeta/observers/otlp.py` — segment-span convention.
- Guard/permission surface for the approval predicate:
  `noeta/guards/`, `compile_options` permission_mode mapping.
- Tests: `tests/` — the D4 case tests, the pinned case-2 test, storage
  contract tests, `test_otlp_export.py`.
