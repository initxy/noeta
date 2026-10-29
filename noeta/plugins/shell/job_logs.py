"""Where background job logs (``jobs.py``) live: a private directory per user
inside the env, by default under the temp directory on disk (``$TMPDIR``,
else ``/var/tmp``, else ``/tmp``; never the RAM-backed
``$XDG_RUNTIME_DIR``), in ``noeta-background-<uid>``, created 0700; a
directory that already exists must be a real directory owned by the user
(else launches are refused). Log files are created 0600, in a directory of the worker process's
own (``w<pid>``); a worker on this machine removes those of workers that are
gone, a day after their last write, when it first prepares the directory (a
lost job's notice names its output file). In an env that is not this
machine (a container) those are left: workers on other machines may share
that env, so a ``w<pid>`` there cannot be told gone; they go with the env.
A live worker removes a job's files when it lets go of the job
(``Registry.ended_jobs_keep``) or its root ends.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import posixpath
import re
import shutil
import time

from noeta.core.execenv import ExecEnv
from noeta.toolkit.local_env import is_local

#: Where job logs live inside the env: empty = the per-user private default
#: (``<runtime or temp dir>/noeta-background-<uid>``).
DEFAULT_LOG_DIR = ""

#: How long the logs of a worker that is gone stay after their last write:
#: the notice of a job it lost names its output file.
DEAD_WORKER_LOGS_KEEP_S = 24 * 3600

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


_WORKER_DIR_NAME = re.compile(r"w([0-9]+)")


def worker_dir() -> str:
    """This worker process's own directory inside the log directory (asked
    each time: a forked worker is a process of its own)."""
    return f"w{os.getpid()}"


def remove_dead_workers(logs: str) -> None:
    """Remove the directories of worker processes on this machine that are
    gone (``w<pid>`` inside ``logs``) once nothing in them was written for
    :data:`DEAD_WORKER_LOGS_KEEP_S`; a pid that answers is kept."""
    cutoff = time.time() - DEAD_WORKER_LOGS_KEEP_S
    with contextlib.suppress(OSError), os.scandir(logs) as entries:
        for entry in entries:
            found = _WORKER_DIR_NAME.fullmatch(entry.name)
            if found is None or entry.name == worker_dir() \
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


async def prepare_log_dir(env: ExecEnv, log_dir: str) -> str:
    """Make or check ``env``'s private log directory (``log_dir``, empty for
    the per-user default) and return this worker's directory in it."""
    # The env's own variables may be scrubbed: on this machine, ours.
    base = (os.environ.get("TMPDIR") or "") if is_local(env) else ""
    result = await env.run(["sh", "-c", _PREPARE_DIR, "sh", log_dir, base,
                            worker_dir()], timeout=10, output_cap=8192)
    if result.exit_code != 0:
        raise OSError(result.stderr.decode("utf-8", "replace").strip()
                      or "could not prepare the background log directory")
    path = result.stdout.decode("utf-8", "replace")
    if is_local(env):
        # A worker that died left its jobs' logs: nobody reads them now.
        await asyncio.to_thread(remove_dead_workers, posixpath.dirname(path))
    return path
