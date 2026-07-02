"""Background-shell process registry.

A live, in-process table of background processes launched by
``shell_run(background=true)``. A background command is a **host-layer
effect**, not a subtask: the spawned process has no Policy,
so it does not belong in the Task model. The registry owns the ``Popen``
handle, an off-ledger byte buffer per job, and a daemon watcher thread that
incrementally drains the pipes into that buffer.

This is a RUNTIME accelerator only — exactly the same property as
:class:`noeta.runtime.cancellation.CancellationRegistry`. The authoritative
record of a background job is the ``BackgroundShell*`` event triple in the
log; the registry just holds the live process state so a poll is O(1) and
the watcher can reap + record the exit. It is never persisted, so it has zero
effect on the recorded record.

Mechanism A — put-per-poll snapshots (core mechanism): the watcher attic
into ``buffer`` off-ledger; whenever the model needs a ref (spawn = empty,
each poll, exit) the registry calls ``content_store.put(bytes(buffer))`` to
mint a fresh, immutable, content-addressed snapshot. ``BackgroundShellPolled``
pins ``(ref, offset)`` so resume reproduces exactly the prefix the model saw —
the process's later output can never bleed into a historical poll. Zero
ContentStore protocol change; the cost is re-hashing the buffer per poll
(poll is model-paced, and ``output_cap`` tail-truncates the buffer).

Thread-safe: the spawn arrives on the engine's drive thread while the watcher
runs on its own daemon thread, and a poll may arrive on a third (an HTTP
handler), so a :class:`threading.Lock` guards the table and each buffer.
"""

from __future__ import annotations

import enum
import os
import signal
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from noeta.core.fold import fold
from noeta.protocols.content_store import ContentStore
from noeta.protocols.dispatcher import Dispatcher
from noeta.protocols.event_log import EventLogReader, EventLogWriter
from noeta.protocols.events import (
    BackgroundShellExitedPayload,
    BackgroundShellKilledPayload,
    BackgroundShellLostPayload,
    BackgroundShellPolledPayload,
    BackgroundShellStartedPayload,
)
from noeta.protocols.values import ContentRef
from noeta.runtime._proc_group import send_group_signal


#: The host completion-push callback. The watcher calls it
#: once, AFTER the durable ``BackgroundShellExited``, with
#: ``(session_id, job_id, summary, final_ref)``. The host hands it off to its
#: background-drive thread (it MUST NOT block the watcher) to inject the
#: completion notice at a turn boundary (Mechanism C). Default ``None`` keeps
#: the issue-01 registry byte-identical (no push).
BackgroundExitCallback = Callable[[str, str, str, ContentRef], None]


class ProbeResult(enum.Enum):
    """The verdict of a process-identity probe (recovery).

    Conservative by construction — the recovery PID-kill only fires on
    :attr:`CONFIRMED`. A killed process is irreversible, so when in doubt we do
    NOT kill (PID reuse hazard: the orphan's PID may have been recycled onto an
    innocent process).
    """

    #: The recorded PID is not alive — nothing to kill (only the Lost mark).
    DEAD = "dead"
    #: A live process holds the PID AND its identity matches the recorded job
    #: (start time not newer than the recorded job AND/OR command matches) — it
    #: is (very probably) the orphan, so a best-effort kill is attempted.
    CONFIRMED = "confirmed"
    #: A live process holds the PID but its identity CANNOT be confirmed (a
    #: different command, or a start time newer than the recorded job → PID
    #: reuse). NEVER kill — only the Lost mark stands.
    UNCONFIRMED = "unconfirmed"


#: A testable / injectable process-identity probe. Given the recorded ``pid``,
#: ``command`` and the job's ``started_at`` (the ``BackgroundShellStarted``
#: envelope ``occurred_at``), it returns a :class:`ProbeResult`. The default
#: implementation (:func:`_posix_identity_probe`) shells out to ``ps``; tests
#: inject a fake so they do not depend on a real process table.
IdentityProbe = Callable[..., ProbeResult]

#: Best-effort PID killer (injectable for tests). The default
#: :func:`_posix_kill_pid` sends SIGTERM→SIGKILL via ``os.kill``; tests inject a
#: recorder so they can assert the never-kill-on-doubt behaviour without
#: touching a real process.
PidKiller = Callable[[int], None]


__all__ = [
    "DEFAULT_BACKGROUND_OUTPUT_CAP",
    "DEFAULT_KILL_GRACE_S",
    "DEFAULT_MAX_BACKGROUND_JOBS_PER_SESSION",
    "BackgroundExitCallback",
    "IdentityProbe",
    "PidKiller",
    "ProbeResult",
    "ProcessRegistry",
]


#: Cap on the off-ledger buffer (combined stdout+stderr) per job. Mirrors the
#: sync shell path's per-stream cap (``DEFAULT_SHELL_OUTPUT_CAP``) — the tail
#: is what survives so a chatty long-running process keeps its most recent
#: output. v1 hard ceiling; rotation / per-job override is backlog (06/07).
DEFAULT_BACKGROUND_OUTPUT_CAP = 256 * 1024  # 256 KB

