"""The process-local table of background jobs (``Bash`` with
``run_in_background`` and ``Monitor``).

A job belongs to the worker process that started it and to the task tree's
root: the table is keyed by root task, the cap counts a root's running jobs,
and a stop of the root kills them all. A job runs through the task's
``ExecEnv`` with its output redirected to a log file inside that env, so the
same code serves the local machine and a container: ``BashOutput`` reads the
file, a monitor tails it, and killing the job is cancelling the env's run
(which kills its process group).

When a job ends — or a monitor emits an event — the notice goes to the task
that started it while that task is live, else to the root, as a
system-origin message posted with ``when="now"``: it lands after the running
batch's tool results, and wakes an idle task into a turn.

Job logs live in a private directory per user inside the env: by default
``$XDG_RUNTIME_DIR`` (local env) or the temp directory, under
``noeta-background-<uid>``, created 0700; a directory that already exists
must be a real directory owned by the user (else launches are refused). Log
files are created 0600.
"""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import logging
import os
import posixpath
import shlex
import socket
import tempfile
import uuid
import weakref
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from noeta.core.events import Say
from noeta.core.execenv import ExecEnv, is_local
from noeta.core.types import user
from noeta.plugins.shell.notices import (
    MonitorStream,
    event_body,
    monitor_end_summary,
    monitor_event,
    monitor_expired_note,
    shell_end_summary,
    task_notification,
)

log = logging.getLogger("noeta.plugins.shell")

#: Per-root ceiling on RUNNING jobs; a launch over it is refused, not queued.
DEFAULT_MAX_JOBS = 8
#: Bytes of a job's output kept for a poll or a notice (the tail survives).
DEFAULT_OUTPUT_CAP = 256 * 1024
#: Characters of a finished command's output its notice carries.
NOTICE_TAIL_CHARS = 30_000
#: Characters of a monitor's stderr its end notice carries.
STDERR_TAIL_CHARS = 2_000
#: How often a monitor's log is read for new lines.
MONITOR_POLL_S = 0.1
#: Where job logs live inside the env: empty = the per-user private default
#: (``<runtime or temp dir>/noeta-background-<uid>``).
DEFAULT_LOG_DIR = ""
#: Tasks remembered as ended (their notices go to the root) at most.
ENDED_KEEP = 4096

#: This worker process: recorded with each job so another process that picks
#: the task up can tell a job running elsewhere from a lost one.
WORKER_ID = f"{socket.gethostname()}:{os.getpid()}"

#: Makes (or checks) the log directory: ``$1`` an explicit directory, else
#: ``${2:-/tmp}/noeta-background-<uid>``. Prints the directory.
_PREPARE_DIR = r"""
d=$1
[ -n "$d" ] || d="${2:-/tmp}/noeta-background-$(id -u)"
if [ ! -e "$d" ] && [ ! -L "$d" ]; then mkdir -p -m 700 -- "$d" || exit 3; fi
if [ -L "$d" ] || [ ! -d "$d" ] || [ ! -O "$d" ]; then
  echo "refusing $d: not a directory owned by the current user" >&2; exit 4
fi
chmod 700 -- "$d" || exit 5
printf '%s' "$d"
"""


def worker_alive(worker: str) -> bool:
    """Whether the worker process ``host:pid`` may still be running: on
    this host the pid is checked, another host's is assumed alive."""
    host, _, pid = worker.rpartition(":")
    if host != socket.gethostname() or not pid.isdigit():
        return True
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True

#: ``(body, task_id)`` posts an inbox message to a task.
Post = Callable[..., Awaitable[None]]


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
    status: str = "running"  # running | exited | killed
    exit_code: int | None = None
    #: Output bytes already handed to ``BashOutput``.
    polled: int = 0
    #: Output bytes a monitor has already turned into events.
    tailed: int = 0
    decoder: Any = field(
        default_factory=lambda: codecs.getincrementaldecoder("utf-8")(errors="replace"))
    #: No end notice: the job ended with its turn or task, or a monitor
    #: already sent its last word.
    quiet: bool = False
    monitor: MonitorStream | None = None
    #: The env's run of the command; cancelling it kills the job.
    proc: asyncio.Task[Any] | None = None
    task: asyncio.Task[None] | None = None
    sender: asyncio.Task[None] | None = None
    done: asyncio.Event = field(default_factory=asyncio.Event)
    outbox: asyncio.Queue[str | None] = field(default_factory=asyncio.Queue)


