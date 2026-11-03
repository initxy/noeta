"""Skill usage folded from task logs — a ranking a host can pass to
``Skills(rank=...)``.

Every activation is an ``ExtensionRecorded(extension="skills",
kind="activated")`` entry; the model's ``skill`` calls and the user's
``/<name>`` count as use, a host preload does not, and one skill counts at
most once a minute across the store (Claude Code's). The score is Claude
Code's: ``count * max(0.5 ^ (days since last use / 7), 0.1)``.

The extension reads no other task, so a host wires it through
the host's client (what it may read is what the client shows):
``Skills(usage_from=client)`` (the ``noeta`` command passes its store's), or
``Skills(rank=UsageRank(client))``. The ranker answers from its last
snapshot at once and refreshes in the background when the snapshot is
older than ``ttl``, so a task start never waits on the scan; until the first
scan lands the ranking is empty (tier and name order).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from noeta.core.client import ClientAPI
from noeta.core.events import ExtensionRecorded, Recorded

__all__ = ["SkillUsage", "UsageRank", "decayed_usage_score", "skill_usage", "usage_rank"]

_log = logging.getLogger(__name__)
_SECONDS_PER_DAY = 86_400.0
_COUNTED = ("model", "user")
#: Uses of one skill closer together than this count once (Claude Code's).
COUNT_INTERVAL_SECONDS = 60.0


@dataclass(frozen=True, slots=True)
class SkillUsage:
    count: int
    last_used_at: float


def skill_usage(records: Iterable[Recorded], extension: str = "skills") -> dict[str, SkillUsage]:
    """Each skill's use count and last counted use over ``records`` (any
    tasks' logs, in any order): a use within a minute of the last counted
    one is not counted."""
    uses: list[tuple[float, str]] = []
    for rec in records:
        ev = rec.event
        if not (isinstance(ev, ExtensionRecorded) and ev.extension == extension
                and ev.kind == "activated" and isinstance(ev.data, dict)):
            continue
        name = ev.data.get("name")
        if not isinstance(name, str) or ev.data.get("source") not in _COUNTED:
            continue
        uses.append((rec.at, name))
    counts: dict[str, int] = {}
    latest: dict[str, float] = {}
    for at, name in sorted(uses):
        if name in latest and at - latest[name] < COUNT_INTERVAL_SECONDS:
            continue
        counts[name] = counts.get(name, 0) + 1
        latest[name] = at
    return {n: SkillUsage(counts[n], latest[n]) for n in sorted(counts)}


def decayed_usage_score(usage: SkillUsage, *, now: float, half_life_days: float = 7.0,
                        floor: float = 0.1) -> float:
    days = max(0.0, (now - usage.last_used_at) / _SECONDS_PER_DAY)
    decay = 0.5 ** (days / half_life_days) if half_life_days > 0 else 1.0
    return usage.count * max(decay, floor)


async def usage_rank(client: ClientAPI, *, max_tasks: int = 200, now: float | None = None,
                     extension: str = "skills") -> dict[str, float]:
    """Scores over the ``max_tasks`` most recently updated tasks the
    client shows."""
    records: list[Recorded] = []
    for row in await client.tasks(limit=max_tasks):
        records.extend(await client.events(row.task_id))
    at = time.time() if now is None else now
    return {n: decayed_usage_score(u, now=at)
            for n, u in skill_usage(records, extension).items()}


class UsageRank:
    """A ``rank`` callable over a usage scan of the tasks ``client`` shows
    (a host's client: a remote one works too)."""

    def __init__(self, client: ClientAPI, *, ttl: float = 600.0, max_tasks: int = 200,
                 clock: Callable[[], float] = time.time) -> None:
        self.client = client
        self.ttl = ttl
        self.max_tasks = max_tasks
        self.clock = clock
        self._rank: dict[str, float] = {}
        self._at: float | None = None
        self._task: asyncio.Task[None] | None = None

    def __call__(self, _view: Any = None) -> Mapping[str, float]:
        now = self.clock()
        due = self._at is None or now - self._at >= self.ttl
        if due and (self._task is None or self._task.done()):
            self._at = now
            self._task = asyncio.get_running_loop().create_task(self._scan(now))
        return dict(self._rank)

    async def wait(self) -> None:
        """Wait for a running scan (tests, warm-up)."""
        if self._task is not None:
            await self._task

    async def _scan(self, now: float) -> None:
        try:
            self._rank = await usage_rank(self.client, max_tasks=self.max_tasks, now=now)
        except Exception:  # noqa: BLE001 — keep the previous ranking
            _log.warning("skill usage scan failed; keeping the previous ranking", exc_info=True)
