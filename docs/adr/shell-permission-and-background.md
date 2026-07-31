# Shell gating is allowlist-or-approve driven by permission mode; a background command is a host process registry, not a Subtask

## Context

The shell tool has to answer two questions that are easy to conflate. First, when should a command run and when should a person be asked — a gate that fires on every `git status` is noise, and a gate that never fires is not a gate. Second, what happens to a command that outlives a turn: a dev server or a long build must not hold a turn open. Both answers reuse the runtime's own primitives — content offloading, the wake mechanism, the origin marker — and add no runtime primitive of their own.

## Decision

### Shell permission: the mode drives the gate, an allowlist decides the call

- **`permission_mode` drives the shell gate.** `bypassPermissions` runs the tool in its arbitrary tier with no per-call gate. `default` and `acceptEdits` also run the arbitrary tier — the tool does not refuse itself — and are gated by a per-call predicate: a command matching the effective allowlist runs silently; anything else, including a malformed command, returns `require_approval` and suspends through the approval chain. A shell mode of `off` removes the tool entirely.
- **The predicate is an injected closure, so the guard depends only on the protocol.** `(tool_name, arguments) -> whether approval is needed` is built in noeta-sdk, which can import the matcher, and passed as the plain `PermissionPolicy.conditional_approval` field; the guard consults it after the static `require_approval_tools` set.
- **The effective allowlist is assembled per turn** from the fs built-in's curated rule table, the host's configured rules, and the project file `<workspace>/.noeta/shell-allowlist.json`, which follows the session's workspace and survives restarts. The kernel owns the matching engine, the rule-spec parsing and the project file; it curates no commands of its own.
- **Remembering an approval is a plain external side effect on that file — no event, no context, no fingerprint.** A rule is derived at program (plus first-argument) granularity and appended, deduplicated. Accepted trade-off: after the file changes, resuming an earlier task may have the guard judge differently than it did at recording time.
- **The arbitrary tier is real bash; the allowlist tier stays strictly structural.** The arbitrary tier runs the raw command through `bash -c`, so pipes, redirection and chaining work, and the security boundary is the guard plus the approval predicate rather than an argv wall. The allowlist tier rejects shell metacharacters before tokenisation, matches the parsed argv structurally against the rules, and runs the argv with no shell at all.

### Background execution: a host process registry

- **A background command is a host-layer effect, not a Subtask.** The spawned process has no Policy, so it has no decision-maker and does not belong in the Task model. It lives in an in-process registry on the host — a runtime accelerator, never persisted; the authoritative record of a job is its event triple in the log.
- **Tool surface:** `shell_run` takes a `run_in_background` flag, `shell_poll` and `shell_kill` take a `job_id`. Output is read back through the content dereference; nothing new is built to read it.
- **Reading and notifying are pull and push, and both are needed.** A watcher thread drains the merged output stream into an off-ledger buffer; whenever the model needs a reference — at spawn, at each poll, at exit — the registry mints a fresh content-addressed snapshot of the buffer's current bytes. The poll event pins the `(ref, offset)` pair, so replay reproduces exactly the prefix the model saw and later output can never bleed into a historical poll. On exit a terminal event is recorded and a host callback drives a wake-and-notify turn at a turn boundary, tagged with a system origin. Bytes are never inlined into events.
- **Lifetime belongs to the session, lineage to the task.** Jobs are keyed by the session's root task, so a job spawned by a subtask survives that subtask and is reaped by the session's close or cancel cascade. The spawning task recorded on the start event is a label; it never blocks that task's completion.
- **Kill is model-first, human-second.** `shell_kill` marks the job and signals its process group — every job leads its own group, so backgrounded grandchildren are reaped too — escalating from SIGTERM to SIGKILL after a grace on a short-lived thread so no engine thread is ever blocked. The watcher performs the actual reap and records exactly one terminal event, even when a kill races a natural exit. A whole session's jobs are killed through the same per-job primitive.
- **Crash recovery is conservative.** After a host restart, streams are scanned for a start event with no terminal; each such job is marked lost unconditionally, which is the durable record. The recorded PID is killed only when an identity probe confirms it — the live process's start time is not newer than the recorded job and its leading program token matches. Any doubt leaves the process alone.
- **Resource governance:** a per-session cap on concurrently running jobs (default 8) rejects an over-cap spawn outright rather than queueing it, before any event is written; the output buffer is capped (default 256 KB) and tail-truncated, with the truncation flag surfaced to the model and recorded so replay and dereference agree.
- **Non-goal:** line-by-line streaming. Dereferencing the latest snapshot at any time covers the real need.