def _label(text: str) -> str:
    return " ".join(text.split())


class Registry:
    """Background jobs of this process, by root task."""

    def __init__(self, *, max_jobs: int = DEFAULT_MAX_JOBS,
                 output_cap: int = DEFAULT_OUTPUT_CAP, log_dir: str = DEFAULT_LOG_DIR) -> None:
        self.max_jobs = max_jobs
        self.output_cap = output_cap
        self.log_dir = log_dir
        self.worker = WORKER_ID
        self.jobs: dict[str, dict[str, Job]] = {}
        #: Tasks known (in this process) to have ended with jobs of their
        #: tree still here: their notices go to the root instead.
        self.ended: set[str] = set()
        self._retiring: set[asyncio.Task[None]] = set()
        self._dirs: weakref.WeakKeyDictionary[Any, str] = weakref.WeakKeyDictionary()

    def mark_ended(self, task_id: str, root_id: str) -> None:
        """``task_id`` ended: remembered only while its tree has jobs here
        (and at most :data:`ENDED_KEEP` tasks)."""
        if not self.jobs.get(root_id):
            return
        if len(self.ended) >= ENDED_KEEP:
            self.ended.pop()
        self.ended.add(task_id)

    async def _log_dir(self, env: ExecEnv) -> str:
        """The env's private log directory, made or checked once per env."""
        try:
            known = self._dirs.get(env)
        except TypeError:
            known = None
        if known is not None:
            return known
        base = "/tmp"
        if is_local(env):
            runtime = os.environ.get("XDG_RUNTIME_DIR") or ""
            base = runtime if runtime and os.path.isdir(runtime) else tempfile.gettempdir()
        result = await env.run(["sh", "-c", _PREPARE_DIR, "sh", self.log_dir, base],
                               timeout=10, output_cap=8192)
        if result.exit_code != 0:
            raise OSError(result.stderr.decode("utf-8", "replace").strip()
                          or "could not prepare the background log directory")
        path = result.stdout.decode("utf-8", "replace")
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
        return [j for j in self.jobs.get(root_id, {}).values() if j.status == "running"]

    # ------------------------------------------------------------ launch

    async def spawn(self, *, env: ExecEnv, command: str, cwd: str, env_vars: Mapping[str, str],
                    root_id: str, spawner_id: str, tool_use_id: str, description: str,
                    post: Post, monitor_timeout_ms: float | None = None) -> Job | str:
        """Start a job, or return the refusal text."""
        running = len(self.running(root_id))
        if running >= self.max_jobs:
            return (f"too many background jobs ({running}/{self.max_jobs}); kill one with "
                    f"KillShell first")
        job_id = f"bg-{uuid.uuid4().hex[:12]}"
        try:
            log_dir = await self._log_dir(env)
        except OSError as exc:
            return f"background jobs are unavailable: {exc}"
        out = posixpath.join(log_dir, f"{job_id}.out")
        err = posixpath.join(log_dir, f"{job_id}.err") if monitor_timeout_ms else None
        made = await env.run(
            ["sh", "-c", 'umask 077 && for f; do : > "$f" || exit 1; done', "sh",
             *[p for p in (out, err) if p]], timeout=10)
        if made.exit_code != 0:
            return "background jobs are unavailable: could not create the job's log file"
        job = Job(job_id=job_id, root_id=root_id, spawner_id=spawner_id, command=command,
                  description=_label(description or command), tool_use_id=tool_use_id,
                  env=env, out_path=out, err_path=err, post=post)
        if monitor_timeout_ms is not None:
            job.monitor = MonitorStream(sink=lambda text, _note: self._event(job, text),
                                        stop=lambda: self._stop_quietly(job))
        self.jobs.setdefault(root_id, {})[job_id] = job
        redirect = f"2>{shlex.quote(err)}" if err is not None else "2>&1"
        script = f"exec >{shlex.quote(out)} {redirect}\n{command}"
        job.proc = asyncio.ensure_future(env.run(["bash", "-c", script], cwd=cwd,
                                                 env=dict(env_vars), timeout=None,
                                                 output_cap=4096))
        job.task = asyncio.create_task(self._run(job, monitor_timeout_ms))
        job.sender = asyncio.create_task(self._send(job))
        return job

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
            try:
                await self._finish(job)
            finally:
                job.done.set()
                job.outbox.put_nowait(None)

    async def _finish(self, job: Job) -> None:
        if job.monitor is not None:
            await self._read_new(job)  # the last lines before the exit
        notice = await self._end_notice(job)
        if notice is not None:
            job.outbox.put_nowait(notice)

    # ------------------------------------------------------------ control

    def kill(self, job: Job, *, quiet: bool = False) -> str:
        """Stop a job (SIGTERM, then SIGKILL after the env's grace). Returns
        the status KillShell reports."""
        if job.status != "running":
            return job.status
        if quiet:
            job.quiet = True
        if job.proc is not None:
            job.proc.cancel()
        return "killing"

    def _stop_quietly(self, job: Job) -> None:
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
        if not jobs:
            self.jobs.pop(root_id, None)
            return
        task = asyncio.create_task(self._drop(root_id, jobs))
        self._retiring.add(task)
        task.add_done_callback(self._retiring.discard)

    async def _drop(self, root_id: str, jobs: list[Job]) -> None:
        for job in jobs:
            await job.done.wait()
        for job in jobs:
            paths = [p for p in (job.out_path, job.err_path) if p]
            with contextlib.suppress(Exception):
                await job.env.run(["rm", "-f", *paths], timeout=10)
            self.ended.discard(job.spawner_id)
        self.ended.discard(root_id)
        if self.jobs.get(root_id) and all(j in jobs for j in self.jobs[root_id].values()):
            self.jobs.pop(root_id, None)

    async def poll(self, job: Job) -> tuple[str, int]:
        """The output since the last poll (its last ``output_cap`` bytes) and
        how many older undelivered bytes were dropped."""
        st = await job.env.stat(job.out_path)
        size = st.size if st is not None else 0
        undelivered = max(0, size - job.polled)
        take = min(undelivered, self.output_cap)
        data = await job.env.read_range(job.out_path, size - take, take) if take else b""
        job.polled = size
        return data.decode("utf-8", errors="replace"), undelivered - take

    # ------------------------------------------------------------ notices

    def _target(self, job: Job) -> str:
        return job.root_id if job.spawner_id in self.ended else job.spawner_id

    async def _send(self, job: Job) -> None:
        """Deliver a job's notices in order."""
        while True:
            text = await job.outbox.get()
            if text is None:
                return
            try:
                await job.post(Say(message=user(text, origin="system"), when="now"),
                               task_id=self._target(job))
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

    async def _end_notice(self, job: Job) -> str | None:
        killed = job.status == "killed"
        code = None if killed else job.exit_code
        status = "killed" if killed else ("completed" if code == 0 else "failed")
        stream = job.monitor
        if stream is None:
            if job.quiet:
                return None
            tail = (await self._tail_text(job.env, job.out_path, self.output_cap)).rstrip()
            if len(tail) > NOTICE_TAIL_CHARS:
                tail = "[… earlier output omitted]\n" + tail[-NOTICE_TAIL_CHARS:]
            return task_notification(
                task_id=job.job_id, tool_use_id=job.tool_use_id, status=status,
                summary=shell_end_summary(job.description, status, code),
                trailing=f"\nLast output:\n{tail}" if tail else "")
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
        return task_notification(
            task_id=job.job_id, tool_use_id=job.tool_use_id, status=status,
            summary=monitor_end_summary(job.description, status, code,
                                        produced_output=stream.saw_output),
            body=event_body("\n".join(parts)) if parts else "")


#: The process-wide table every ``Shell`` extension shares unless
#: given its own (jobs belong to the worker process).
REGISTRY = Registry()