#: Per-session concurrency cap. ``spawn`` counts the
#: session root's currently-RUNNING jobs and REJECTS the next one over this
#: ceiling (it does NOT queue — a clear "kill one first" refusal is more direct
#: for an agent than an invisible wait). v1 default; ``HostConfig`` overrides it.
#: A runtime accelerator like the rest of the registry: a rejected spawn writes
#: no event, so it is invisible to resume (the count is recomputed from the
#: live table, never the log).
DEFAULT_MAX_BACKGROUND_JOBS_PER_SESSION = 8

#: Grace between SIGTERM and the escalation to SIGKILL (kill). Long
#: enough for a well-behaved process to flush + exit on SIGTERM, short enough
#: that a human emergency-stop feels prompt. v1 default; ``kill(grace_s=...)``
#: overrides it (tests use a tight grace).
DEFAULT_KILL_GRACE_S = 5.0

#: The watcher reads pipes in this many bytes per chunk. Small enough that a
#: mid-run poll catches a partial prefix promptly.
_READ_CHUNK = 4096

#: Bound on the ``parent_task_id`` chain walk (root resolution). A
#: real lineage is shallow (the workspace-and-session model caps subtask depth well under this); the
#: bound just guarantees termination against a degenerate / cyclic stream.
_MAX_CHAIN_DEPTH = 64

_SNAPSHOT_MEDIA_TYPE = "text/plain"

#: ``actor`` label on the system-emitted events — names the writer's identity
#: (orthogonal to ``origin="observer"``, the role the suspend-window keys on:
#: these are tagged observer so a suspend-window exit re-injects).
_ACTOR = "background-shell"


@dataclass
class _JobHandle:
    """Live state for one background job (off-ledger, never serialized)."""

    job_id: str
    popen: subprocess.Popen[bytes]
    command: str
    #: Lifetime OWNER = the session root task. Events
    #: ride this stream; the registry keys ``_jobs`` by it; the close / cancel
    #: cascade kills by it. For a root-spawned job this equals
    #: ``spawned_by_task_id`` (the common case → byte-identical to 01–03).
    session_root: str
    #: LINEAGE only (a label, never a wake_on blocker): the task
    #: that actually ran ``shell_run(background)``. Recorded in the
    #: ``BackgroundShellStarted`` payload so the frontend can show "dev started
    #: by main" even when a subtask spawned it.
    spawned_by_task_id: str
    trace_id: str
    output_cap: int
    #: Combined stdout+stderr captured so far (tail-truncated to ``output_cap``).
    buffer: bytearray = field(default_factory=bytearray)
    #: Set True (ONCE) by the watcher the first time the
    #: buffer overflows ``output_cap`` and the head is dropped. Surfaced on
    #: ``poll`` + the ``Polled`` / ``Exited`` payloads so the model knows the
    #: snapshot is the tail, not the full output. Read under ``lock``.
    truncated: bool = False
    status: str = "running"  # "running" | "exited" | "killed"
    exit_code: Optional[int] = None
    watcher: Optional[threading.Thread] = None
    #: Per-job lock guarding ``buffer`` / ``status`` / ``exit_code`` / the
    #: ``killed`` marker so the watcher's append never tears against a
    #: concurrent poll snapshot AND the reap path sees a consistent kill marker.
    lock: threading.Lock = field(default_factory=threading.Lock)
    #: Exit-event dedup (02/03 reuse this when kill + natural exit race) — set
    #: ONCE under ``lock`` in the watcher's reap so exactly one terminal event
    #: + exactly one completion push fire even if ``kill`` and the natural exit
    #: race.
    notified: bool = False
    #: issue 03 — kill marker set by :meth:`ProcessRegistry.kill` under ``lock``.
    #: The watcher's reap reads it to record ``BackgroundShellKilled`` (with
    #: ``kill_signal``) instead of ``BackgroundShellExited``. ``kill_signal`` is
    #: the signal that ACTUALLY reaped the process (SIGTERM if the grace request
    #: sufficed, SIGKILL after escalation).
    killed: bool = False
    kill_signal: int = int(signal.SIGTERM)


@dataclass(frozen=True)
class _Orphan:
    """An orphan job recovered from the durable log (issue 06).

    Reconstructed from a ``BackgroundShellStarted`` with no later terminal — the
    crash-recovery scan reads it straight from the event log (the registry is
    empty after a restart). ``started_at`` is the Started envelope's
    ``occurred_at`` (the identity probe compares it against the live process's
    start time to defend against PID reuse)."""

    job_id: str
    session_root: str
    pid: int
    command: str
    started_at: float
    trace_id: str


