"""The process-local table of background jobs (``Bash`` with
``run_in_background`` and ``Monitor``), and of foreground ``Bash`` commands,
which run as jobs the call waits for so that one whose timeout runs out can
be moved to the background (it then keeps its task's claim).

A job belongs to the worker process that started it and to the task that
started it: the table is keyed by task, the cap counts a task's running
jobs, and the task's end kills them all. A job runs through the task's
``ExecEnv`` with its output redirected to a log file inside that env, so the
same code serves the local machine and a container: the model reads the
file (its path is in the launch result and the end notice), a monitor tails
its stdout (kept beside it; its output file has stderr too), and stopping
the job (``TaskStop``) is cancelling the env's run (which kills its process
group). A finished command's file ends with a blank line
and ``[exited with code N]``.

A monitor's events go to the task that started the job, as system-origin
messages behind the not-the-user guard, posted with ``when="now"``: they
land after the running batch's tool results, and wake an idle task into a
turn. A job's end goes the same way as ``External(JOB_ENDED)`` (its status
and notice, posted once, before the job gives its kept claim back): the
task's runner records it ended and tells the model, unless the log already
says the job was lost (``noeta.plugins.shell``).

A job lives as long as its task's claim, which it keeps
(``ToolContext.keep``) so the task stays on this worker over its waits:
when the claim is lost (the worker silent past its lease, or stopping) the
job is killed and its end never posted; the next worker to pick the task
up tells the model the job is lost. A job also dies when its
worker cannot kill it (killed itself, or frozen): a guard runs beside the
command, in its process group, and kills the group once the worker stops
re-creating the job's ``.alive`` file (it does while the claim is kept) or,
on this machine, once the worker's process is gone. The guard needs only
``sh`` tools, so it works the same in a container or a sandbox.

The log directory is ``job_logs.py``'s. A job's output reaches its log
through a pipe that keeps the first ``log_max_bytes``
(:data:`DEFAULT_LOG_MAX_BYTES`), then a line saying the rest was not kept,
and drains the rest, so the command runs on unharmed; a foreground
command's log keeps its start and a rolling tail instead (its result is the
end of what it printed).
"""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import logging
import os
import posixpath
import secrets
import shlex
import string
import weakref
from collections.abc import Awaitable, Callable, Coroutine, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from noeta.core.events import External, Say
from noeta.core.execenv import ExecEnv
from noeta.core.tool import KeptClaim
from noeta.core.types import user
from noeta.plugins.shell.job_logs import DEFAULT_LOG_DIR, prepare_log_dir
from noeta.plugins.shell.notices import (
    MonitorStream,
    event_body,
    looks_like_prompt,
    monitor_end_summary,
    monitor_event,
    monitor_expired_note,
    shell_end_summary,
    stalled_notice,
    task_notification,
    ws_end_summary,
)
from noeta.plugins.shell.output import background_exit_note
from noeta.plugins.shell.websocket import NORMAL_CLOSES, run_ws
from noeta.toolkit.local_env import is_local
from noeta.toolkit.notices import NOTICE_GUARD
from noeta.toolkit.worktrees import job_runs_in

log = logging.getLogger("noeta.plugins.shell")

#: Per-task ceiling on RUNNING jobs; a launch over it is refused, not queued.
DEFAULT_MAX_JOBS = 8
#: Job ids: nine lower-case letters and digits, as Claude Code's.
_ID_CHARS = string.ascii_lowercase + string.digits
#: How often a monitor's log is read for new lines.
MONITOR_POLL_S = 0.1
#: Background jobs of a task kept in memory once their end is posted (for
#: ``TaskStop`` / ``TaskOutput`` on them) at most; older ones are let go.
ENDED_JOBS_KEEP = 64
#: Bytes of output logs a task keeps of the jobs it let go of (their end
#: notices name the file): past it the oldest are removed; the rest go
#: with the task.
LET_GO_LOGS_MAX_BYTES = 256 * 1024 * 1024
#: Tasks kept in the table at most: past it, the least recently used task
#: with no running job is let go of with its logs (a server's tasks rarely
#: end here: an idle one waits for days, one another worker took never
#: comes back).
TASKS_KEEP = 256
#: How often a job's guard looks at its worker.
GUARD_TICK_S = 0.5
#: The shortest tick, for a short lease's guard.
GUARD_TICK_MIN_S = 0.05
#: How often a foreground command's guard (which keeps no claim) looks for
#: its ``.alive`` file; a job that keeps its task's claim is looked at every
#: quarter of the claim's lease, so it is dead before another worker may
#: take the task.
ALIVE_S = 30.0
#: Ticks of 20 ms a foreground command's keeper is given to write what came
#: once the command's shell exited.
FOREGROUND_DRAIN_TICKS = 50
#: Bytes of a job's output (and, for a monitor, of its stdout alone) its
#: logs keep.
DEFAULT_LOG_MAX_BYTES = 64 * 1024 * 1024
#: Claude Code's watch for a background command stuck at a prompt: every
#: ``STALL_CHECK_S`` its output file's size is read; once it has not grown
#: for ``STALL_AFTER_S`` and its last ``STALL_TAIL_BYTES`` end in a line that
#: asks for input, the model is told once.
STALL_CHECK_S, STALL_AFTER_S, STALL_TAIL_BYTES = 5.0, 45.0, 1024

