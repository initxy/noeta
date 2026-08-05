"""Live table of processes launched by ``shell_run(background=true)``.

A background command is a host-layer effect, not a subtask — it carries no
Policy — so it never enters the Task model: the ``BackgroundShell*`` events
are its authoritative record and this table is a runtime accelerator that is
never persisted. Output is buffered off-ledger and minted into a fresh
immutable content-addressed snapshot whenever the model needs a ref, so the
``(ref, offset)`` a ``BackgroundShellPolled`` pins reproduces exactly the
prefix the model saw and later output can never bleed into a historical poll.
Spawn, watcher and poll each arrive on a different thread, so the table and
every job buffer are lock-guarded.
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


#: The host completion-push callback: called once, AFTER the durable
#: ``BackgroundShellExited``, with ``(root_task_id, job_id, summary,
#: final_ref)``. It MUST NOT block — the host's contract is to hand the drive
#: off to its own thread, since it runs on the job's watcher thread.
BackgroundExitCallback = Callable[[str, str, str, ContentRef], None]


class ProbeResult(enum.Enum):
    """The verdict of a process-identity probe during orphan recovery.

    The recovery PID-kill fires only on :attr:`CONFIRMED`. A kill is
    irreversible and the orphan's PID may have been recycled onto an innocent
    process, so doubt must resolve to "do not kill".
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


#: Process-identity probe over the recorded ``pid``, ``command`` and the job's
#: ``started_at`` (the ``BackgroundShellStarted`` envelope's ``occurred_at``).
#: Injectable so recovery is testable without depending on a real process table.
IdentityProbe = Callable[..., ProbeResult]

#: Best-effort PID killer. Injectable so a test can assert the
#: never-kill-on-doubt behaviour without touching a real process.
PidKiller = Callable[[int], None]


__all__ = [
    "DEFAULT_BACKGROUND_OUTPUT_CAP",
    "DEFAULT_KILL_GRACE_S",
    "DEFAULT_MAX_BACKGROUND_JOBS_PER_ROOT_TASK",
    "BackgroundExitCallback",
    "IdentityProbe",
    "PidKiller",
    "ProbeResult",
    "ProcessRegistry",
]


#: Cap on the off-ledger buffer (combined stdout+stderr) per job. The TAIL is
#: what survives, so a chatty long-running process keeps its most recent output.
DEFAULT_BACKGROUND_OUTPUT_CAP = 256 * 1024  # 256 KB

#: Per-session ceiling on RUNNING jobs. ``spawn`` REJECTS over it rather than
#: queueing — a "kill one first" refusal is more actionable for an agent than
#: an invisible wait. A rejected spawn writes no event, so it stays invisible
#: to resume: the count comes from the live table, never the log.
DEFAULT_MAX_BACKGROUND_JOBS_PER_ROOT_TASK = 8

#: Grace between SIGTERM and the escalation to SIGKILL. Long enough for a
#: well-behaved process to flush and exit, short enough that a human
#: emergency-stop feels prompt.
DEFAULT_KILL_GRACE_S = 5.0

#: The watcher reads pipes in this many bytes per chunk. Small enough that a
#: mid-run poll catches a partial prefix promptly.
_READ_CHUNK = 4096

#: Bound on the ``parent_task_id`` chain walk. Real lineage is far shallower;
#: the bound only guarantees termination on a degenerate / cyclic stream.
_MAX_CHAIN_DEPTH = 64

_SNAPSHOT_MEDIA_TYPE = "text/plain"

#: ``actor`` names the writer's identity; it is orthogonal to
#: ``origin="observer"``, which is the role a suspend window keys on so that
#: exiting the window re-injects these events.
_ACTOR = "background-shell"


