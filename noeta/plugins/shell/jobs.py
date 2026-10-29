"""The process-local table of background jobs (``Bash`` with
``run_in_background`` and ``Monitor``), and of foreground ``Bash`` commands,
which run as jobs the call waits for so that one whose timeout runs out can
be moved to the background (it then takes its hold on the tree).

A job belongs to the worker process that started it and to the task tree's
root: the table is keyed by root task, the cap counts a root's running jobs,
and a stop of the root kills them all. A job runs through the task's
``ExecEnv`` with its output redirected to a log file inside that env, so the
same code serves the local machine and a container: the model reads the
file (its path is in the launch result and the end notice), a monitor tails
it, and stopping the job (``TaskStop``) is cancelling the env's run (which
kills its process group). A finished command's file ends with a blank line
and ``[exited with code N]``.

A monitor's events go to the task that started the job while that task is
live, else to the root, as system-origin messages behind the not-the-user
guard, posted with ``when="now"``: they land after the running batch's tool
results, and wake an idle task into a turn. A job's end goes the same way as
``External(JOB_ENDED)`` (its status and notice, posted once, before the job
gives its hold back): the task's runner records it ended and tells the model,
unless the log already says the job was lost (``noeta.plugins.shell``).

A job lives as long as its worker's hold on the task tree
(``ToolContext.hold``): when the hold is lost (reaped after the worker was
silent too long, or the worker stopping) the job is killed and its end never
posted; the tree is told by ``hold_lost`` instead. A job also dies when its
worker cannot kill it (killed itself, or frozen): a guard runs beside the
command, in its process group, and kills the group once the worker stops
re-creating the job's ``.alive`` file (it does while its hold is fresh, and
for a foreground command, which has no hold, while the worker runs) or, on
this machine, once the worker's process is gone. The guard needs only
``sh`` tools, so it works the same in a container or a sandbox.

Job logs live in a private directory per user inside the env: by default under
the temp directory on disk (``$TMPDIR``, else ``/var/tmp``, else ``/tmp``;
never the RAM-backed ``$XDG_RUNTIME_DIR``), in ``noeta-background-<uid>``,
created 0700; a directory that already exists must be a real directory owned
by the user (else launches are refused). An env with a ``temp_dir`` of its own
(a tenant's task) keeps them in its ``jobs/`` instead, out of other tenants'
reach. Log files are created 0600, in a directory of the worker process's own
(``w<pid>``); a worker on this machine removes those of workers that are gone,
a day after their last write, when it first prepares the directory (a lost
job's notice names its output file). A job's output reaches its log through a
pipe that keeps the first ``log_max_bytes`` (:data:`DEFAULT_LOG_MAX_BYTES`),
then a line saying the rest was not kept, and drains the rest, so the command
runs on unharmed.
"""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import logging
import os
import posixpath
import re
import secrets
import shlex
import shutil
import socket
import string
import time
import weakref
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from noeta.core.events import External, Say
from noeta.core.execenv import ExecEnv
from noeta.core.holds import TreeHold
from noeta.core.types import user
from noeta.plugins.shell.notices import (
    MonitorStream,
    event_body,
    monitor_end_summary,
    monitor_event,
    monitor_expired_note,
    shell_end_summary,
    task_notification,
    ws_end_summary,
)
from noeta.plugins.shell.websocket import NORMAL_CLOSES, run_ws
from noeta.toolkit.local_env import is_local
from noeta.toolkit.notices import NOTICE_GUARD

log = logging.getLogger("noeta.plugins.shell")