class ProcessRegistry:
    """Thread-safe table of background ``Popen`` jobs + their watchers."""

    def __init__(
        self,
        *,
        event_log: EventLogWriter,
        content_store: ContentStore,
        output_cap: int = DEFAULT_BACKGROUND_OUTPUT_CAP,
        max_jobs_per_session: int = DEFAULT_MAX_BACKGROUND_JOBS_PER_SESSION,
        dispatcher: Optional[Dispatcher] = None,
        on_background_exit: Optional[BackgroundExitCallback] = None,
        identity_probe: Optional[IdentityProbe] = None,
        kill_pid: Optional[PidKiller] = None,
    ) -> None:
        self._event_log = event_log
        self._content_store = content_store
        self._output_cap = output_cap
        # Crash-recovery seams. The identity probe + PID
        # killer are injectable so :meth:`recover_orphans` is testable without a
        # real process table; the defaults are the conservative POSIX ``ps`` /
        # ``os.kill`` implementations. Both are runtime-only (a resume that
        # folds the log never invokes them).
        self._identity_probe = identity_probe or _posix_identity_probe
        self._kill_pid = kill_pid or _posix_kill_pid
        # Per-session RUNNING-job ceiling (reject over it).
        self._max_jobs_per_session = max_jobs_per_session
        # Mechanism C completion push. The dispatcher is the
        # wake seam (kept for issue 03's kill push + symmetry with the
        # cancellation registry); ``on_background_exit`` is the host hook the
        # watcher fires once after the durable Exited so the host's drive thread
        # injects the next-goal notice. Both default ``None`` ⇒ no push (the
        # issue-01 registry byte-identical; a resume that folds the log wires
        # neither, so it stays inert there too).
        self._dispatcher = dispatcher
        self._on_background_exit = on_background_exit
        self._lock = threading.Lock()
        # Keyed by the SESSION ROOT task (not the
        # spawning task): a subtask-spawned job is owned by the session, so it
        # survives the subtask's terminal and the close / cancel cascade reaps
        # it by the root. In the common case (spawner == root) this is the same
        # key as issue 01, so the spawn + event stream are byte-identical.
        self._jobs: dict[str, dict[str, _JobHandle]] = {}

    # -- spawn -------------------------------------------------------------

    def spawn(
        self,
        *,
        argv: list[str],
        cwd: Path,
        env: dict[str, str],
        command: str,
        spawned_by_task_id: str,
        trace_id: str,
    ) -> dict[str, Any]:
        """Launch ``argv`` detached, start its watcher, record ``Started``.

        Returns ``{job_id, ref}`` immediately — the engine main loop is never
        blocked on the process. ``argv`` is a list (never ``shell=True``); the
        caller (``ShellRunTool``) has already run the argv-only / metachar /
        allowlist validation, so the registry just spawns.

        If the session already has
        ``max_jobs_per_session`` RUNNING jobs, the spawn is REJECTED (not
        queued): returns ``{"rejected": True, "reason": ...}`` and records NO
        ``BackgroundShellStarted`` event / starts NO process. The caller
        (``ShellRunTool``) surfaces the reason as a clean tool failure.
        """
        job_id = f"bg-{uuid.uuid4().hex[:12]}"
        # Resolve the SESSION ROOT from the spawning task: the job
        # is owned by the session, not by the (possibly sub-)task that ran the
        # command. Common case (root spawner) → root == spawner → byte-identical.
        session_root = self._resolve_session_root(spawned_by_task_id)
        # Refuse over the per-session concurrency cap BEFORE
        # any event. Count only RUNNING jobs so a session that has killed /
        # drained earlier jobs regains its budget. The count-check AND the table
        # insert MUST happen under a SINGLE lock hold, else two concurrent spawns
        # on one session could both pass the ceiling and overrun to cap+1 (the
        # gap matters the moment per-session parallelism is introduced). A
        # rejected spawn never touches the OS or the log, so it is invisible to
        # resume (the count is recomputed from the live table, never the log).
        popen = subprocess.Popen(
            argv,
            cwd=str(cwd),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,  # merge so one buffer = the model's view
            # tools m4: the job leads its own process group so kill reaps
            # backgrounded grandchildren too (see ``_terminate``'s group kill).
            start_new_session=True,
        )
        handle = _JobHandle(
            job_id=job_id,
            popen=popen,
            command=command,
            session_root=session_root,
            spawned_by_task_id=spawned_by_task_id,
            trace_id=trace_id,
            output_cap=self._output_cap,
        )
        with self._lock:
            running = sum(
                1
                for h in self._jobs.get(session_root, {}).values()
                if h.status == "running"
            )
            if running >= self._max_jobs_per_session:
                # Over the ceiling — reject. The process was just spawned to
                # avoid holding the lock across a (slow) Popen, so kill it here
                # before returning; no event was emitted, so resume never sees
                # this short-lived process.
                popen.kill()
                try:  # reap so the throwaway process never lingers as a zombie
                    popen.wait(timeout=DEFAULT_KILL_GRACE_S)
                except (subprocess.TimeoutExpired, OSError):
                    pass
                return {
                    "rejected": True,
                    "reason": (
                        f"too many background jobs "
                        f"({running}/{self._max_jobs_per_session}); kill one with "
                        f"shell_kill first"
                    ),
                }
            self._jobs.setdefault(session_root, {})[job_id] = handle

        # An empty content-addressed snapshot — every later snapshot grows
        # from it (Started carries a ref, not bytes).
        ref = self._snapshot(handle)
        # Emit on the SESSION ROOT stream so issue 05's per-session read model
        # lists every job of the session; the payload's ``spawned_by_task_id``
        # keeps the real spawner for lineage (AC: blood-line correct).
        self._event_log.system_emit(
            task_id=session_root,
            type="BackgroundShellStarted",
            payload=BackgroundShellStartedPayload(
                job_id=job_id,
                command=command,
                spawned_by_task_id=spawned_by_task_id,
                pid=popen.pid,
                ref=ref,
            ),
            actor=_ACTOR,
            origin="observer",
            trace_id=trace_id,
        )

        watcher = threading.Thread(
            target=self._watch,
            args=(handle,),
            name=f"bg-watch-{job_id}",
            daemon=True,
        )
        handle.watcher = watcher
        watcher.start()
        return {"job_id": job_id, "ref": _ref_json(ref)}

    # -- poll --------------------------------------------------------------

    def poll(self, job_id: str) -> dict[str, Any]:
        """Snapshot the current buffer, record ``Polled(ref, offset)``.

        Returns ``{status, ref, offset}`` (+ ``exit_code`` once exited). The
        snapshot pins the exact prefix the model sees right now so resume can
        reproduce it."""
        handle = self._find(job_id)
        if handle is None:
            return {"status": "unknown", "ref": None, "offset": 0}
        ref = self._snapshot(handle)
        with handle.lock:
            truncated = handle.truncated
            # Snapshot status + exit_code together under the lock so a watcher
            # reap landing mid-poll can never yield an inconsistent pair (a
            # 'running' job that already carries an exit_code). The reap writes
            # exit_code before flipping status, both under this same lock.
            status = handle.status
            exit_code = handle.exit_code
        self._event_log.system_emit(
            task_id=handle.session_root,
            type="BackgroundShellPolled",
            payload=BackgroundShellPolledPayload(
                job_id=job_id,
                ref=ref,
                offset=ref.size,
                # issue 07 — record True ONLY when truncated so an un-truncated
                # poll stays byte-identical to a pre-07 recording (omit-None).
                truncated=True if truncated else None,
            ),
            actor=_ACTOR,
            origin="observer",
            trace_id=handle.trace_id,
        )
        out: dict[str, Any] = {
            "status": status,
            "ref": _ref_json(ref),
            "offset": ref.size,
            # issue 07 — surface to the model so it knows the snapshot is the
            # tail (the tool result echoes this; plain bool, not omit-None).
            "truncated": truncated,
        }
        if status in ("exited", "killed"):
            out["exit_code"] = exit_code
        return out

    # -- kill --------------------------------------------------------------

    def kill(self, job_id: str, *, grace_s: float = DEFAULT_KILL_GRACE_S) -> dict[str, Any]:
        """SIGTERM the job, then SIGKILL after ``grace_s`` if still alive.

        Returns PROMPTLY ``{job_id, status}`` — the grace wait +
        the SIGKILL escalation run on a short-lived daemon thread so the
        engine / tool-invoke thread is never blocked. The actual reap + the
        terminal ``BackgroundShellKilled`` event come from the watcher (already
        blocked on ``popen.wait()``, it unblocks when the process dies), so
        ``kill`` only marks the handle ``killed`` and sends the signal.

        Idempotent / safe: an unknown ``job_id`` → ``{status:"unknown"}``; an
        already-terminal job → its current status (the natural reap won — its
        ``Exited`` stands, no Killed is forced). The ``killed`` marker is set
        under the per-job lock so the watcher's reap reads a consistent value
        when choosing Killed vs Exited."""
        handle = self._find(job_id)
        if handle is None:
            return {"job_id": job_id, "status": "unknown"}
        with handle.lock:
            if handle.notified:
                # The watcher already reaped (natural exit or an earlier kill);
                # do not force a Killed over a recorded Exited.
                return {"job_id": job_id, "status": handle.status}
            already_killing = handle.killed
            handle.killed = True
            handle.kill_signal = int(signal.SIGTERM)
        # Send SIGTERM (best-effort; the process may have just died — the
        # watcher's wait() reaps it regardless). Only the FIRST kill sends +
        # arms the escalation timer; a redundant kill is a clean no-op.
        if not already_killing:
            self._terminate(handle, signal.SIGTERM)
            timer = threading.Thread(
                target=self._escalate_after_grace,
                args=(handle, grace_s),
                name=f"bg-kill-{job_id}",
                daemon=True,
            )
            timer.start()
        return {"job_id": job_id, "status": "killing"}

    def kill_session(self, session_root_task_id: str) -> list[dict[str, Any]]:
        """Human emergency-stop — kill ALL background jobs of one session.

        "A human can emergency-stop one or all
        background jobs via the kill-switch / cancel cascade." Reuses the per-job :meth:`kill` primitive (so issue 04's
        session-CLOSE cascade reuses the SAME primitive — it just calls this).
        Returns the per-job kill results. Unknown / empty session → ``[]``.
        Jobs are keyed by the SESSION ROOT (issue 04), so this kills every job
        of the session — including ones a subtask spawned."""
        with self._lock:
            job_ids = list(self._jobs.get(session_root_task_id, {}).keys())
        return [self.kill(job_id) for job_id in job_ids]

    def purge_session(self, session_root_task_id: str) -> None:
        """Drop all retained job handles for a session — the memory-reclaim
        seam for a *permanently deleted* conversation.

        A ``_JobHandle`` (with its tail buffer, up to ``output_cap``) is kept
        resident after a job exits so a late ``poll`` can still report the job's
        terminal status + final output — that is why ``kill_session`` / close
        deliberately do NOT drop handles (a closed conversation is reopenable
        and inspectable). But once a conversation is *deleted*, its jobs are
        gone for good and never polled again, so retaining their handles is a
        pure leak: over a long server uptime the map grew one entry per job
        forever. The delete path calls this to reclaim them. Idempotent; kills
        nothing (delete follows close/cancel, which already reaped the OS
        processes)."""
        with self._lock:
            self._jobs.pop(session_root_task_id, None)

    # -- crash recovery / orphan reaping (issue 06) ------------------------

    def recover_orphans(
        self, *, probe: Optional[IdentityProbe] = None
    ) -> list[str]:
        """Reap orphan background jobs left by a host crash/restart (issue 06).

        A host restart loses this in-memory registry; the OS processes it
        spawned were reparented to ``init`` (orphans), and the event log holds
        their ``BackgroundShellStarted`` with NO later terminal
        (``Exited`` / ``Killed`` / ``Lost``) on the session-root stream. This
        scans every persisted stream and, for each orphan:

        1. **MANDATORY** — emits ``BackgroundShellLost(job_id)`` (observer-origin)
           on the job's session-root stream so the read model / model stop
           showing it as forever-"running". This stands regardless of the PID
           outcome — the Lost mark is the durable record.
        2. **CONSERVATIVE best-effort PID kill** — from the recorded ``pid`` it
           runs the (injectable) identity ``probe``. The orphan is killed ONLY
           when the probe returns :attr:`ProbeResult.CONFIRMED` (a live process
           still holds the PID AND its identity matches the recorded job). A
           dead PID → nothing to kill. A live but UNCONFIRMED PID → **NO kill**:
           the PID may have been recycled onto an innocent process, and killing
           it is irreversible, so when in doubt we never kill (PID reuse hazard).

        Runs ONCE at live host startup (the SSE product wires it in
        ``build_code_server``). It is a startup SIDE EFFECT, never re-derived
        from the log — the ``BackgroundShellLost`` events it emits are the
        durable record; a resume that folds the log constructs no registry, so
        the scan + PID kill never re-run. An idempotent second pass is a clean
        no-op (a Lost is itself a terminal, so a recovered job is no longer an
        orphan).

        Returns the list of ``job_id``s newly marked Lost (for logging / tests).
        Requires the ``event_log`` to also satisfy
        :class:`~noeta.protocols.event_log.EventLogTaskIndex` (the live InMemory /
        Sqlite adapters do); an event log without the enumeration capability
        (a non-storage test double) silently recovers nothing — exactly
        the right behaviour, since recovery must never run on those paths.
        """
        index = self._event_log
        # The enumeration capability (EventLogTaskIndex) is a structural seam:
        # only genuine storage-backed logs (live InMemory / Sqlite) implement
        # ``list_task_streams``. A log without it (a non-storage test double)
        # recovers nothing — exactly right, recovery must never run there.
        if not hasattr(index, "list_task_streams"):
            return []
        active_probe = probe or self._identity_probe
        recovered: list[str] = []
        for summary in index.list_task_streams():  # type: ignore[attr-defined]
            for orphan in self._scan_orphans(summary.task_id):
                self._mark_lost(orphan)
                self._best_effort_kill(orphan, active_probe)
                recovered.append(orphan.job_id)
        return recovered

    def _scan_orphans(self, task_id: str) -> list[_Orphan]:
        """Return the jobs on ``task_id`` with a Started but no terminal.

        A job is an orphan iff its ``BackgroundShellStarted`` has no later
        ``Exited`` / ``Killed`` / ``Lost`` on the SAME (session-root) stream —
        the lifetime owner stream issue 04 emits every lifecycle event on. Pure
        read of the durable log; no registry / process state involved (the
        registry is empty after a restart, which is exactly why we read the
        log)."""
        started: dict[str, _Orphan] = {}
        terminal: set[str] = set()
        for env in self._event_log.read(task_id):
            if env.type == "BackgroundShellStarted":
                started[env.payload.job_id] = _Orphan(
                    job_id=env.payload.job_id,
                    session_root=task_id,
                    pid=env.payload.pid,
                    command=env.payload.command,
                    started_at=env.occurred_at,
                    trace_id=env.trace_id,
                )
            elif env.type in (
                "BackgroundShellExited",
                "BackgroundShellKilled",
                "BackgroundShellLost",
            ):
                terminal.add(env.payload.job_id)
        return [o for jid, o in started.items() if jid not in terminal]

    def _mark_lost(self, orphan: _Orphan) -> None:
        """Emit the MANDATORY ``BackgroundShellLost`` on the orphan's stream."""
        self._event_log.system_emit(
            task_id=orphan.session_root,
            type="BackgroundShellLost",
            payload=BackgroundShellLostPayload(job_id=orphan.job_id),
            actor=_ACTOR,
            origin="observer",
            trace_id=orphan.trace_id,
        )

    def _best_effort_kill(self, orphan: _Orphan, probe: IdentityProbe) -> None:
        """CONSERVATIVE PID kill — fire ONLY on a CONFIRMED identity.

        The PID may have been recycled onto an innocent process since the host
        crashed; killing it is irreversible, so the bar to kill is high: the
        probe must positively confirm the live PID is the original job. A dead
        PID needs no kill; an UNCONFIRMED live PID is left strictly alone. The
        probe failing (e.g. ``ps`` unavailable) is treated as "cannot confirm"
        → no kill (it raises out of the probe; we never reach the kill)."""
        verdict = probe(
            pid=orphan.pid, command=orphan.command, started_at=orphan.started_at
        )
        if verdict is ProbeResult.CONFIRMED:
            self._kill_pid(orphan.pid)

    def _terminate(self, handle: _JobHandle, sig: int) -> None:
        """Send ``sig`` to the job's whole process GROUP, swallowing a
        dead-process race.

        The job was spawned with ``start_new_session=True`` (it leads its own
        group), so signalling the group reaps backgrounded grandchildren
        (``bash -c "server & wait"``) a single-PID signal would orphan
        (tools m4). Falls back to the single PID if the group is already
        gone. The process may have exited between the watcher's reap and
        this call (``ProcessLookupError``) or never started cleanly — either
        way the terminal event is the watcher's job, so a failed signal is
        benign."""
        # Pid-reuse guard: once the watcher has reaped the child (returncode
        # set) its pid may be recycled onto an unrelated process, and
        # ``os.getpgid(pid)`` would then resolve a stranger's group for us to
        # ``killpg``. ``Popen.send_signal`` skips a signal on a set returncode
        # for exactly this reason; mirror it before the raw-pid group kill.
        if handle.popen.returncode is not None:
            return
        # Group-first, single-PID fallback, swallowing the exit race — the
        # shared primitive every kill path routes through.
        send_group_signal(handle.popen.pid, sig)

    def _escalate_after_grace(self, handle: _JobHandle, grace_s: float) -> None:
        """Daemon thread: wait ``grace_s``, then SIGKILL if still alive.

        Polls the popen with a bounded wait; if the process is still running
        after the grace it escalates to SIGKILL and records that as the reaping
        signal (under ``lock``, so the watcher's reap reads SIGKILL). Never
        blocks the engine — runs entirely off the ``kill`` caller's thread."""
        try:
            handle.popen.wait(timeout=grace_s)
            return  # SIGTERM (or a natural exit) reaped it within the grace.
        except subprocess.TimeoutExpired:
            pass
        with handle.lock:
            if handle.notified:
                return  # the watcher already reaped it during the grace
            handle.kill_signal = int(signal.SIGKILL)
        self._terminate(handle, signal.SIGKILL)

    # -- watcher (daemon thread) -------------------------------------------

    def _watch(self, handle: _JobHandle) -> None:
        """Drain the merged pipe into the buffer, then reap + record exit."""
        stdout = handle.popen.stdout
        if stdout is not None:
            while True:
                # ``read1`` (not ``read``): return whatever bytes are available
                # from one underlying pipe read, WITHOUT waiting to fill the
                # whole ``_READ_CHUNK``. A plain ``read(_READ_CHUNK)`` blocks
                # until 4096 bytes accumulate OR EOF, so a process that dribbles
                # < 4096 bytes slowly (a dev server, a build printing progress)
                # would show NOTHING on a mid-run poll until it exits — defeating
                # the D3 "deref to watch progress (pull)" half. ``read1`` makes
                # the off-ledger buffer grow incrementally as output arrives.
                chunk = stdout.read1(_READ_CHUNK)
                if not chunk:
                    break
                with handle.lock:
                    handle.buffer.extend(chunk)
                    if len(handle.buffer) > handle.output_cap:
                        # Tail-truncate (keep the most recent output), mirroring
                        # the sync path's ``cap_stream``. issue 07 — latch the
                        # ``truncated`` flag so poll / Exited tell the model the
                        # snapshot is the tail (oldest output dropped).
                        del handle.buffer[: len(handle.buffer) - handle.output_cap]
                        handle.truncated = True
            stdout.close()
        exit_code = handle.popen.wait()
        with handle.lock:
            handle.exit_code = exit_code
            # A killed job reaches terminal too — but it records
            # ``BackgroundShellKilled`` (NOT ``Exited``); ``status`` reflects
            # which terminal it is so a late poll reports the right one.
            killed = handle.killed
            handle.status = "killed" if killed else "exited"
            # Exactly-once: the FIRST thread to reach the reap under ``lock``
            # wins; ``notified`` then forbids a second terminal event AND a
            # second push, so a kill racing a near-simultaneous natural exit
            # still records ONE terminal event + ONE push.
            if handle.notified:
                return
            handle.notified = True
            kill_signal = handle.kill_signal
            truncated = handle.truncated  # issue 07 — surfaced on Exited
        final_ref = self._snapshot(handle)
        summary = _exit_summary(handle.command, exit_code, final_ref.size, killed=killed)
        if killed:
            # issue 03 — the terminal event for a killed job. One terminal event
            # per job: a killed job records Killed, never also Exited. issue 04
            # — emitted on the SESSION ROOT stream (lifetime owner).
            self._event_log.system_emit(
                task_id=handle.session_root,
                type="BackgroundShellKilled",
                payload=BackgroundShellKilledPayload(
                    job_id=handle.job_id, signal=kill_signal
                ),
                actor=_ACTOR,
                origin="observer",
                trace_id=handle.trace_id,
            )
        else:
            self._event_log.system_emit(
                task_id=handle.session_root,
                type="BackgroundShellExited",
                payload=BackgroundShellExitedPayload(
                    job_id=handle.job_id,
                    exit_code=exit_code,
                    final_ref=final_ref,
                    summary=summary,
                    # issue 07 — record True ONLY when the buffer overflowed
                    # output_cap; un-truncated exits omit it (byte-identical to
                    # a pre-07 recording).
                    truncated=True if truncated else None,
                ),
                actor=_ACTOR,
                origin="observer",
                trace_id=handle.trace_id,
            )
        # Mechanism C: after the durable Exited, hand the
        # completion to the host so it drives a wake-and-notify turn. The
        # ``notified`` dedup above guards exactly-once (kill (03) + natural exit
        # cannot both push). The callback MUST NOT block the watcher — the host
        # contract is to enqueue the drive onto its own background-drive thread.
        # issue 04 — the first arg is the SESSION ROOT (the stream the host
        # suspends on / drives ``notify_background_exit`` against), so a
        # subtask-spawned job wakes the ROOT session, never a dead subtask.
        if self._on_background_exit is not None:
            self._on_background_exit(
                handle.session_root, handle.job_id, summary, final_ref
            )

    # -- helpers -----------------------------------------------------------

    def _resolve_session_root(self, task_id: str) -> str:
        """Walk the ``parent_task_id`` chain to the session root.

        The canonical lineage edge is ``fold(...).parent_task_id`` (the same one
        ``read_models/sessions`` reads from ``TaskCreated.parent_task_id``); a
        root conversation folds to ``parent_task_id is None``. We follow it to
        the top so a subtask-spawned job is owned by the SESSION, not the
        subtask. The registry is a runtime accelerator and ``fold`` is a pure
        read of the same log — no new chain-walk primitive is invented.

        Defensive: a stream with no ``TaskCreated`` genesis (a degenerate /
        synthetic task_id, as in the registry unit tests, or a stream whose
        first event is a background / tool event) has no resolvable parent, so
        the spawner is treated as its OWN root — the common-case, byte-identical
        path. A cycle / unbounded chain is bounded by ``_MAX_CHAIN_DEPTH`` (never
        seen in practice; the workspace-and-session model caps subtask depth) and falls back to the
        last id reached rather than spinning."""
        reader: EventLogReader = self._event_log  # InMemoryEventLog is both
        current = task_id
        for _ in range(_MAX_CHAIN_DEPTH):
            try:
                parent = fold(reader, self._content_store, current).parent_task_id
            except (ValueError, KeyError):
                # No genesis to bootstrap from → not a foldable task stream;
                # treat ``current`` as the root (it owns its own lifetime).
                return current
            if parent is None:
                return current
            current = parent
        return current

    def _snapshot(self, handle: _JobHandle) -> ContentRef:
        """Mint a fresh content-addressed ref for the buffer's current bytes."""
        with handle.lock:
            body = bytes(handle.buffer)
        return self._content_store.put(body, media_type=_SNAPSHOT_MEDIA_TYPE)

    def _find(self, job_id: str) -> Optional[_JobHandle]:
        with self._lock:
            for jobs in self._jobs.values():
                handle = jobs.get(job_id)
                if handle is not None:
                    return handle
        return None