@dataclass
class _JobHandle:
    """Live state for one background job (off-ledger, never serialized)."""

    job_id: str
    popen: subprocess.Popen[bytes]
    command: str
    #: Lifetime OWNER = the session root task. Events ride this stream, ``_jobs``
    #: is keyed by it, and the close / cancel cascade kills by it.
    root_task_id: str
    #: LINEAGE only — a label, never a ``wake_on`` blocker: the task that
    #: actually ran ``shell_run(background)``, so a UI can attribute a job even
    #: when a subtask spawned it.
    spawned_by_task_id: str
    trace_id: str
    output_cap: int
    #: Combined stdout+stderr captured so far (tail-truncated to ``output_cap``).
    buffer: bytearray = field(default_factory=bytearray)
    #: Latched True the first time the buffer overflows ``output_cap`` and the
    #: head is dropped. Surfaced on ``poll`` and the ``Polled`` / ``Exited``
    #: payloads so the model knows it is reading the tail, not the whole output.
    truncated: bool = False
    #: Monotonic count of bytes ever appended (never decremented on the tail
    #: drop); with ``delivered_total`` it derives the incremental slice a
    #: ``BashOutput`` poll hands the model (only output since the last poll).
    total_written: int = 0
    #: How many appended bytes have already been delivered via ``poll``.
    delivered_total: int = 0
    status: str = "running"  # "running" | "exited" | "killed"
    exit_code: Optional[int] = None
    watcher: Optional[threading.Thread] = None
    #: Per-job lock guarding ``buffer`` / ``status`` / ``exit_code`` / the
    #: ``killed`` marker so the watcher's append never tears against a
    #: concurrent poll snapshot AND the reap path sees a consistent kill marker.
    lock: threading.Lock = field(default_factory=threading.Lock)
    #: Exit-event dedup, set ONCE under ``lock`` in the watcher's reap so a
    #: kill racing a natural exit still yields exactly one terminal event and
    #: exactly one completion push.
    notified: bool = False
    #: Kill marker set by :meth:`ProcessRegistry.kill` under ``lock``; the
    #: watcher's reap reads it to record ``BackgroundShellKilled`` instead of
    #: ``BackgroundShellExited``. ``kill_signal`` is the signal that ACTUALLY
    #: reaped the process (SIGTERM if the grace sufficed, SIGKILL after
    #: escalation).
    killed: bool = False
    kill_signal: int = int(signal.SIGTERM)


@dataclass
class _ForegroundHandle:
    """Live state for one FOREGROUND shell command (off-ledger, never persisted).

    Foreground runs are the tool call's own synchronous result — no watcher, no
    buffer, no ``BackgroundShell*`` events. The table entry exists ONLY so the
    human-stop family (interrupt / cancel / close) can reap the process group a
    step thread is blocked on in ``communicate()``; the tool thread owns the
    ``Popen`` and does the reap itself once the group dies and the pipes close.
    """

    popen: subprocess.Popen[bytes]
    #: Lifetime OWNER = the session root task, same keying as ``_JobHandle`` so
    #: one ``kill_root_task`` cascade reaches both kinds.
    root_task_id: str
    #: Set (under the registry lock) by the kill cascade BEFORE it signals, so
    #: the tool call — once ``communicate`` returns — reports *interrupted*
    #: rather than a plain non-zero exit. Read via ``unregister_foreground``.
    killed: bool = False