#: Appends its stdin to the file ``$2`` (and, given ``$3``, to that file
#: too until it has added ``$4`` bytes there) as it comes (one read at a
#: time, so a monitor sees each line at once) until it has added ``$1``
#: bytes to ``$2`` (one read more at most), then a line saying the rest was
#: not kept, and reads the rest away: the writer never gets a broken pipe.
#: Each keeper counts only what it wrote itself, so a monitor's two
#: keepers, which share its output file (stderr's ``$2``, stdout's ``$3``),
#: each keep their own half of the cap there whatever order their output
#: comes in, and the file stays within the cap (a read more at most each).
#: It ends when its stdin does. Only ``sh`` tools.
_KEEP_HEAD = r"""keep_head() {
  s=0 t=0 w=1
  while [ "$s" -lt "$1" ]; do
    if [ -n "$3" ] && [ "$w" = 1 ] && [ "$t" -ge "$4" ]; then
      w=0
      printf '\n[output past %s bytes was not kept]\n' "$4" >>"$3"
    fi
    if [ -n "$3" ] && [ "$w" = 1 ]; then
      c=$(dd bs=65536 count=1 2>/dev/null | tee -a "$2" "$3" | wc -c)
      t=$((t + c))
    else
      c=$(dd bs=65536 count=1 2>/dev/null | tee -a "$2" | wc -c)
    fi
    [ "$c" -gt 0 ] || return 0
    s=$((s + c))
  done
  if [ "$(dd bs=1 count=1 2>/dev/null | wc -c)" -gt 0 ]; then
    printf '\n[output past %s bytes was not kept]\n' "$1" >>"$2"
    [ -z "$3" ] || [ "$w" = 0 ] || printf '\n[output past %s bytes was not kept]\n' "$4" >>"$3"
    cat >/dev/null
  fi
}
"""

#: A foreground command's keeper: its result is the end of what it printed,
#: so past ``$1`` bytes the file ``$2`` is cut back to its first quarter, a
#: line saying the middle was not kept, and its last quarter, and goes on
#: growing (each cut copies at most ``$1`` bytes once ``$1 / 2`` came in).
#: While reads come back full it takes up to 16 per size check (never past
#: ``$1`` by more than one): a check costs two processes.
_KEEP_TAIL = r"""keep_tail() {
  s=0
  q=$(($1 / 4))
  n=1
  while :; do
    dd bs=65536 count="$n" 2>/dev/null >>"$2"
    t=$(wc -c <"$2")
    [ "$t" -gt "$s" ] || return 0
    if [ $((t - s)) -gt $((n * 32768)) ]; then
      [ "$n" -ge 16 ] || n=$((n * 2))
    else
      n=1
    fi
    m=$((($1 - t) / 65536))
    [ "$m" -ge 1 ] || m=1
    [ "$n" -le "$m" ] || n=$m
    s=$t
    if [ "$s" -ge "$1" ]; then
      { head -c "$q" "$2"
        printf '\n[output past the first %s bytes was not kept, up to its last part below]\n' "$q"
        tail -c "$q" "$2"; } >"$2.cut" && cat "$2.cut" >"$2"
      rm -f "$2.cut"
      s=$(wc -c <"$2")
    fi
  done
}
"""

#: Runs beside a job, in its process group: exits with the job, kills the
#: group once the worker is gone. ``$1`` the job's shell (its pid), ``$2``
#: the worker's pid on this machine (empty: none to watch), ``$3`` the
#: ``.alive`` file the worker re-creates, ``$4`` ticks it has to do so
#: (0: no file to watch).
_GUARD = r"""
n=0
while sleep "$5"; do
  kill -0 "$1" 2>/dev/null || exit 0
  if [ -n "$2" ] && ! kill -0 "$2" 2>/dev/null; then kill -KILL 0; fi
  if [ "$4" -gt 0 ]; then
    n=$((n + 1))
    if [ "$n" -ge "$4" ]; then n=0; rm "$3" 2>/dev/null || kill -KILL 0; fi
  fi
done
"""

#: The ``External`` a job's end is posted as: ``data = {"job_id", "status",
#: "exit_code", "notice"}`` (``notice`` is ``None`` for a job stopped quietly).
JOB_ENDED = "shell_job_ended"



#: ``(body, task_id)`` posts an inbox message to a task.
Post = Callable[..., Awaitable[None]]
#: Gives back what the job used (its sandbox, the claim it kept).
Release = Callable[[], Awaitable[None]]