#: Per-root ceiling on RUNNING jobs; a launch over it is refused, not queued.
DEFAULT_MAX_JOBS = 8
#: Job ids: nine lower-case letters and digits, as Claude Code's.
_ID_CHARS = string.ascii_lowercase + string.digits
#: Characters of a monitor's stderr its end notice carries.
STDERR_TAIL_CHARS = 2_000
#: How often a monitor's log is read for new lines.
MONITOR_POLL_S = 0.1
#: Where job logs live inside the env: empty = the per-user private default
#: (``<runtime or temp dir>/noeta-background-<uid>``).
DEFAULT_LOG_DIR = ""
#: Tasks remembered as ended (their notices go to the root) at most.
ENDED_KEEP = 4096
#: Background jobs of a root kept in memory once their end is posted (for
#: ``TaskStop`` / ``TaskOutput`` on them) at most; older ones are let go,
#: their logs removed with the root's.
ENDED_JOBS_KEEP = 64
#: How long the logs of a worker that is gone stay after their last write:
#: the notice of a job it lost names its output file.
DEAD_WORKER_LOGS_KEEP_S = 24 * 3600
#: How often a job's guard looks at its worker.
GUARD_TICK_S = 0.5
#: How long a foreground command outlives a worker that stopped re-creating
#: its ``.alive`` file (it has no hold to time it by).
FOREGROUND_ALIVE_S = 30.0
#: Bytes of a job's output (and, for a monitor, of its stderr) its log keeps.
DEFAULT_LOG_MAX_BYTES = 64 * 1024 * 1024

#: Appends its stdin to the file ``$2`` as it comes (one read at a time, so
#: a monitor sees each line at once) until the file holds ``$1`` bytes (one
#: read more at most), then a line saying the rest was not kept, and reads
#: the rest away: the writer never gets a broken pipe. Only ``sh`` tools.
_KEEP_HEAD = r"""keep_head() {
  s=0
  while [ "$s" -lt "$1" ]; do
    dd bs=65536 count=1 2>/dev/null >>"$2"
    t=$(wc -c <"$2")
    [ "$t" -gt "$s" ] || return 0
    s=$t
  done
  if [ "$(dd bs=1 count=1 2>/dev/null | wc -c)" -gt 0 ]; then
    printf '\n[output past %s bytes was not kept]\n' "$1" >>"$2"
    cat >/dev/null
  fi
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

#: This worker process, recorded with a job that has no hold (no runner:
#: a test, a host running tools itself); a held job records its hold's worker.
WORKER_ID = f"{socket.gethostname()}:{os.getpid()}"
#: The ``External`` a job's end is posted as: ``data = {"job_id", "status",
#: "exit_code", "notice"}`` (``notice`` is ``None`` for a job stopped quietly).
JOB_ENDED = "shell_job_ended"

#: Makes (or checks) the log directory: ``$1`` an explicit directory, else
#: ``<temp>/noeta-background-<uid>``, ``<temp>`` the first usable of ``$2``,
#: ``$TMPDIR``, ``/var/tmp`` and ``/tmp``; in it, this worker's own directory
#: ``$3``. Prints that one.
_PREPARE_DIR = r"""
d=$1
if [ -z "$d" ]; then
  b=/tmp
  for c in "$2" "${TMPDIR:-}" /var/tmp; do
    if [ -n "$c" ] && [ -d "$c" ] && [ -w "$c" ]; then b=$c; break; fi
  done
  d="$b/noeta-background-$(id -u)"
fi
if [ ! -e "$d" ] && [ ! -L "$d" ]; then (umask 077 && mkdir -p -- "$d") || exit 3; fi
if [ -L "$d" ] || [ ! -d "$d" ] || [ ! -O "$d" ]; then
  echo "refusing $d: not a directory owned by the current user" >&2; exit 4