@dataclass(frozen=True)
class _Orphan:
    """An orphan job reconstructed from the durable log.

    The registry is empty after a host restart, so a ``BackgroundShellStarted``
    with no later terminal is the only trace of a still-running process.
    ``started_at`` is that envelope's ``occurred_at``; the identity probe
    compares it against the live process's start time to defend against PID
    reuse."""

    job_id: str
    root_task_id: str
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
        max_jobs_per_root_task: int = DEFAULT_MAX_BACKGROUND_JOBS_PER_ROOT_TASK,
        dispatcher: Optional[Dispatcher] = None,
        on_background_exit: Optional[BackgroundExitCallback] = None,
        identity_probe: Optional[IdentityProbe] = None,
        kill_pid: Optional[PidKiller] = None,
    ) -> None:
        self._event_log = event_log
        self._content_store = content_store
        self._output_cap = output_cap
        # Crash-recovery seams, runtime-only: a resume that folds the log never
        # invokes either.
        self._identity_probe = identity_probe or _posix_identity_probe
        self._kill_pid = kill_pid or _posix_kill_pid
        self._max_jobs_per_root_task = max_jobs_per_root_task
        # Both default to ``None`` ⇒ no completion push at all, which is the
        # correct shape on a log-folding resume: neither is wired there.
        self._dispatcher = dispatcher
        self._on_background_exit = on_background_exit
        self._lock = threading.Lock()
        # Keyed by the ROOT TASK, not the spawning task: a subtask-spawned job
        # is owned by the session, so it must survive the subtask's terminal
        # and be reachable by the close / cancel cascade.
        self._jobs: dict[str, dict[str, _JobHandle]] = {}
        # Sibling table for FOREGROUND runs, same root-task keying: an entry
        # lives only for the duration of one blocking ``shell_run`` call so the
        # human-stop cascade can reap the group the step thread is waiting on.
        self._foreground: dict[str, list[_ForegroundHandle]] = {}

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
        blocked on the process. ``argv`` is a list, never ``shell=True``, and
        the caller has already done the argv-only / metachar / allowlist
        validation, so the registry only spawns.

        Over ``max_jobs_per_root_task`` RUNNING jobs the spawn is REJECTED:
        returns ``{"rejected": True, "reason": ...}`` with no surviving process
        and no ``BackgroundShellStarted``.
        """
        job_id = f"bg-{uuid.uuid4().hex[:12]}"
        root_task_id = self._resolve_root_task_id(spawned_by_task_id)
        # Popen runs OUTSIDE the lock because it is slow; the ceiling check and
        # the table insert below then have to happen under a SINGLE lock hold,
        # or two concurrent spawns on one session both pass the ceiling and
        # overrun to cap+1.
        popen = subprocess.Popen(
            argv,
            cwd=str(cwd),
            env=env,
            # A detached job must NOT inherit the host's stdin: it has no
            # console to read from, and an inherited fd 0 lets it steal bytes
            # from whatever drives the host process. DEVNULL gives it an
            # immediate, deterministic EOF instead.
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,  # merge so one buffer = the model's view
            # The job leads its own process group so a kill reaps backgrounded
            # grandchildren too (see ``_terminate``'s group signal).
            start_new_session=True,
        )
        handle = _JobHandle(
            job_id=job_id,
            popen=popen,
            command=command,
            root_task_id=root_task_id,
            spawned_by_task_id=spawned_by_task_id,
            trace_id=trace_id,
            output_cap=self._output_cap,
        )
        with self._lock:
            running = sum(
                1
                for h in self._jobs.get(root_task_id, {}).values()
                if h.status == "running"
            )
            if running >= self._max_jobs_per_root_task:
                # The process is already spawned, so rejecting means killing it
                # here. No event was emitted, so resume never sees it.
                popen.kill()
                try:  # reap so the throwaway process never lingers as a zombie
                    popen.wait(timeout=DEFAULT_KILL_GRACE_S)
                except (subprocess.TimeoutExpired, OSError):
                    pass
                return {
                    "rejected": True,
                    "reason": (
                        f"too many background jobs "
                        f"({running}/{self._max_jobs_per_root_task}); kill one with "
                        f"KillShell first"
                    ),
                }
            self._jobs.setdefault(root_task_id, {})[job_id] = handle

        # Started carries a ref, not bytes, so even the empty buffer is minted
        # as a snapshot — every later one grows from it.
        ref = self._snapshot(handle)
        # Emitted on the ROOT-TASK stream so a per-session read model lists
        # every job of the session; the payload's ``spawned_by_task_id`` keeps
        # the real spawner for lineage.
        self._event_log.system_emit(
            task_id=root_task_id,
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

        The snapshot pins the exact prefix the model sees right now, which is
        what lets a resume reproduce this poll instead of a later, longer
        buffer."""
        handle = self._find(job_id)
        if handle is None:
            return {"status": "unknown", "ref": None, "offset": 0}
        ref = self._snapshot(handle)
        with handle.lock:
            truncated = handle.truncated
            # Read status + exit_code together under the lock: a watcher reap
            # landing mid-poll would otherwise yield an inconsistent pair (a
            # 'running' job already carrying an exit_code).
            status = handle.status
            exit_code = handle.exit_code
            # The incremental slice: bytes appended since the last poll, taken
            # from the buffer tail. A gap larger than the buffer means the
            # oldest undelivered bytes were tail-dropped — reported as a count
            # so the model knows the delta itself has a hole.
            undelivered = handle.total_written - handle.delivered_total
            take = min(undelivered, len(handle.buffer))
            new_output = (
                bytes(handle.buffer[len(handle.buffer) - take:]) if take else b""
            )
            new_output_dropped = undelivered - take
            handle.delivered_total = handle.total_written
        self._event_log.system_emit(
            task_id=handle.root_task_id,
            type="BackgroundShellPolled",
            payload=BackgroundShellPolledPayload(
                job_id=job_id,
                ref=ref,
                offset=ref.size,
                # Omitted rather than False when un-truncated, so the payload
                # carries the flag only where it changes meaning.
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
            # Surfaced to the model so it knows this snapshot is the tail.
            "truncated": truncated,
            # Only the output produced since the previous poll rides inline.
            "new_output": new_output.decode("utf-8", errors="replace"),
            "new_output_dropped": new_output_dropped,
        }
        if status in ("exited", "killed"):
            out["exit_code"] = exit_code
        return out

    # -- foreground tracking -----------------------------------------------

    def register_foreground(
        self, *, popen: subprocess.Popen[bytes], spawned_by_task_id: str
    ) -> _ForegroundHandle:
        """Track a FOREGROUND command for the session kill cascade.

        Called by ``shell_run`` right after the spawn and BEFORE the blocking
        ``communicate()``, so an interrupt / cancel / close landing mid-run
        finds the group in :meth:`kill_root_task` and reaps it — the blocked
        wait then returns immediately instead of running out the timeout. The
        caller MUST pair this with :meth:`unregister_foreground` in a
        ``finally``. Root resolution mirrors ``spawn``: a subtask-run command
        is owned by the session, so the session-level stop reaches it.
        """
        root_task_id = self._resolve_root_task_id(spawned_by_task_id)
        handle = _ForegroundHandle(popen=popen, root_task_id=root_task_id)
        with self._lock:
            self._foreground.setdefault(root_task_id, []).append(handle)
        return handle

    def unregister_foreground(self, handle: _ForegroundHandle) -> bool:
        """Drop a foreground entry once its ``communicate()`` has returned.

        Returns ``True`` iff the kill cascade reaped this run, so the tool
        reports *interrupted* rather than a plain non-zero exit. Idempotent:
        an already-removed handle (e.g. after :meth:`purge_root_task`) still
        answers from its own ``killed`` mark.
        """
        with self._lock:
            entries = self._foreground.get(handle.root_task_id)
            if entries is not None:
                try:
                    entries.remove(handle)
                except ValueError:
                    pass
                if not entries:
                    del self._foreground[handle.root_task_id]
            return handle.killed

    def _kill_foreground(
        self, handle: _ForegroundHandle, *, grace_s: float = DEFAULT_KILL_GRACE_S
    ) -> None:
        """SIGTERM a foreground run's group, then SIGKILL after ``grace_s``.

        Same escalation discipline as :meth:`kill`, minus the watcher: the tool
        thread blocked in ``communicate()`` owns the ``Popen`` and reaps it as
        soon as the group dies and the pipes close, so this only signals — and
        the grace wait runs on a short-lived daemon thread so the caller
        (the control-plane interrupt / cancel / close) is never blocked."""
        self._terminate(handle.popen, signal.SIGTERM)

        def _escalate() -> None:
            try:
                handle.popen.wait(timeout=grace_s)
                return  # SIGTERM reaped it within the grace.
            except subprocess.TimeoutExpired:
                pass
            self._terminate(handle.popen, signal.SIGKILL)

        threading.Thread(
            target=_escalate,
            name=f"fg-kill-{handle.popen.pid}",
            daemon=True,
        ).start()

    # -- kill --------------------------------------------------------------

    def kill(self, job_id: str, *, grace_s: float = DEFAULT_KILL_GRACE_S) -> dict[str, Any]:
        """SIGTERM the job, then SIGKILL after ``grace_s`` if still alive.

        Returns PROMPTLY: the grace wait and the SIGKILL escalation run on a
        short-lived daemon thread so the engine / tool-invoke thread is never
        blocked. The reap and the terminal ``BackgroundShellKilled`` come from
        the watcher, which is already blocked on ``popen.wait()``, so this only
        marks the handle and signals.

        Idempotent: an unknown ``job_id`` reports ``unknown``, and an
        already-terminal job keeps the terminal the natural reap recorded — a
        Killed is never forced over a recorded Exited."""
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
        # Only the FIRST kill signals and arms the escalation timer; a
        # redundant kill must not stack a second timer.
        if not already_killing:
            self._terminate(handle.popen, signal.SIGTERM)
            timer = threading.Thread(
                target=self._escalate_after_grace,
                args=(handle, grace_s),
                name=f"bg-kill-{job_id}",
                daemon=True,
            )
            timer.start()
        return {"job_id": job_id, "status": "killing"}

    def kill_root_task(self, root_task_id: str) -> list[dict[str, Any]]:
        """Human emergency-stop — kill ALL background jobs of one session.

        Jobs are keyed by the ROOT task, so this reaches every job of the
        session including ones a subtask spawned. The session-close cascade
        routes through here too, so there is one kill primitive, not two.

        Also reaps any registered FOREGROUND run (interrupt / cancel / close
        landing while a step thread is blocked in ``communicate()``): the
        handle is marked killed BEFORE the signal so the returning tool call
        reads *interrupted*, never a plain exit. Foreground reaps are not
        itemized in the return — the tool call reports its own result."""
        with self._lock:
            job_ids = list(self._jobs.get(root_task_id, {}).keys())
            fg_handles = list(self._foreground.get(root_task_id, ()))
            for fg in fg_handles:
                fg.killed = True
        for fg in fg_handles:
            self._kill_foreground(fg)
        return [self.kill(job_id) for job_id in job_ids]

    def purge_root_task(self, root_task_id: str) -> None:
        """Drop all retained job handles for a *permanently deleted* session.

        A ``_JobHandle`` (with its tail buffer, up to ``output_cap``) stays
        resident after a job exits so a late ``poll`` still reports the
        terminal status and final output — which is why ``kill_root_task`` and
        close deliberately do NOT drop handles: a closed conversation is
        reopenable and inspectable. A deleted conversation is never polled
        again, so its handles are a pure leak of one entry per job for the
        process lifetime. Kills nothing: delete follows close/cancel, which
        already reaped the OS processes."""
        with self._lock:
            self._jobs.pop(root_task_id, None)
            # Foreground entries are transient (the tool call unregisters in a
            # ``finally``); dropping any straggler here is pure hygiene, and a
            # late ``unregister_foreground`` still answers from its own mark.
            self._foreground.pop(root_task_id, None)

    # -- crash recovery / orphan reaping -----------------------------------

    def recover_orphans(
        self, *, probe: Optional[IdentityProbe] = None
    ) -> list[str]:
        """Reap orphan background jobs left by a host crash or restart.

        A restart loses this in-memory registry while the OS processes it
        spawned are reparented to ``init``, leaving a
        ``BackgroundShellStarted`` with no terminal on the session-root stream.
        For each such orphan this does two things, in order of authority:

        1. **MANDATORY** — emit ``BackgroundShellLost(job_id)``, so nothing
           keeps showing the job as forever-"running". The Lost mark stands
           regardless of what happens to the PID; it is the durable record.
        2. **CONSERVATIVE best-effort PID kill** — kill only on
           :attr:`ProbeResult.CONFIRMED`. A dead PID needs no kill, and an
           UNCONFIRMED live PID may have been recycled onto an innocent
           process, so doubt never escalates to an irreversible kill.

        A startup SIDE EFFECT, run once and never re-derived: a resume that
        folds the log constructs no registry, so the scan and the PID kill do
        not re-run. A second pass is a clean no-op, since a Lost is itself a
        terminal.
        """
        index = self._event_log
        # Only storage-backed logs implement ``list_task_streams``
        # (EventLogTaskIndex). Recovering nothing on a log without it is the
        # intended outcome: recovery must never run against a non-storage log.
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

        Every lifecycle event of a job rides its session-root stream, so
        "no later ``Exited`` / ``Killed`` / ``Lost`` on the SAME stream" is a
        complete orphan test. Pure read of the durable log — the registry is
        empty after a restart, which is why the log is the only source."""
        started: dict[str, _Orphan] = {}
        terminal: set[str] = set()
        for env in self._event_log.read(task_id):
            if env.type == "BackgroundShellStarted":
                started[env.payload.job_id] = _Orphan(
                    job_id=env.payload.job_id,
                    root_task_id=task_id,
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
            task_id=orphan.root_task_id,
            type="BackgroundShellLost",
            payload=BackgroundShellLostPayload(job_id=orphan.job_id),
            actor=_ACTOR,
            origin="observer",
            trace_id=orphan.trace_id,
        )

    def _best_effort_kill(self, orphan: _Orphan, probe: IdentityProbe) -> None:
        """CONSERVATIVE PID kill — fire ONLY on a CONFIRMED identity.

        The PID may have been recycled onto an innocent process since the host
        crashed and a kill is irreversible, so the probe must positively
        confirm the live PID is the original job. An UNCONFIRMED live PID is
        left strictly alone, and a probe that raises never reaches the kill."""
        verdict = probe(
            pid=orphan.pid, command=orphan.command, started_at=orphan.started_at
        )
        if verdict is ProbeResult.CONFIRMED:
            self._kill_pid(orphan.pid)

    def _terminate(self, popen: subprocess.Popen[bytes], sig: int) -> None:
        """Send ``sig`` to the process's whole GROUP, swallowing a
        dead-process race.

        Background jobs and foreground runs alike lead their own group
        (``start_new_session=True``), so signalling the group also reaps
        backgrounded grandchildren (``bash -c "server & wait"``) that a
        single-PID signal would orphan. A failed signal is benign: the
        terminal outcome is the reaping thread's job either way."""
        # PID-reuse guard: once the child has been reaped (returncode set),
        # the pid may be recycled onto an unrelated process and
        # ``os.getpgid(pid)`` would resolve a stranger's group to ``killpg``.
        if popen.returncode is not None:
            return
        send_group_signal(popen.pid, sig)

    def _escalate_after_grace(self, handle: _JobHandle, grace_s: float) -> None:
        """Daemon thread: wait ``grace_s``, then SIGKILL if still alive.

        The escalated signal is recorded under ``lock`` so the watcher's reap
        reports SIGKILL rather than the SIGTERM that did not work. Runs off the
        ``kill`` caller's thread so the engine never blocks on a grace."""
        try:
            handle.popen.wait(timeout=grace_s)
            return  # SIGTERM (or a natural exit) reaped it within the grace.
        except subprocess.TimeoutExpired:
            pass
        with handle.lock:
            if handle.notified:
                return  # the watcher already reaped it during the grace
            handle.kill_signal = int(signal.SIGKILL)
        self._terminate(handle.popen, signal.SIGKILL)

    # -- watcher (daemon thread) -------------------------------------------

    def _watch(self, handle: _JobHandle) -> None:
        """Drain the merged pipe into the buffer, then reap + record exit."""
        stdout = handle.popen.stdout
        if stdout is not None:
            while True:
                # ``read1``, not ``read``: a plain ``read(_READ_CHUNK)`` blocks
                # until 4096 bytes accumulate OR EOF, so a process that dribbles
                # output slowly (a dev server, a build printing progress) would
                # show NOTHING on a mid-run poll until it exits.
                chunk = stdout.read1(_READ_CHUNK)
                if not chunk:
                    break
                with handle.lock:
                    handle.buffer.extend(chunk)
                    handle.total_written += len(chunk)
                    if len(handle.buffer) > handle.output_cap:
                        # Tail-truncate (keep the most recent output), mirroring
                        # Tail-truncate (keep the most recent output), mirroring
                        # the sync path's ``cap_stream``, and latch the
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
            # which terminal it is so a late poll reports the right one. The
            # flip itself happens AFTER the durable terminal event below, so a
            # poll that observes a terminal status implies the event is
            # already in the ledger (never the other way around).
            killed = handle.killed
            # Exactly-once: the FIRST thread to reach the reap under ``lock``
            # wins; ``notified`` then forbids a second terminal event AND a
            # second push, so a kill racing a near-simultaneous natural exit
            # still records ONE terminal event + ONE push.
            if handle.notified:
                return
            handle.notified = True
            kill_signal = handle.kill_signal
            truncated = handle.truncated  # surfaced on Exited
        final_ref = self._snapshot(handle)
        summary = _exit_summary(handle.command, exit_code, final_ref.size, killed=killed)
        if killed:
            # One terminal event per job: a killed job records Killed, never
            # also Exited. Emitted on the ROOT-TASK stream (lifetime owner).
            self._event_log.system_emit(
                task_id=handle.root_task_id,
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
                task_id=handle.root_task_id,
                type="BackgroundShellExited",
                payload=BackgroundShellExitedPayload(
                    job_id=handle.job_id,
                    exit_code=exit_code,
                    final_ref=final_ref,
                    summary=summary,
                    # Record True ONLY when the buffer overflowed output_cap;
                    # un-truncated exits omit it.
                    truncated=True if truncated else None,
                ),
                actor=_ACTOR,
                origin="observer",
                trace_id=handle.trace_id,
            )
        # Ledger-first ordering: only now may the terminal status become
        # observable to ``poll`` — a terminal status answer implies the
        # terminal event is already durable. (Before this ordering, a fast
        # poll could read ``exited`` in the window between the status flip
        # and the emit, which is exactly what the lifetime tests assert
        # cannot happen.)
        with handle.lock:
            handle.status = "killed" if killed else "exited"
        # After the durable Exited, hand the completion to the host so it
        # drives a wake-and-notify turn. The ``notified`` dedup above guards
        # exactly-once (a kill and a natural exit cannot both push). The
        # callback MUST NOT block the watcher — the host contract is to enqueue
        # the drive onto its own background-drive thread. The first arg is the
        # ROOT-TASK (the stream the host suspends on / drives
        # ``notify_background_exit`` against), so a subtask-spawned job wakes the
        # ROOT session, never a dead subtask.
        if self._on_background_exit is not None:
            self._on_background_exit(
                handle.root_task_id, handle.job_id, summary, final_ref
            )

    # -- helpers -----------------------------------------------------------

    def _resolve_root_task_id(self, task_id: str) -> str:
        """Walk the ``parent_task_id`` chain to the session root.

        The canonical lineage edge is ``fold(...).parent_task_id`` (the same one
        ``read_models/sessions`` reads from ``TaskCreated.parent_task_id``); a
        root conversation folds to ``parent_task_id is None``. We follow it to
        the top so a subtask-spawned job is owned by the ROOT TASK, not the
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

    The same shape ``noeta.tools.refs.ref_json`` produces; spelled locally so
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


#: Slack on the elapsed-time identity check. A process whose live start time is
#: later than ``started_at + this`` is treated as a DIFFERENT process (PID
#: reuse) → UNCONFIRMED. The window absorbs clock skew / low-resolution
#: ``lstart`` rounding without ever confirming a process that clearly started
#: after the recorded job.
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
    """Best-effort SIGTERM→SIGKILL of a CONFIRMED orphan PID.

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

    ``require_leader=True`` gates the group kill: recovery signals the GROUP
    only when the orphan LEADS its own group (``pgid == pid``, as every job
    spawned with ``start_new_session=True`` does) so its backgrounded
    grandchildren are reaped too; an orphan that does not lead its own group
    sits in someone else's group, where a group-kill would nuke unrelated
    processes, so it falls back to the single PID."""
    send_group_signal(pid, signal.SIGTERM, require_leader=True)

    def _escalate() -> None:
        time.sleep(DEFAULT_KILL_GRACE_S)
        send_group_signal(pid, signal.SIGKILL, require_leader=True)

    threading.Thread(
        target=_escalate, name=f"bg-orphan-kill-{pid}", daemon=True
    ).start()