@dataclass(eq=False)
class Job:
    job_id: str
    #: The task that started it, which it reports to.
    task_id: str
    command: str
    description: str
    tool_use_id: str
    env: ExecEnv
    out_path: str
    #: A command monitor's stdout alone, read for its events; its output
    #: file has stdout and stderr, for the model to read.
    events_path: str | None
    post: Post
    release: Release | None = None
    #: The task's claim, kept while the job runs.
    kept: KeptClaim | None = None
    #: Its claim was lost: it was killed and its end is never posted.
    fenced: bool = False
    #: The file the worker re-creates while the job may live (its guard
    #: kills the job once it stays gone).
    alive_path: str = ""
    #: How often its guard checks that file, fixed when the guard started.
    guard_period: float = 0.0
    status: str = "running"  # running | exited | killed
    exit_code: int | None = None
    #: A ``Bash`` command waited on in the foreground: it ends without a
    #: notice and leaves its files to the call (:meth:`Registry.discard`),
    #: until :meth:`Registry.to_background` moves it to the background.
    foreground: bool = False
    #: Where a foreground command's shell writes its final directory (fd 3).
    cwd_path: str = ""
    #: A ``Bash`` job's command as the model wrote it: what reads its exit
    #: status (``""`` for a monitor, whose status 0 alone succeeds).
    shell_command: str = ""
    #: Output bytes a monitor has already turned into events.
    tailed: int = 0
    decoder: Any = field(
        default_factory=lambda: codecs.getincrementaldecoder("utf-8")(errors="replace"))
    #: No end notice: the job ended with its turn or task, or a monitor
    #: already sent its last word.
    quiet: bool = False
    monitor: MonitorStream | None = None
    #: A monitor's WebSocket source ``(url, protocols)``, instead of a command.
    ws: tuple[str, list[str]] | None = None
    #: The env's run of the command; cancelling it kills the job.
    proc: asyncio.Task[Any] | None = None
    task: asyncio.Task[None] | None = None
    sender: asyncio.Task[None] | None = None
    #: Watches a background command for a stall at a prompt.
    stall: asyncio.Task[None] | None = None
    done: asyncio.Event = field(default_factory=asyncio.Event)
    #: Says the job no longer works in its directory (a worktree is not
    #: removed under a running job).
    left_dir: Callable[[], None] = lambda: None
    #: Stops listening for its claim being lost (:meth:`Registry.keep_with`).
    forget_kept: Callable[[], None] = lambda: None
    outbox: asyncio.Queue[str | None] = field(default_factory=asyncio.Queue)


def _touch(paths: Sequence[str]) -> None:
    for path in paths:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
        os.close(fd)


def _label(text: str) -> str:
    return " ".join(text.split())


def _keeping(out_path: str) -> str:
    """The file a foreground command's output keeper removes once done."""
    return f"{out_path}.keeping"


def _files(job: Job) -> list[str]:
    """The job's files in its env."""
    keeping = _keeping(job.out_path) if job.cwd_path else ""
    return [p for p in (job.out_path, job.events_path, job.alive_path, job.cwd_path, keeping) if p]