fi
chmod 700 -- "$d" || exit 5
if [ ! -e "$d/$3" ]; then mkdir -m 700 -- "$d/$3" || exit 6; fi
if [ -L "$d/$3" ] || [ ! -d "$d/$3" ]; then echo "refusing $d/$3: not a directory" >&2; exit 4; fi
printf '%s' "$d/$3"
"""


#: ``(body, task_id)`` posts an inbox message to a task.
Post = Callable[..., Awaitable[None]]
#: Gives back the job's hold on its task tree (``ToolContext.hold``).
Release = Callable[[], Awaitable[None]]


@dataclass(eq=False)
class Job:
    job_id: str
    root_id: str
    spawner_id: str
    command: str
    description: str
    tool_use_id: str
    env: ExecEnv
    out_path: str
    err_path: str | None
    post: Post
    release: Release | None = None
    #: The worker's hold on the tree, while the job runs.
    tree: TreeHold | None = None
    #: The worker recorded with the job (its hold's).
    worker: str = WORKER_ID
    #: Its hold was lost: it was killed and its end is never posted.
    fenced: bool = False
    #: The file the worker re-creates while the job may live (its guard
    #: kills the job once it stays gone).
    alive_path: str = ""
    #: How often its guard checks that file, fixed when the guard started
    #: (a later hold does not change it).
    guard_period: float = 0.0
    status: str = "running"  # running | exited | killed
    exit_code: int | None = None
    #: A ``Bash`` command waited on in the foreground: it ends without a
    #: notice and leaves its files to the call (:meth:`Registry.discard`),
    #: until :meth:`Registry.to_background` moves it to the background.
    foreground: bool = False
    #: Where a foreground command's shell writes its final directory (fd 3).
    cwd_path: str = ""
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
    done: asyncio.Event = field(default_factory=asyncio.Event)
    outbox: asyncio.Queue[str | None] = field(default_factory=asyncio.Queue)


_WORKER_DIR_NAME = re.compile(r"w([0-9]+)")


def _worker_dir() -> str:
    """This worker process's own directory inside the log directory (asked
    each time: a forked worker is a process of its own)."""
    return f"w{os.getpid()}"


def _remove_dead_workers(logs: str) -> None:
    """Remove the directories of worker processes on this machine that are
    gone (``w<pid>`` inside ``logs``) once nothing in them was written for
    :data:`DEAD_WORKER_LOGS_KEEP_S`; a pid that answers is kept."""
    cutoff = time.time() - DEAD_WORKER_LOGS_KEEP_S
    with contextlib.suppress(OSError), os.scandir(logs) as entries:
        for entry in entries:
            found = _WORKER_DIR_NAME.fullmatch(entry.name)
            if found is None or entry.name == _worker_dir() \
                    or not entry.is_dir(follow_symlinks=False):
                continue
            try:
                os.kill(int(found.group(1)), 0)
                continue
            except ProcessLookupError:
                pass
            except (OSError, OverflowError):
                continue
            if _last_written(entry.path) < cutoff:
                shutil.rmtree(entry.path, ignore_errors=True)


def _last_written(directory: str) -> float:
    """When ``directory`` or a file directly in it was last written."""
    newest = 0.0
    with contextlib.suppress(OSError):
        newest = os.stat(directory).st_mtime
        with os.scandir(directory) as entries:
            for entry in entries:
                with contextlib.suppress(OSError):
                    newest = max(newest, entry.stat(follow_symlinks=False).st_mtime)
    return newest


def _touch(paths: Sequence[str]) -> None:
    for path in paths:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
        os.close(fd)


def _label(text: str) -> str:
    return " ".join(text.split())


def _files(job: Job) -> list[str]:
    """The job's files in its env."""
    return [p for p in (job.out_path, job.err_path, job.alive_path, job.cwd_path) if p]


