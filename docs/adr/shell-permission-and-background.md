# Shell permission model (approve once, record once) + background command execution (a host-layer side effect, not a Subtask)

## Context

This is one coherent decision about "how the shell tool should gate, and how it should avoid blocking," formed from three pieces: the shell permission model (allow through when an allowlist matches, prompt once for confirmation on a miss), background command execution, and the half of the change that loosens shell admission (the description-shape half belongs to tool-and-agent-catalog.md).

All of them reuse existing primitives and add no new runtime primitives: the ContentStore's content offloading (see event-sourced-truth.md), the wake mechanism (see subtask-fanout-and-durable-wake.md), and the origin marker (see event-origin-marker.md).

## Decision

### Shell permission: permission_mode drives the gate + approve once, record once

- **permission_mode now drives the shell gate; the two are no longer orthogonal.** `bypassPermissions` → shell runs `ARBITRARY` with no gate; `default` / `acceptEdits` → also `ARBITRARY` (no longer self-rejecting), gated instead by a **per-call decision closure**: a match against the effective allowlist → silently allow; a miss → `require_approval` (reusing the existing approval suspend / resume chain). The config `shell_mode=off` still removes the tool entirely.
- **"Miss → approval" is implemented via an injected decision closure; the guard still depends only on the protocol.** The closure `(tool_name, arguments) -> whether approval is needed` is built in noeta-sdk (which can import the shell-matching function) and injected as a plain Callable field `PermissionPolicy.conditional_approval`; the guard consumes it after the static `require_approval_tools`.
- **The effective allowlist = built-in defaults + HostConfig + the project file** (`<workspace>/.noeta/shell-allowlist.json`, loaded once at startup, following the workspace and persisting across restarts).
- **"Remember" is a pure external side effect on the project file: no event, no context, no fingerprint.** The `/approvals` endpoint takes an optional `remember`; on approval with `remember=True`, a rule is derived at program (+ first-arg) granularity and appended to the project file (deduplicated, best-effort). **Accepted trade-off**: after the project file changes, resuming an earlier task may have the guard judge differently than at recording time and drift silently — acceptable in a single-tenant context; we won't pin a per-workspace file into per-task durable state for it.
- **`ARBITRARY` is upgraded to full bash; `ALLOWLIST` stays strictly unchanged.** On the SDK-host product path, `ARBITRARY` goes from "any argv but metacharacters forbidden" to real `bash -c` (pipes / redirects / chaining all available); the security boundary shifts from "a wall against argv injection" to the PermissionGuard + the approval predicate. The daemon / CLI default `ALLOWLIST` still rejects metacharacters, structurally matches argv against the allowlist, and runs `subprocess.run(argv)` without going through a shell.

### Background command execution: a host-process registry, not a Subtask

- **A background command is a host-layer side effect, not a Subtask.** A process has no Policy (no decision-maker), and forcing it into "Task = has a Policy" is awkward. Like the LLM gateway, it is a host-layer, non-replayable side effect, so it lives in a **process registry** on the host / runner.
- **Tool surface: `shell_run` gains a `run_in_background` flag + a new `shell_kill`; reads reuse deref, notifications reuse wake.** `poll` / `kill` take a `job_id` rather than a `command` — a different input type from `run`, so they aren't crammed into a single tool's action enum (that would tear the parameter schema and hurt description routing); reads reuse the existing deref rather than building a separate cursor-read tool.
- **Reading + notifying = pull + push (both needed).** The output stream is appended to a **growable ContentStore artifact** (pull); on exit, a boundary event is emitted + `dispatcher.wake` pushes the final ref to the model (push).
- **Kill surface: the model can call `shell_kill(job_id)` (primary), with a human-side emergency stop (secondary).** `shell_kill` goes through the PermissionGuard so it can be gated; kill reuses the cancel cascade.
- **Output accounting: a growable ContentStore artifact + boundary events (`BackgroundShellStarted` / `Exited` / `Killed`) + one `BackgroundShellPolled(ref, offset)` recorded per poll to pin "the snapshot the model saw at that moment"; bytes are never inlined into events.** This is the same offloading a synchronous shell does to the ContentStore, only with "write once" replaced by "write incrementally"; replay reads the artifact and never re-runs the process.
- **Lifetime belongs to the session, lineage to the task.** A background process may outlive the task that started it; its lifecycle is owned by the session, not the launching task. The launching task completes as normal, the process is adopted by the session, and it lives until it exits on its own / is killed / the **session closes** (only close cascades SIGTERM→SIGKILL). The lineage recorded in `Started.spawned_by_task_id` is just a label — it never blocks task completion.
- **Crash recovery (conservative)**: after a host restart, scan for jobs with a Started but no terminal state → emit `BackgroundShellLost`. PID-based kill happens **only when process identity can be verified** (start time + command match); otherwise the job is only marked Lost, not killed — under PID reuse we never risk killing an innocent process.
- **Resource governance**: a per-session concurrency cap on background jobs (default 8, configurable); over the cap is rejected outright (not queued); an output-artifact size cap (default 256KB, tail-truncated), with a `truncated` flag passed through to the model, and truncation kept consistent between replay / deref.
- **Non-goal**: Monitor-style line-by-line streaming output is not done in v1 ("deref the growable artifact anytime to see progress" already covers ~90% of real-time needs).