class Registry:
    """Background jobs of this process, by task."""

    def __init__(self, *, max_jobs: int = DEFAULT_MAX_JOBS,
                 log_dir: str = DEFAULT_LOG_DIR,
                 log_max_bytes: int = DEFAULT_LOG_MAX_BYTES,
                 alive_s: float = ALIVE_S,
                 ended_jobs_keep: int = ENDED_JOBS_KEEP,
                 tasks_keep: int = TASKS_KEEP,
                 let_go_logs_max_bytes: int = LET_GO_LOGS_MAX_BYTES,
                 stall_check_s: float = STALL_CHECK_S,
                 stall_after_s: float = STALL_AFTER_S) -> None:
        self.max_jobs = max_jobs
        self.stall_check_s = stall_check_s
        self.stall_after_s = stall_after_s
        self.ended_jobs_keep = ended_jobs_keep
        self.let_go_logs_max_bytes = let_go_logs_max_bytes
        #: Output logs of the jobs each task let go of, oldest first, with
        #: their sizes: ``(env, path, bytes)``.
        self.let_go_logs: dict[str, list[tuple[ExecEnv, str, int]]] = {}
        self.tasks_keep = tasks_keep
        self.alive_s = alive_s
        self.log_dir = log_dir
        self.log_max_bytes = log_max_bytes
        self.jobs: dict[str, dict[str, Job]] = {}
        self._retiring: set[asyncio.Task[None]] = set()
        self._dirs: weakref.WeakKeyDictionary[Any, str] = weakref.WeakKeyDictionary()
        self._keeper: asyncio.Task[None] | None = None

    async def _log_dir(self, env: ExecEnv) -> str:
        """The env's private log directory, made or checked once per env."""
        try:
            known = self._dirs.get(env)
        except TypeError:
            known = None
        if known is not None:
            return known
        path = await prepare_log_dir(env, self.log_dir)
        with contextlib.suppress(TypeError):
            self._dirs[env] = path
        return path

    # ------------------------------------------------------------ lookup

    def find(self, task_id: str, job_id: str) -> Job | None:
        return self.jobs.get(task_id, {}).get(job_id)

    def running(self, task_id: str) -> list[Job]:
        """The task's jobs whose end is not yet told: a job whose command
        has exited counts until its end is posted, so whoever waits for the
        task's jobs to finish sees that end in the inbox once they have."""
        return [j for j in self.jobs.get(task_id, {}).values() if not j.done.is_set()]

    def live(self, task_id: str, job_id: str) -> bool:
        """Whether the job runs here, or its end is on the way to its
        task's inbox: not lost with a claim this worker no longer has."""
        job = self.find(task_id, job_id)
        return job is not None and not job.fenced

    # ------------------------------------------------------------ launch

    async def spawn(self, *, env: ExecEnv, command: str, cwd: str, env_vars: Mapping[str, str],
                    task_id: str, tool_use_id: str, description: str,
                    post: Post, monitor_timeout_ms: float | None = None,
                    release: Release | None = None, kept: KeptClaim | None = None,
                    argv: Sequence[str] | None = None,
                    ws: tuple[str, list[str]] | None = None,
                    ws_resolve: Callable[[str, int], Awaitable[str]] | None = None,
                    foreground: bool = False, shell_command: str = "") -> Job | str:
        """Start a job, or return the refusal text. A ``foreground`` job
        (:attr:`Job.foreground`) also gets a file its fd 3 writes to. ``release`` is awaited
        once the job has ended and its end is posted (or it was refused);
        ``kept`` is the task's claim it keeps, whose loss kills the job.
        ``argv``: what runs instead
        of ``bash -c command`` (the command wrapped in a sandbox); the log
        redirection stays outside it. A monitor with ``ws`` streams that
        WebSocket's messages instead of running ``command``, connecting where
        ``ws_resolve`` says (``run_ws``'s ``resolve``)."""
        job = await self._spawn(env=env, command=command, cwd=cwd, env_vars=env_vars,
                                task_id=task_id,
                                tool_use_id=tool_use_id, description=description, post=post,
                                monitor_timeout_ms=monitor_timeout_ms, release=release,
                                kept=kept, argv=argv, ws=ws, ws_resolve=ws_resolve,
                                foreground=foreground, shell_command=shell_command)
        if isinstance(job, str) and release is not None:
            await release()
        return job

    async def _spawn(self, *, env: ExecEnv, command: str, cwd: str, env_vars: Mapping[str, str],
                     task_id: str, tool_use_id: str, description: str,
                     post: Post, monitor_timeout_ms: float | None,
                     release: Release | None, kept: KeptClaim | None,
                     argv: Sequence[str] | None,
                     ws: tuple[str, list[str]] | None = None,
                     ws_resolve: Callable[[str, int], Awaitable[str]] | None = None,
                     foreground: bool = False, shell_command: str = "") -> Job | str:
        # The cap counts background jobs only (what TaskStop can stop); a
        # foreground command is not held to it either: past its timeout it
        # moves to the background rather than being killed.
        running = sum(not j.foreground for j in self.running(task_id))
        if running >= self.max_jobs and not foreground:
            return (f"too many background jobs ({running}/{self.max_jobs}); stop one with "
                    f"TaskStop first")
        job_id = "".join(secrets.choice(_ID_CHARS) for _ in range(9))
        try:
            log_dir = await self._log_dir(env)
        except OSError as exc:
            return f"background jobs are unavailable: {exc}"
        out = posixpath.join(log_dir, f"{job_id}.output")
        events = posixpath.join(log_dir, f"{job_id}.events") \
            if monitor_timeout_ms and ws is None else None
        alive = (posixpath.join(log_dir, f"{job_id}.alive")
                 if kept is not None or foreground else "")
        cwd_file = posixpath.join(log_dir, f"{job_id}.cwd") if foreground else ""
        made = await env.run(
            ["sh", "-c", 'umask 077 && for f; do : > "$f" || exit 1; done', "sh",
             *[p for p in (out, events, alive, cwd_file) if p]], timeout=10)
        if made.exit_code != 0:
            return "background jobs are unavailable: could not create the job's log file"
        job = Job(job_id=job_id, task_id=task_id, command=command,
                  description=_label(description or command), tool_use_id=tool_use_id,
                  env=env, out_path=out, events_path=events, post=post, release=release, ws=ws,
                  kept=kept, alive_path=alive, foreground=foreground, cwd_path=cwd_file,
                  shell_command=shell_command)
        if monitor_timeout_ms is not None:
            job.monitor = MonitorStream(sink=lambda text, _note: self._event(job, text),
                                        stop=lambda: self._stop_quietly(job))
        job.left_dir = job_runs_in(env, cwd)
        self.jobs[task_id] = self.jobs.pop(task_id, {})  # most recently used last
        self.jobs[task_id][job_id] = job
        self._let_go_of_tasks()
        if ws is not None and job.monitor is not None:
            stream = job.monitor
            job.proc = asyncio.ensure_future(run_ws(*ws, lambda text: stream.feed(text + "\n"),
                                                    resolve=ws_resolve))
        else:
            cap = str(self.log_max_bytes)
            keeper = "keep_tail" if foreground else "keep_head"
            keep_out = f"{keeper} {cap} {shlex.quote(out)}"
            if events is not None:
                # A monitor's output file is shared: each stream keeps half.
                half = str(max(1, self.log_max_bytes // 2))
                keep_out = f"keep_head {half} {shlex.quote(out)}"
                # stdout and stderr each through their own pipe: both are
                # members of the pipeline, so both are written out before
                # the shell exits. Both go to the output file; stdout also
                # to the events file the monitor reads.
                pipeline = (f'{{ "$@" 2>&1 1>&4 4>&- | {keep_out}; '
                            f'exit "${{PIPESTATUS[0]}}"; }} 4>&1 | '
                            f'keep_head {cap} {shlex.quote(events)} {shlex.quote(out)} {half}\n'
                            'exit "${PIPESTATUS[0]}"')
            elif foreground:
                # Ends when the command's shell exits, as Claude Code's: a
                # child it left running (``cmd &``) holds the keeper's pipe,
                # so the keeper is given a moment to write what came (its
                # flag file gone), not waited for. Nothing but the shell
                # may hold the run's own output: the keeper, a process
                # substitution, gets /dev/null.
                flag = shlex.quote(_keeping(out))
                pipeline = (f': > {flag}\nexec >/dev/null 2>&1\n'
                            f'"$@" > >({keep_out}; rm -f {flag}) 2>&1\n'
                            f'code=$?\nn=0\nwhile [ -e {flag} ] && [ "$n" -lt '
                            f'{FOREGROUND_DRAIN_TICKS} ]; do sleep 0.02; n=$((n + 1)); done\n'
                            f'rm -f {flag}\nexit "$code"')
            else:
                pipeline = f'"$@" 2>&1 | {keep_out}\nexit "${{PIPESTATUS[0]}}"'
            fd3 = f"exec 3>{shlex.quote(cwd_file)}\n" if cwd_file else ""
            # The command runs in the pipeline, the guard beside it: a
            # ``wait`` in the command never waits for the guard.
            script = (f"{fd3}{_KEEP_HEAD}{_KEEP_TAIL if foreground else ''}"
                      f"{self._guard(job)} </dev/null >/dev/null 2>&1 3>&- &\n"
                      f"{pipeline}")
            run = ["bash", "-c", script, "bash", *(argv or ["bash", "-c", command])]
            job.proc = asyncio.ensure_future(env.run(run, cwd=cwd,
                                                     env=dict(env_vars), timeout=None,
                                                     output_cap=4096))
        job.task = asyncio.create_task(self._run(job, monitor_timeout_ms))
        job.sender = asyncio.create_task(self._send(job))
        if not foreground:
            self._watch_stall(job)
        if kept is not None:
            self.keep_with(job, kept)
        if alive and (self._keeper is None or self._keeper.done()):
            self._keeper = asyncio.create_task(self._keep_alive())
        return job


    def _alive_period(self, job: Job) -> float:
        """How often ``job``'s guard removes its ``.alive`` file, killing the
        job when it is already gone: the job dies one to two periods after
        the worker last re-created it. A quarter of its kept claim's lease: the
        lease, renewed every third of it, runs out at least two thirds of it
        after a worker froze, so its job is dead by then; else
        :attr:`alive_s`."""
        return job.kept.lease_seconds / 4 if job.kept is not None else self.alive_s

    def _guard(self, job: Job) -> str:
        """The guard's command line for ``job`` (see :data:`_GUARD`)."""
        worker_pid = str(os.getpid()) if is_local(job.env) else ""
        ticks, tick = 0, GUARD_TICK_S
        if job.alive_path:
            # The worker re-creates the file three times per period; the
            # period is never longer than asked.
            period = self._alive_period(job)
            tick = max(min(GUARD_TICK_S, period / 2), GUARD_TICK_MIN_S)
            ticks = max(2, int(period / tick))
        job.guard_period = ticks * tick
        return shlex.join(["sh", "-c", _GUARD, "sh", "$$", worker_pid, job.alive_path,
                           str(ticks), f"{tick:g}"]).replace("'$$'", "$$")

    async def _keep_alive(self) -> None:
        """Re-create the ``.alive`` file of every running job whose claim is
        kept (or that keeps none: a foreground command), three times per
        guard period, until no such job is left."""
        while True:
            jobs = [j for task in self.jobs.values() for j in task.values()
                    if j.status == "running" and j.alive_path]
            if not jobs:
                return
            by_env: dict[int, list[Job]] = {}
            for job in jobs:
                if (job.kept is None or not job.kept.lost) and not job.fenced:
                    by_env.setdefault(id(job.env), []).append(job)
            for group in by_env.values():
                paths = [j.alive_path for j in group]
                with contextlib.suppress(Exception):
                    if is_local(group[0].env):
                        await asyncio.to_thread(_touch, paths)
                    else:
                        await group[0].env.run(
                            ["sh", "-c", 'for f; do : > "$f"; done', "sh", *paths], timeout=10)
            period = min(j.guard_period or self._alive_period(j) for j in jobs)
            await asyncio.sleep(period / 3)

    async def _run(self, job: Job, monitor_timeout_ms: float | None) -> None:
        tail: asyncio.Task[None] | None = None
        expiry: asyncio.TimerHandle | None = None
        if job.monitor is not None and monitor_timeout_ms is not None:
            tail = asyncio.create_task(self._tail(job))
            expiry = asyncio.get_running_loop().call_later(
                monitor_timeout_ms / 1000, self._expire, job, monitor_timeout_ms)
        assert job.proc is not None
        try:
            result = await asyncio.shield(job.proc)
            job.status, job.exit_code = "exited", result.exit_code
        except asyncio.CancelledError:
            if not job.proc.cancelled():
                raise
            job.status = "killed"
        except Exception as exc:  # noqa: BLE001 - a launch failure ends the job
            log.warning("background job %s failed to run: %s", job.job_id, exc)
            job.status, job.exit_code = "exited", 127
        finally:
            if job.stall is not None:
                job.stall.cancel()
            if expiry is not None:
                expiry.cancel()
            if tail is not None:
                tail.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await tail
            try:
                if job.foreground:
                    # The call waiting for it reports the end itself.
                    job.outbox.put_nowait(None)
                    job.forget_kept()
                    job.done.set()
                else:
                    await self._end(job)
            finally:
                job.left_dir()

    async def _end(self, job: Job) -> None:
        notice: str | None = None
        try:
            notice = await self._finish(job)
        finally:
            job.outbox.put_nowait(None)
            if job.sender is not None:
                with contextlib.suppress(Exception):
                    await job.sender  # the monitor's events come first
            try:
                # Posted before the claim is given back: the task's next
                # step finds the end in the inbox.
                await self._post_end(job, notice)
            finally:
                job.forget_kept()
                job.done.set()
                if job.release is not None:
                    with contextlib.suppress(Exception):
                        await job.release()
                self._let_go_of_ended(job.task_id)

    def _let_go_of_ended(self, task_id: str) -> None:
        """Keep at most :attr:`ended_jobs_keep` of the task's background jobs
        whose end is posted, dropping the oldest. A dropped job's output log
        stays until its task goes (its end notice named it), within
        :attr:`let_go_logs_max_bytes`; its other files go now."""
        jobs = self.jobs.get(task_id)
        if not jobs:
            return
        ended = [j for j in jobs.values() if j.done.is_set() and not j.foreground]
        for job in ended[:max(0, len(ended) - self.ended_jobs_keep)]:
            del jobs[job.job_id]
            self._in_background(self._keep_log(job))

    async def _keep_log(self, job: Job) -> None:
        await self._remove_files(job.env, [p for p in _files(job) if p != job.out_path])
        try:
            st = await job.env.stat(job.out_path)
        except OSError:
            st = None
        kept = self.let_go_logs.setdefault(job.task_id, [])
        kept.append((job.env, job.out_path, st.size if st is not None else 0))
        while kept and sum(size for _, _, size in kept) > self.let_go_logs_max_bytes:
            env, path, _ = kept.pop(0)
            await self._remove_files(env, [path])

    async def _remove_let_go_logs(self, task_id: str) -> None:
        for env, path, _ in self.let_go_logs.pop(task_id, []):
            await self._remove_files(env, [path])

    def _let_go_of_tasks(self) -> None:
        """Keep at most :attr:`tasks_keep` tasks, letting go of the least
        recently used ones with no running job, with their logs."""
        idle = [t for t, jobs in self.jobs.items()
                if all(j.done.is_set() for j in jobs.values())]
        for task_id in idle[:max(0, len(self.jobs) - self.tasks_keep)]:
            jobs = self.jobs.pop(task_id)
            self._in_background(self._remove_files_of(list(jobs.values())))
            self._in_background(self._remove_let_go_logs(task_id))

    async def _remove_files_of(self, jobs: list[Job]) -> None:
        for job in jobs:
            await self._remove_files(job.env, _files(job))

    def _in_background(self, work: Coroutine[Any, Any, None]) -> None:
        task = asyncio.create_task(work)
        self._retiring.add(task)
        task.add_done_callback(self._retiring.discard)

    @staticmethod
    async def _remove_files(env: ExecEnv, paths: list[str]) -> None:
        with contextlib.suppress(Exception):
            await env.run(["rm", "-f", *paths], timeout=10)

    async def _post_end(self, job: Job, notice: str | None) -> None:
        if job.fenced:
            return  # its claim is gone: the next pickup calls it lost
        data = {"job_id": job.job_id, "status": self.status_of(job), "exit_code": job.exit_code,
                "notice": f"{NOTICE_GUARD}\n\n{notice}" if notice is not None else None}
        try:
            await job.post(External(name=JOB_ENDED, data=data), task_id=job.task_id,
                           dedupe_key=f"{JOB_ENDED}:{job.job_id}")
        except Exception as exc:  # noqa: BLE001 - the next pickup calls it lost
            log.warning("background job %s: its end was not posted: %s", job.job_id, exc)

    async def _finish(self, job: Job) -> str | None:
        if job.monitor is not None:
            await self._read_new(job)  # the last lines before the exit
        elif job.status == "exited":
            with contextlib.suppress(Exception):
                await job.env.run(["sh", "-c", 'printf "\n[exited with code %s]\n" "$1" >> "$2"',
                                   "sh", str(job.exit_code), job.out_path], timeout=10)
        return await self._end_notice(job)

    # ------------------------------------------------------------ control

    def kill(self, job: Job, *, quiet: bool = False) -> str:
        """Stop a job (SIGTERM, then SIGKILL after the env's grace). Returns
        the status ``TaskStop`` reports."""
        if job.status != "running":
            return job.status
        if quiet:
            job.quiet = True
        if job.proc is not None:
            job.proc.cancel()
        return "killing"

    def to_background(self, job: Job, release: Release | None) -> bool:
        """Move a foreground job to the background: from now on its end is
        told like any job's and ``release`` runs once it is posted. False
        when the command has already ended (the caller reports it)."""
        if not job.foreground or job.proc is None or job.proc.done():
            return False
        job.foreground = False
        job.release = release
        self._watch_stall(job)
        return True

    def _watch_stall(self, job: Job) -> None:
        """Start watching a background command (not a monitor, whose silence
        is normal) for a stall at a prompt."""
        if job.monitor is None and job.ws is None and job.stall is None:
            job.stall = asyncio.create_task(self._stall(job))

    async def _stall(self, job: Job) -> None:
        """Tell the model, once, when the job's output has not grown for
        :attr:`stall_after_s` and its last line asks for input. Reads the
        output file through the job's env, so it watches a job in a
        container as well; any env's failure to read it is logged and the
        watch goes on."""
        try:
            await self._watch_for_prompt(job)
        except Exception:
            log.exception("shell: the stall watch of job %s failed", job.job_id)

    async def _watch_for_prompt(self, job: Job) -> None:
        loop = asyncio.get_running_loop()
        size, quiet_since = 0, loop.time()
        while job.status == "running":
            await asyncio.sleep(self.stall_check_s)
            try:
                st = await job.env.stat(job.out_path)
            except Exception:
                log.warning("shell: cannot read the size of job %s's output", job.job_id,
                            exc_info=True)
                continue
            now = st.size if st is not None else 0
            if now > size:
                size, quiet_since = now, loop.time()
                continue
            if loop.time() - quiet_since < self.stall_after_s:
                continue
            tail = await self._tail_text(job.env, job.out_path, STALL_TAIL_BYTES)
            if not looks_like_prompt(tail):
                quiet_since = loop.time()
                continue
            if job.status == "running" and not job.fenced:
                job.outbox.put_nowait(stalled_notice(
                    task_id=job.job_id, tool_use_id=job.tool_use_id,
                    output_file=job.out_path, description=job.description, tail=tail))
            return

    def keep_with(self, job: Job, kept: KeptClaim) -> None:
        """``job`` lives by ``kept`` (its task's claim) from now on. Losing
        it (the task left this worker) kills the job, and the job and its
        logs are let go of once it is gone: the worker that takes the task
        next has its own table."""
        job.kept = kept

        def lost() -> None:
            self._fence(job)
            self._in_background(self._drop(job.task_id, [job]))

        job.forget_kept = kept.on_lost(lost)

    async def discard(self, job: Job) -> None:
        """Forget a foreground job once it is done, with its files."""
        await job.done.wait()
        if self.jobs.get(job.task_id, {}).get(job.job_id) is job:
            del self.jobs[job.task_id][job.job_id]
            if not self.jobs[job.task_id]:
                self.jobs.pop(job.task_id, None)
        await self._remove_files(job.env, _files(job))

    def discard_later(self, job: Job) -> None:
        """:meth:`discard` without waiting for it."""
        self._in_background(self.discard(job))

    def _stop_quietly(self, job: Job) -> None:
        self.kill(job, quiet=True)

    def _fence(self, job: Job) -> None:
        """The job's claim is lost: the log will call it lost, so it stops
        and tells nothing more."""
        job.fenced = True
        self.kill(job, quiet=True)

    def kill_all(self, task_id: str) -> None:
        """Quietly kill the task's running jobs."""
        for job in self.running(task_id):
            self.kill(job, quiet=True)

    def retire(self, task_id: str) -> None:
        """The task ended: kill its jobs quietly, then drop them and their
        logs once they are gone."""
        self.kill_all(task_id)
        jobs = list(self.jobs.get(task_id, {}).values())
        if not jobs:
            self.jobs.pop(task_id, None)
            self._in_background(self._remove_let_go_logs(task_id))
            return
        self._in_background(self._drop(task_id, jobs))

    async def _drop(self, task_id: str, jobs: list[Job]) -> None:
        """Once ``jobs`` (of ``task_id``) are gone, forget them and remove
        their logs; a job started since stays."""
        for job in jobs:
            await job.done.wait()
        await self._remove_files_of(jobs)
        table = self.jobs.get(task_id, {})
        for job in jobs:
            if table.get(job.job_id) is job:
                del table[job.job_id]
        if task_id in self.jobs and not table:
            self.jobs.pop(task_id, None)
            await self._remove_let_go_logs(task_id)

    # ------------------------------------------------------------ notices

    async def _send(self, job: Job) -> None:
        """Deliver a job's notices in order."""
        while True:
            text = await job.outbox.get()
            if text is None:
                return
            if job.fenced:
                continue
            try:
                await job.post(Say(message=user(f"{NOTICE_GUARD}\n\n{text}", origin="system"),
                                   when="now"),
                               task_id=job.task_id)
            except Exception as exc:  # noqa: BLE001 - a lost notice must not kill the sender
                log.warning("background notice for %s not delivered: %s", job.job_id, exc)

    def _event(self, job: Job, text: str) -> None:
        job.outbox.put_nowait(
            monitor_event(task_id=job.job_id, description=job.description, text=text))

    def _expire(self, job: Job, timeout_ms: float) -> None:
        stream = job.monitor
        if stream is None or job.status != "running":
            return
        stream.flush()
        stream.note(monitor_expired_note(timeout_ms, stream.delivered))
        stream.close()
        self.kill(job, quiet=True)

    async def _tail(self, job: Job) -> None:
        while True:
            await asyncio.sleep(MONITOR_POLL_S)
            await self._read_new(job)

    async def _read_new(self, job: Job) -> None:
        stream = job.monitor
        if stream is None:
            return
        path = job.events_path or job.out_path
        try:
            st = await job.env.stat(path)
        except OSError:
            return
        size = st.size if st is not None else 0
        if size <= job.tailed:
            return
        data = await job.env.read_range(path, job.tailed, size - job.tailed)
        job.tailed += len(data)
        stream.feed(job.decoder.decode(data))

    async def _tail_text(self, env: ExecEnv, path: str, cap: int) -> str:
        try:
            st = await env.stat(path)
            size = st.size if st is not None else 0
            take = min(size, cap)
            data = await env.read_range(path, size - take, take) if take else b""
        except OSError:
            return ""
        return data.decode("utf-8", errors="replace")

    @staticmethod
    def status_of(job: Job) -> str:
        """``completed`` / ``failed`` / ``killed``, as the notice says. A
        ``Bash`` job whose exit status is an answer (``grep`` matching
        nothing) completed."""
        if job.status == "killed":
            return "killed"
        if job.ws is not None:
            ok = job.exit_code in NORMAL_CLOSES
        else:
            ok = job.exit_code == 0 or (job.monitor is None and background_exit_note(
                job.shell_command, job.exit_code) is not None)
        return "completed" if ok else "failed"

    async def _end_notice(self, job: Job) -> str | None:
        status = self.status_of(job)
        code = None if status == "killed" else job.exit_code
        stream = job.monitor
        if stream is None:
            if job.quiet:
                return None
            note = background_exit_note(job.shell_command, code) \
                if status == "completed" else None
            return task_notification(
                task_id=job.job_id, tool_use_id=job.tool_use_id, output_file=job.out_path,
                status=status, summary=shell_end_summary(job.description, status, code, note))
        rest = stream.drain()
        if job.quiet:
            return None
        summary = ws_end_summary(job.description, status, code) if job.ws is not None \
            else monitor_end_summary(job.description, status, code,
                                     produced_output=stream.saw_output)
        return task_notification(
            task_id=job.job_id, tool_use_id=job.tool_use_id, status=status, summary=summary,
            output_file=job.out_path if job.ws is None else "",
            body=event_body(rest) if rest else "")


#: The process-wide table every ``Shell`` extension shares unless
#: given its own (jobs belong to the worker process).
REGISTRY = Registry()
