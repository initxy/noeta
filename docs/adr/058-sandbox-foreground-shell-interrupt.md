# ADR-058: A foreground shell inside a sandbox container

- Status: accepted
- Date: 2026-09-25

## Context

Refines [ADR-042](042-interrupt-responsiveness.md) (Interrupt reaches the turn boundary promptly: abandonable provider waits, cancel-aware seams, and force-stop as enqueue). Where the two disagree, this record wins.

## Decision

Decision 4 of [ADR-042](042-interrupt-responsiveness.md) covered a foreground shell only where the host owns a process group.
Under a sandbox the command runs in the container, so the kill table had nothing
to signal: interrupt, cancel and close waited out the command, and a timeout was
only the HTTP read timeout while the command kept running in the container. The
same decision now reaches it, without a new `ExecEnv` member:

- **Every container command runs in its own shell session.** The AIO adapter
  creates a session with a fresh id, then execs with `hard_timeout` set to the
  command's budget and `no_change_timeout` above it (the image's default of
  120 s would otherwise cut a quiet build short). The transport read timeout sits
  a few seconds above the budget so a wedged container still answers.
- **The kill table takes a kill callable as well as a `Popen`.**
  `register_foreground(kill=..., spawned_by_task_id=...)`; the cascade marks the
  handle `killed` exactly as before and runs the callable on a daemon thread,
  since it is an HTTP call (`POST /v1/shell/kill {id}`, which ends the session's
  whole process tree). `Bash` registers it when the backend declares the optional
  `supports_foreground_kill` capability and passes `run_argv(on_start=...)`; the
  backend calls `on_start` with the terminator before it blocks.
- **The wait is abandonable, the command is not left behind.** The image keeps an
  exec request open until `hard_timeout` even after a kill, so the adapter waits
  for the exec on a worker thread and returns the moment the kill fires; the
  orphan request has no consumer — the reasoning of ADR-042's decision 1, safe here because the
  command it was waiting for is already dead. The tool reports *interrupted* from
  the `killed` mark, as on the host. A command that hits `hard_timeout` (which by
  itself does not reliably end the process in the image) or the transport timeout
  is killed explicitly and reported as timed out.

A stopped container command returns no partial output; the local one returns
what it printed. Accepted: the stop is the result, and the full output of a long
command belongs in a file anyway.