## Rationale

- **Binding "does the allowlist match" to "should we ask a person" is the point.** Kept orthogonal, the gate degenerates: either every command under `default` prompts, including `git status`, or the tool refuses unknown commands itself and there is no way to express "miss → ask" at all.
- **Remembering stays out of the event log and out of the context because it is external governance configuration, unrelated to the model.** On resume the approval outcome is read back from the recorded resolution rather than re-judged, so the file write never enters the resume path, and the model never sees the allowlist. That removes an entire event-and-provenance apparatus for a fact the conversation does not own.
- **A background command as a host effect** avoids inventing an "empty Policy" task and avoids prying open the concurrency invariant that fan-out deliberately keeps closed. A subtask tree would give nicer structure at a much worse cost.
- **Push is required because nothing is waiting.** A background command is fire-and-forget and the session is not suspended on it, so marking it ready has no thread to lease it. Riding the same wake handle an ordinary goal uses makes the completion notice's durable footprint identical to a normal turn, adding no new wake condition to serialize.
- **Snapshot-per-poll rather than one mutable artifact** keeps every reference immutable and content-addressed, which is what makes a historical poll reproducible; the cost is re-hashing a capped buffer at model pace.
- **Lifetime by session** so a long-lived service does not lock a task into never completing, and a batch job's results are not killed the moment its launcher finishes.

## Alternatives considered

1. **Keep the tool's own allowlist rejection and gate the whole tool statically.** Rejected: the granularity does not line up — an allowlist match should skip approval entirely — and a static gate cannot express "miss → ask" instead of "miss → reject".
2. **Fold "remember" into task state as an event, pinning the allowlist into per-task durable state.** Rejected: it mistakes external configuration for per-task provenance, and drags the allowlist into resume and context where it has no business.
3. **Reimplement argv matching inside the guard, or derive a shell-specific guard subclass.** Rejected: duplication that drifts, or an extra inheritance chain. A closure field is smaller than either.
4. **Model a background command as a Subtask.** Rejected: it needs an "empty Policy" concept and prematurely opens the concurrency invariant.
5. **A single tool with an action enum, or a purpose-built cursor-read tool.** Rejected: `run` takes a command while `poll` and `kill` take a job id, so one enum tears the parameter schema and blurs the descriptions the model routes on; and dereference reads output on its own.
6. **Notification as pure push, or pure pull.** Rejected: pure push shows nothing mid-run; pure pull either misses completion or burns turns on empty polling.
7. **One event per output chunk, or a host-memory buffer recorded once at exit.** Rejected: a chatty process floods the event log; recording only at exit loses the intermediate state replay depends on.
8. **Block task completion while a background job lives, or kill jobs when the launching task completes.** Rejected: a service scenario stays stuck forever, and a batch result is destroyed before anyone collects it.
9. **Kill every orphan by its recorded PID after a restart.** Rejected: PID reuse turns that into an irreversible mistake against an unrelated process.

## Consequences

- `noeta.runtime.shell_policy` owns the shell modes, the matching engine, rule derivation and the project rules file; the curated rule table and the tool itself ship with the `fs` built-in; `noeta.runtime.governance` carries the injected approval predicate on the permission policy.
- `noeta.runtime.background_shell` owns the process registry, the watcher, the snapshot minting, the conservative recovery and the caps; the background lifecycle payloads live in `noeta.protocols.events`.
- Orphan recovery is a startup side effect: a resume that folds the log builds no registry, so neither the scan nor the PID kill can re-run.
- Background execution is host-side only — a sandboxed execution backend refuses `run_in_background` rather than pretending to support it.
- After the project rules file changes, resuming an earlier task may have the guard judge differently than at recording time — the accepted cost of keeping the file out of durable state.
