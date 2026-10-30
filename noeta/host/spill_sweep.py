"""Removing saved tool outputs nobody will read again.

A task's saved outputs (``<spill root>/<task id>/``) stay while the task (or
its tree) is open, however long it waits: a durable task may wake weeks later and read
them. Once its task has ended, a directory not written for
:data:`SPILL_KEEP_DAYS` is removed, the period Claude Code keeps its
sessions' tool results for (``cleanupPeriodDays``). A continuation or a fork
of an ended task (``history_from``) still names the old files, so they are
not removed when the task ends.

This sweeps a spill root on this machine (the local exec env's); a host whose
tasks run in a remote exec env sweeps that env's root itself.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import time

from noeta.core.store import Store

__all__ = ["SPILL_KEEP_DAYS", "SWEEP_EVERY_SECONDS", "keep_spills_swept", "sweep_spills"]

log = logging.getLogger("noeta.host.spill_sweep")

#: Days an ended task's saved outputs are kept after their last write.
SPILL_KEEP_DAYS = 30
#: How often a running host sweeps again.
SWEEP_EVERY_SECONDS = 24 * 3600


def _last_write(path: str) -> float:
    newest = os.lstat(path).st_mtime
    for top, dirs, files in os.walk(path):
        for name in (*dirs, *files):
            with contextlib.suppress(OSError):
                newest = max(newest, os.lstat(os.path.join(top, name)).st_mtime)
    return newest


async def sweep_spills(store: Store, root: str, *, keep_days: float = SPILL_KEEP_DAYS,
                       now: float | None = None) -> list[str]:
    """Remove the directories below ``root`` whose task has ended (or is
    not in ``store``) and that were not written for ``keep_days``; returns
    the task directories removed."""
    try:
        names = await asyncio.to_thread(os.listdir, root)
    except OSError:
        return []
    cutoff = (now if now is not None else time.time()) - keep_days * 86_400
    removed = []
    for name in sorted(names):
        path = os.path.join(root, name)
        try:
            if not os.path.isdir(path) or os.path.islink(path):
                continue
            if await asyncio.to_thread(_last_write, path) > cutoff:
                continue
        except OSError:
            continue
        row = await store.get(name)
        if row is not None and row.status != "ended":
            continue
        if row is not None and row.root_id and row.root_id != row.task_id:
            # A sub-agent's files stay while its tree is open.
            tree = await store.get(row.root_id)
            if tree is not None and tree.status != "ended":
                continue
        await asyncio.to_thread(shutil.rmtree, path, ignore_errors=True)
        removed.append(name)
    if removed:
        log.info("removed the saved tool outputs of %d ended task(s) from %s", len(removed), root)
    return removed


async def keep_spills_swept(store: Store, root: str, stop: asyncio.Event) -> None:
    """Sweep ``root`` now and every :data:`SWEEP_EVERY_SECONDS` until
    ``stop`` is set; a failing sweep is logged and tried again later."""
    while not stop.is_set():
        try:
            await sweep_spills(store, root)
        except Exception:  # housekeeping never stops the host
            log.warning("sweeping saved tool outputs in %s failed", root, exc_info=True)
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), SWEEP_EVERY_SECONDS)