def _ref_json(ref: ContentRef) -> dict[str, Any]:
    """JSON-native ``ContentRef`` form for the tool ``output`` dict.

    The same shape ``noeta.tools._refs.ref_json`` produces; spelled locally so
    noeta-runtime never imports a noeta-sdk (``noeta.tools``) helper (the
    import-linter layering forbids it)."""
    return {"hash": ref.hash, "size": ref.size, "media_type": ref.media_type}


def _exit_summary(
    command: str, exit_code: int, size: int, *, killed: bool = False
) -> str:
    if killed:
        status = "killed"
    elif exit_code == 0:
        status = "OK"
    else:
        status = f"exit={exit_code}"
    cmd = command if len(command) <= 80 else command[:77] + "..."
    return f"background {cmd} → {status} ({size}B output)"


#: Slack on the elapsed-time identity check (issue 06). A process whose live
#: start time is later than ``started_at + this`` is treated as a DIFFERENT
#: process (PID reuse) → UNCONFIRMED. The window absorbs clock skew /
#: low-resolution ``lstart`` rounding without ever confirming a process that
#: clearly started after the recorded job.
_PROBE_START_SLACK_S = 5.0


def _posix_identity_probe(
    *, pid: int, command: str, started_at: float
) -> ProbeResult:
    """Conservative POSIX process-identity probe.

    Runs ``ps -p <pid> -o lstart=,command=`` and decides:

    * the PID is not alive (``ps`` non-zero / empty) → :attr:`ProbeResult.DEAD`;
    * a live process holds the PID AND it is plausibly the original job — its
      reported command shares the recorded command's leading program token, and
      its start time is NOT newer than the recorded ``started_at`` (within a
      small slack) → :attr:`ProbeResult.CONFIRMED`;
    * a live process holds the PID but its identity cannot be confirmed (a
      different command, or a start time clearly after the recorded job → PID
      reuse), OR ``ps`` is unavailable / unparsable → :attr:`ProbeResult.UNCONFIRMED`.

    The bar to CONFIRM is deliberately high — a killed process is irreversible,
    so any doubt resolves to UNCONFIRMED (no kill). Non-POSIX / no-``ps`` hosts
    fall through to UNCONFIRMED, so recovery still emits the mandatory Lost mark
    but never kills."""
    try:
        proc = subprocess.run(
            ["ps", "-p", str(pid), "-o", "lstart=,command="],
            capture_output=True,
            text=True,
            timeout=5.0,
        )
    except (OSError, subprocess.SubprocessError):
        # ``ps`` unavailable (non-POSIX, sandbox) → cannot confirm → never kill.
        return ProbeResult.UNCONFIRMED
    line = proc.stdout.strip()
    if proc.returncode != 0 or not line:
        return ProbeResult.DEAD  # no such live process — nothing to kill
    # ``lstart`` is ``Www Mmm DD HH:MM:SS YYYY`` (5 whitespace tokens: weekday
    # month day time year); the remainder is the command. Parse defensively —
    # any parse failure → UNCONFIRMED.
    parts = line.split(None, 5)
    if len(parts) < 6:
        return ProbeResult.UNCONFIRMED
    lstart_str = " ".join(parts[:5])
    live_command = parts[5]
    live_started_at = _parse_lstart(lstart_str)
    if live_started_at is None:
        return ProbeResult.UNCONFIRMED
    # PID reuse guard: a process that started clearly AFTER the recorded job is a
    # different process that recycled the PID — never kill it.
    if live_started_at > started_at + _PROBE_START_SLACK_S:
        return ProbeResult.UNCONFIRMED
    # Command match: the live process's leading program token must match the
    # recorded command's leading token (argv-only spawn, so the program is the
    # first token). A mismatch → a different process on the same PID.
    if not _commands_match(recorded=command, live=live_command):
        return ProbeResult.UNCONFIRMED
    return ProbeResult.CONFIRMED