## Rationale

- **The old shell design bound "does the allowlist match" and "should we ask the human" into two unrelated things** — under `default`, every command prompted for approval (even `git status`), which was both noisy and blunt. The new design lets an allowlist match run silently, prompts once for an unknown command, and remembers after confirmation.
- **"Remember" stays out of the EventLog / context because it is external governance config, unrelated to the model.** On resume, the approval decision is read back from the recorded `ToolCallApprovalResolved` (not re-judged), so the file-write action never enters the resume path; the model also never sees the allowlist. This saves the entire "event + provenance" complexity.
- **Making background commands a host side effect rather than a Subtask**: avoids inventing an "empty Policy / external task" concept, and avoids prematurely prying open the concurrency invariant that fan-out deliberately deferred — a subtask tree has nicer UX, but its cost/benefit ratio is far worse than a host side effect.
- **"Push" is heavier than the draft assumed (mechanism C)**: noeta-agent is request-driven with no resident WorkerLoop, so marking ready via `wake` alone has no thread to lease it; a background command is "fire and forget," and the session isn't suspended waiting on it. Mechanism C reuses next-goal's wake handle to wake the session and injects a notification preamble tagged with a system `origin` (a one-line summary + ContentRef), triggered by a host-side background driver thread at a turn boundary — its durable footprint is byte-identical to a `send_goal` turn, adding no new WakeCondition serialization.
- **Lifetime belongs to the session**: a long-lived service (`npm run dev`) must not lock a task into never completing; a long-running batch job's results must not get killed by accident.

## Alternatives considered

1. **Keep "the tool's own ALLOWLIST rejection + permission approving the whole tool."** Rejected: the granularity doesn't line up (an allowlist match should skip approval), and it can't express "miss → ask" instead of "miss → reject."
2. **Make "remember" an event folded into task state + pin the allowlist into per-task durable state (first draft).** Rejected: the allowlist is unrelated to the EventLog / resume / context; recording it mistakes external config for per-task provenance.
3. **Rewrite argv matching inside the guard / derive a shell-specific guard subclass.** Rejected: duplication + drift / an extra inheritance chain; a closure field is smaller.
4. **Make background commands a Subtask.** Rejected: it needs an "empty Policy / external task" concept + prematurely pries open the concurrency invariant.
5. **A single-tool action enum / a purpose-built cursor-read tool.** Rejected: it tears the parameter schema and hurts description routing / deref already exists.
6. **Background notification as pure push / pure pull.** Rejected: pure push can't show progress mid-run; pure pull misses completion, or wastes turns on empty polling.
7. **One event per output chunk / a host in-memory buffer recorded once at exit.** Rejected: a noisy process blows up the event log / losing intermediate state breaks the replay red line.
8. **Block = a task can't complete while it has a background process / kill on completion.** Rejected: a service scenario stays stuck forever / a batch result is gone before it's collected.

## Consequences

- On the shell-tool side, allowlist matching, rule derivation, and project-file loading/appending (`command_in_allowlist` / `rule_spec_from_command` / `load_project_shell_allowlist` / `append_project_shell_rule`), the `run_in_background` flag, and ARBITRARY full-bash all land in `noeta.tools.fs.shell`; the injected decision used for gating lands in `noeta.guards` as `PermissionPolicy.conditional_approval`.
- The host process registry, the growable ContentStore artifact, conservative PID recovery, and the concurrency cap land in `noeta.runtime.background_shell`; the boundary events `BackgroundShellStarted/Exited/Killed/Polled/Lost` land in `noeta.protocols.events`.
- Mechanism C's wake + notification preamble reuses the wake mechanism of subtask-fanout-and-durable-wake.md and the origin marker of event-origin-marker.md, introducing no new durable shape.
- Accepted drift cost: after the project allowlist file changes, resuming an earlier task may have the guard judge differently than at recording time. This is the simplicity bought by deliberately keeping it out of durable state under single-tenancy.