class Registry:
    """Background jobs of this process, by root task."""

    def __init__(self, *, max_jobs: int = DEFAULT_MAX_JOBS,
                 log_dir: str = DEFAULT_LOG_DIR,
                 log_max_bytes: int = DEFAULT_LOG_MAX_BYTES,
                 foreground_alive_s: float = FOREGROUND_ALIVE_S,
                 ended_jobs_keep: int = ENDED_JOBS_KEEP) -> None:
        self.max_jobs = max_jobs
        self.ended_jobs_keep = ended_jobs_keep
        self.foreground_alive_s = foreground_alive_s
        self.log_dir = log_dir
        self.log_max_bytes = log_max_bytes
        self.worker = WORKER_ID
        self.jobs: dict[str, dict[str, Job]] = {}
        #: Tasks known (in this process) to have ended with jobs of their
        #: tree still here: their notices go to the root instead. Ordered,
        #: oldest first.
        self.ended: dict[str, None] = {}
        #: Log files of ended jobs let go of, by root: removed with its logs.
        self._let_go: dict[str, list[tuple[ExecEnv, list[str]]]] = {}
        self._retiring: set[asyncio.Task[None]] = set()
        self._dirs: weakref.WeakKeyDictionary[Any, str] = weakref.WeakKeyDictionary()
        self._keeper: asyncio.Task[None] | None = None

    def mark_ended(self, task_id: str, root_id: str) -> None:
        """``task_id`` ended: remembered only while its tree has jobs here
        (and at most :data:`ENDED_KEEP` tasks)."""
        if not self.jobs.get(root_id):
            return
        self.ended.pop(task_id, None)
        while len(self.ended) >= ENDED_KEEP:
            del self.ended[next(iter(self.ended))]
        self.ended[task_id] = None

    async def _log_dir(self, env: ExecEnv) -> str:
        """The env's private log directory, made or checked once per env."""
        try:
            known = self._dirs.get(env)
        except TypeError:
            known = None
        if known is not None:
            return known
        # The env's own variables may be scrubbed: on this machine, ours.
        base = (os.environ.get("TMPDIR") or "") if is_local(env) else ""
        # An env with a temporary directory of its own (a tenant's task)
        # keeps its jobs' logs there, away from other tenants' tasks.
        own = getattr(env, "temp_dir", None)
        logs = posixpath.join(own, "jobs") if isinstance(own, str) and own else self.log_dir
        result = await env.run(["sh", "-c", _PREPARE_DIR, "sh", logs, base,
                                _worker_dir()], timeout=10, output_cap=8192)
        if result.exit_code != 0:
            raise OSError(result.stderr.decode("utf-8", "replace").strip()
                          or "could not prepare the background log directory")
        path = result.stdout.decode("utf-8", "replace")
        if is_local(env):
            # A worker that died left its jobs' logs: nobody reads them now.
            await asyncio.to_thread(_remove_dead_workers, posixpath.dirname(path))
        with contextlib.suppress(TypeError):
            self._dirs[env] = path
        return path

    # ------------------------------------------------------------ lookup

    def find(self, root_id: str, job_id: str) -> Job | None:
        return self.jobs.get(root_id, {}).get(job_id)

    def get(self, job_id: str) -> Job | None:
        for jobs in self.jobs.values():
            if job_id in jobs:
                return jobs[job_id]
        return None

    def running(self, root_id: str) -> list[Job]:
        """The root's jobs whose end is not yet told: a job whose command
        has exited counts until its end is posted, so whoever waits for the
        tree's jobs to finish sees that end in the inbox once they have."""
        return [j for j in self.jobs.get(root_id, {}).values() if not j.done.is_set()]

    # ------------------------------------------------------------ launch

    async def spawn(self, *, env: ExecEnv, command: str, cwd: str, env_vars: Mapping[str, str],
                    root_id: str, spawner_id: str, tool_use_id: str, description: str,
                    post: Post, monitor_timeout_ms: float | None = None,
                    release: Release | None = None, tree: TreeHold | None = None,
                    argv: Sequence[str] | None = None,
                    ws: tuple[str, list[str]] | None = None,
                    ws_resolve: Callable[[str, int], Awaitable[str]] | None = None,
                    foreground: bool = False) -> Job | str:
        """Start a job, or return the refusal text. A ``foreground`` job
        (:attr:`Job.foreground`) also gets a file its fd 3 writes to. ``release`` is awaited
        once the job has ended and its end is posted (or it was refused);
        ``tree`` is the hold it gives back, whose loss kills the job.
        ``argv``: what runs instead
        of ``bash -c command`` (the command wrapped in a sandbox); the log
        redirection stays outside it. A monitor with ``ws`` streams that
        WebSocket's messages instead of running ``command``, connecting where
        ``ws_resolve`` says (``run_ws``'s ``resolve``)."""
        job = await self._spawn(env=env, command=command, cwd=cwd, env_vars=env_vars,
                                root_id=root_id, spawner_id=spawner_id,
                                tool_use_id=tool_use_id, description=description, post=post,
                                monitor_timeout_ms=monitor_timeout_ms, release=release,
                                tree=tree, argv=argv, ws=ws, ws_resolve=ws_resolve,
                                foreground=foreground)
        if isinstance(job, str) and release is not None:
            await release()
        return job

    async def _spawn(self, *, env: ExecEnv, command: str, cwd: str, env_vars: Mapping[str, str],
                     root_id: str, spawner_id: str, tool_use_id: str, description: str,
                     post: Post, monitor_timeout_ms: float | None,
                     release: Release | None, tree: TreeHold | None,
                     argv: Sequence[str] | None,
                     ws: tuple[str, list[str]] | None = None,
                     ws_resolve: Callable[[str, int], Awaitable[str]] | None = None,
                     foreground: bool = False) -> Job | str:
        running = len(self.running(root_id))
        if running >= self.max_jobs:
            return (f"too many background jobs ({running}/{self.max_jobs}); stop one with "
                    f"TaskStop first")
        job_id = "".join(secrets.choice(_ID_CHARS) for _ in range(9))
        try:
            log_dir = await self._log_dir(env)
        except OSError as exc:
            return f"background jobs are unavailable: {exc}"
        out = posixpath.join(log_dir, f"{job_id}.output")
        err = posixpath.join(log_dir, f"{job_id}.err") if monitor_timeout_ms else None
        alive = (posixpath.join(log_dir, f"{job_id}.alive")
                 if tree is not None or foreground else "")
        cwd_file = posixpath.join(log_dir, f"{job_id}.cwd") if foreground else ""
        made = await env.run(
            ["sh", "-c", 'umask 077 && for f; do : > "$f" || exit 1; done', "sh",
             *[p for p in (out, err, alive, cwd_file) if p]], timeout=10)
        if made.exit_code != 0:
            return "background jobs are unavailable: could not create the job's log file"
        job = Job(job_id=job_id, root_id=root_id, spawner_id=spawner_id, command=command,
                  description=_label(description or command), tool_use_id=tool_use_id,
                  env=env, out_path=out, err_path=err, post=post, release=release, ws=ws,
                  tree=tree, worker=tree.worker if tree is not None else self.worker,
                  alive_path=alive, foreground=foreground, cwd_path=cwd_file)
        if monitor_timeout_ms is not None:
            job.monitor = MonitorStream(sink=lambda text, _note: self._event(job, text),
                                        stop=lambda: self._stop_quietly(job))
        self.jobs.setdefault(root_id, {})[job_id] = job
        if ws is not None and job.monitor is not None:
            stream = job.monitor
            job.proc = asyncio.ensure_future(run_ws(*ws, lambda text: stream.feed(text + "\n"),
                                                    resolve=ws_resolve))
        else:
            cap = str(self.log_max_bytes)
            keep_out = f"keep_head {cap} {shlex.quote(out)}"
            if err is not None:
                # stdout and stderr each through their own pipe: both are
                # members of the pipeline, so both are written out before
                # the shell exits.
                pipeline = (f'{{ "$@" 2>&1 1>&4 4>&- | keep_head {cap} {shlex.quote(err)}; '
                            f'exit "${{PIPESTATUS[0]}}"; }} 4>&1 | {keep_out}')
            else:
                pipeline = f'"$@" 2>&1 | {keep_out}'
            fd3 = f"exec 3>{shlex.quote(cwd_file)}\n" if cwd_file else ""
            # The command runs in the pipeline, the guard beside it: a
            # ``wait`` in the command never waits for the guard.
            script = (f"{fd3}{_KEEP_HEAD}"
                      f"{self._guard(job)} </dev/null >/dev/null 2>&1 3>&- &\n"
                      f'{pipeline}\nexit "${{PIPESTATUS[0]}}"')
            run = ["bash", "-c", script, "bash", *(argv or ["bash", "-c", command])]
            job.proc = asyncio.ensure_future(env.run(run, cwd=cwd,
                                                     env=dict(env_vars), timeout=None,
                                                     output_cap=4096))
        job.task = asyncio.create_task(self._run(job, monitor_timeout_ms))
        job.sender = asyncio.create_task(self._send(job))
        if tree is not None:
            tree.on_lost(lambda: self._fence(job))
        if alive and (self._keeper is None or self._keeper.done()):
            self._keeper = asyncio.create_task(self._keep_alive())
        return job

    def _alive_period(self, job: Job) -> float:
        """How long ``job``'s guard waits for its ``.alive`` file: half its
        hold's time, or for a foreground command :attr:`foreground_alive_s`."""
        return job.tree.seconds / 2 if job.tree is not None else self.foreground_alive_s

    def _guard(self, job: Job) -> str:
        """The guard's command line for ``job`` (see :data:`_GUARD`)."""
        worker_pid = str(os.getpid()) if is_local(job.env) else ""
        ticks = 0
        if job.alive_path:
            # The worker re-creates the file three times per period; the
            # period is half the hold's time, so a frozen worker's jobs are
            # dead about when its hold may be reaped.
            ticks = max(2, round(self._alive_period(job) / GUARD_TICK_S))
        job.guard_period = ticks * GUARD_TICK_S
        return shlex.join(["sh", "-c", _GUARD, "sh", "$$", worker_pid, job.alive_path,
                           str(ticks), str(GUARD_TICK_S)]).replace("'$$'", "$$")

    async def _keep_alive(self) -> None:
        """Re-create the ``.alive`` file of every running job whose hold is
        fresh (or that has none: a foreground command), three times per
        guard period, until no such job is left."""
        while True:
            jobs = [j for root in self.jobs.values() for j in root.values()
                    if j.status == "running" and j.alive_path]
            if not jobs:
                return
            by_env: dict[int, list[Job]] = {}
            for job in jobs:
                if (job.tree is None or job.tree.fresh()) and not job.fenced:
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
            await asyncio.sleep(max(period / 3, GUARD_TICK_S / 2))

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
            if expiry is not None:
                expiry.cancel()
            if tail is not None:
                tail.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await tail
            if job.foreground:
                # The call waiting for it reports the end itself.
                job.outbox.put_nowait(None)
                job.done.set()
            else:
                await self._end(job)

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
                # Posted before the hold is given back: a worker that
                # claims the tree next finds the end in the inbox.
                await self._post_end(job, notice)
            finally:
                job.done.set()
                if job.release is not None:
                    with contextlib.suppress(Exception):
                        await job.release()
                self._let_go_of_ended(job.root_id)

    def _let_go_of_ended(self, root_id: str) -> None:
        """Keep at most :attr:`ended_jobs_keep` of the root's background jobs
        whose end is posted, dropping the oldest (their logs stay until the
        root's go)."""
        jobs = self.jobs.get(root_id)
        if not jobs:
            return
        ended = [j for j in jobs.values() if j.done.is_set() and not j.foreground]
        for job in ended[:max(0, len(ended) - self.ended_jobs_keep)]:
            del jobs[job.job_id]
            self._let_go.setdefault(root_id, []).append((job.env, _files(job)))

    async def _post_end(self, job: Job, notice: str | None) -> None:
        if job.fenced:
            return  # its hold is gone: the tree was told it is lost
        data = {"job_id": job.job_id, "status": self.status_of(job), "exit_code": job.exit_code,
                "notice": f"{NOTICE_GUARD}\n\n{notice}" if notice is not None else None}
        try:
            # Forwarded to the root by the store when the spawner ended
            # after ``_target`` looked (this process marks ends late).
            await job.post(External(name=JOB_ENDED, data=data), task_id=self._target(job),
                           dedupe_key=f"{JOB_ENDED}:{job.job_id}", forward_if_ended=True)
        except Exception as exc:  # noqa: BLE001 - its hold runs out and the tree is told
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
        return True

    def hold_with(self, job: Job, tree: TreeHold) -> None:
        """A job moved to the background lives by ``tree`` from now on."""
        job.tree, job.worker = tree, tree.worker
        tree.on_lost(lambda: self._fence(job))

    async def discard(self, job: Job) -> None:
        """Forget a foreground job once it is done, with its files."""
        await job.done.wait()
        if self.jobs.get(job.root_id, {}).get(job.job_id) is job:
            del self.jobs[job.root_id][job.job_id]
            if not self.jobs[job.root_id]:
                self.jobs.pop(job.root_id, None)
        paths = [p for p in (job.out_path, job.err_path, job.alive_path, job.cwd_path) if p]
        with contextlib.suppress(Exception):
            await job.env.run(["rm", "-f", *paths], timeout=10)

    def discard_later(self, job: Job) -> None:
        """:meth:`discard` without waiting for it."""
        task = asyncio.create_task(self.discard(job))
        self._retiring.add(task)
        task.add_done_callback(self._retiring.discard)

    def _stop_quietly(self, job: Job) -> None:
        self.kill(job, quiet=True)

    def _fence(self, job: Job) -> None:
        """The job's hold is lost: the log will call it lost, so it stops
        and tells nothing more."""
        job.fenced = True
        self.kill(job, quiet=True)

    def kill_where(self, root_id: str, *, spawner_id: str | None = None) -> None:
        """Quietly kill the running jobs of a root (or of one spawner)."""
        for job in self.running(root_id):
            if spawner_id is None or job.spawner_id == spawner_id:
                self.kill(job, quiet=True)

    def retire(self, root_id: str) -> None:
        """The root ended: kill its jobs quietly, then drop them and their
        logs once they are gone."""
        self.kill_where(root_id)
        jobs = list(self.jobs.get(root_id, {}).values())
        if not jobs and root_id not in self._let_go:
            self.jobs.pop(root_id, None)
            return
        task = asyncio.create_task(self._drop(root_id, jobs))
        self._retiring.add(task)
        task.add_done_callback(self._retiring.discard)

    async def _drop(self, root_id: str, jobs: list[Job]) -> None:
        for job in jobs:
            await job.done.wait()
        # A job let go of while they ended is in ``jobs`` too.
        files = [*self._let_go.pop(root_id, []), *((job.env, _files(job)) for job in jobs)]
        for env, paths in files:
            with contextlib.suppress(Exception):
                await env.run(["rm", "-f", *paths], timeout=10)
        for job in jobs:
            self.ended.pop(job.spawner_id, None)
        self._let_go.pop(root_id, None)
        self.ended.pop(root_id, None)
        if self.jobs.get(root_id) and all(j in jobs for j in self.jobs[root_id].values()):
            self.jobs.pop(root_id, None)

    # ------------------------------------------------------------ notices

    def _target(self, job: Job) -> str:
        return job.root_id if job.spawner_id in self.ended else job.spawner_id

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
                               task_id=self._target(job), forward_if_ended=True)
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
        try:
            st = await job.env.stat(job.out_path)
        except OSError:
            return
        size = st.size if st is not None else 0
        if size <= job.tailed:
            return
        data = await job.env.read_range(job.out_path, job.tailed, size - job.tailed)
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
        """``completed`` / ``failed`` / ``killed``, as the notice says."""
        if job.status == "killed":
            return "killed"
        ok = job.exit_code in NORMAL_CLOSES if job.ws is not None else job.exit_code == 0
        return "completed" if ok else "failed"

    async def _end_notice(self, job: Job) -> str | None:
        status = self.status_of(job)
        code = None if status == "killed" else job.exit_code
        stream = job.monitor
        if stream is None:
            if job.quiet:
                return None
            return task_notification(
                task_id=job.job_id, tool_use_id=job.tool_use_id, output_file=job.out_path,
                status=status, summary=shell_end_summary(job.description, status, code))
        rest = stream.drain()
        if job.quiet:
            return None
        stderr = ""
        if job.err_path is not None:
            stderr = (await self._tail_text(job.env, job.err_path, 4 * STDERR_TAIL_CHARS))
            stderr = stderr[-STDERR_TAIL_CHARS:].rstrip()
        parts = [rest] if rest else []
        if stderr:
            parts.append(f"stderr:\n{stderr}")
        summary = ws_end_summary(job.description, status, code) if job.ws is not None \
            else monitor_end_summary(job.description, status, code,
                                     produced_output=stream.saw_output)
        return task_notification(
            task_id=job.job_id, tool_use_id=job.tool_use_id, status=status,
            summary=summary,
            body=event_body("\n".join(parts)) if parts else "")


#: The process-wide table every ``Shell`` extension shares unless
#: given its own (jobs belong to the worker process).
REGISTRY = Registry()