def _parse_lstart(lstart: str) -> Optional[float]:
    """Parse ``ps`` ``lstart`` (``Www Mmm DD HH:MM:SS YYYY``) to epoch seconds.

    Returns ``None`` on any parse failure (→ the caller resolves to
    UNCONFIRMED, never a kill)."""
    for fmt in ("%a %b %d %H:%M:%S %Y", "%a %b  %d %H:%M:%S %Y"):
        try:
            return time.mktime(time.strptime(lstart, fmt))
        except (ValueError, OverflowError):
            continue
    return None


def _commands_match(*, recorded: str, live: str) -> bool:
    """True iff the live process's leading program token matches the recorded
    command's. argv-only spawn (never ``shell=True``), so the program is the
    first whitespace token; comparing the basename tolerates absolute-vs-relative
    path differences in how ``ps`` reports the command."""
    rec_tok = recorded.split()
    live_tok = live.split()
    if not rec_tok or not live_tok:
        return False
    return os.path.basename(rec_tok[0]) == os.path.basename(live_tok[0])


def _posix_kill_pid(pid: int) -> None:
    """Best-effort SIGTERM→SIGKILL of a CONFIRMED orphan PID (issue 06).

    Only ever reached after :func:`_posix_identity_probe` (or an injected probe)
    returned CONFIRMED. SIGTERM first (let it flush), then SIGKILL after a short
    grace. A dead-process race (``ProcessLookupError``) is benign — the Lost mark
    is the durable record either way.

    The SIGTERM is sent synchronously, but the ``grace → SIGKILL`` escalation
    runs on a short-lived daemon thread so this NEVER blocks the caller. That
    matters because ``recover_orphans`` runs this serially per orphan on the host
    STARTUP thread (``build_code_server``): a synchronous ``time.sleep(grace)``
    here would stall boot by ``grace × N`` before the server accepts requests.
    Mirrors the live ``kill`` path's daemon-timer offload.

    tools m4: when the orphan LEADS its own process group (``pgid == pid`` —
    every job spawned with ``start_new_session=True``), signal the GROUP so
    its backgrounded grandchildren are reaped too. An orphan recorded by a
    pre-group-spawn version shares the old host's group — group-killing it
    would nuke unrelated processes, so those fall back to the single PID
    (``require_leader=True`` on the shared primitive)."""
    send_group_signal(pid, signal.SIGTERM, require_leader=True)

    def _escalate() -> None:
        time.sleep(DEFAULT_KILL_GRACE_S)
        send_group_signal(pid, signal.SIGKILL, require_leader=True)

    threading.Thread(
        target=_escalate, name=f"bg-orphan-kill-{pid}", daemon=True
    ).start()
