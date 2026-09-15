"""Per-skill usage folded from the ledger — a host-side ranking input for
the ``skill`` menu.

Noeta records no usage counter of its own. Every skill activation is already
a durable ``TaskStatePatched`` event carrying ``activate_skills``, and a
multi-replica server shares the same event store, so the ledger is the one
place a per-tenant count can be derived without a second write path. This
module is the pure fold over those events plus Claude Code's decay score;
what it does NOT know is tenancy — the host picks which task streams belong
to a tenant, folds them here, and hands the result to
``HostConfig.skill_menu_rank_resolver`` (or straight into
``plugin_config["skills"]["menu_rank"]``) as the roster's keep order.

Only activations recorded after a task's loop has started (its first
``ContextPlanComposed``) count — the model's own ``skill`` calls and a user's
mid-task ``/skill`` prelude alike, both being use. A host preload
(``Options.skills``, a seed activation) is the host's choice, not a signal
about what the task reaches for, and counting it would rank every preloaded
skill first forever.

A rank derived here decays with ``now``. The host asks its
``skill_menu_rank_resolver`` once per task (the first non-empty answer is
kept for the task's life in that process), so a fresh ``now`` per call never
rotates a running task's roster; feed it the tenant's fold at task start.

A host that binds no tenancy seam at all does not need a resolver: the SDK
host then ranks with :class:`SkillUsageRanker`, the same fold over the most
recently updated streams of the whole store.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Optional, Protocol

from noeta.protocols.event_log import EventLogReader, EventLogTaskIndex
from noeta.protocols.events import EventEnvelope


__all__ = [
    "SkillUsage",
    "decayed_usage_score",
    "rank_skills_by_usage",
    "skill_usage_from_events",
]


#: The event that marks a task's loop as started: the first compose before
#: the first decide. Activations before it are pre-loop preloads.
_LOOP_STARTED_EVENT = "ContextPlanComposed"
_STATE_PATCHED_EVENT = "TaskStatePatched"
_SECONDS_PER_DAY = 86_400.0

#: How many of the most recently updated task streams
#: :class:`SkillUsageRanker` folds. Bounds the read; the 7-day half-life
#: already makes older use count for little.
DEFAULT_USAGE_SCAN_STREAMS = 200

#: How long, in seconds, one :class:`SkillUsageRanker` snapshot answers
#: before the next ask folds again.
DEFAULT_USAGE_SNAPSHOT_TTL_S = 600.0

_log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SkillUsage:
    """One skill's activation count and the ``occurred_at`` of its latest."""

    count: int
    last_used_at: float


def _activated_names(payload: Any) -> tuple[str, ...]:
    """``activate_skills`` out of a ``TaskStatePatched`` payload — the typed
    dataclass or its plain-dict form — or ``()``."""
    patch = getattr(payload, "patch", None)
    if patch is None and isinstance(payload, Mapping):
        patch = payload.get("patch")
    if not isinstance(patch, Mapping):
        return ()
    names = patch.get("activate_skills")
    # A malformed scalar (a bare string) must not be iterated character by
    # character into phantom skill names.
    if not isinstance(names, (list, tuple)):
        return ()
    return tuple(name for name in names if isinstance(name, str))


def skill_usage_from_events(
    events: Iterable[EventEnvelope],
) -> dict[str, SkillUsage]:
    """Fold event envelopes into ``skill name → SkillUsage``.

    Streams of any number of tasks may be interleaved; each task's own events
    must arrive in ``seq`` order (as an ``EventLog`` read returns them), since
    "after the loop started" is judged per task. Names are returned sorted,
    so two folds of the same events produce equal dicts.
    """
    started: set[str] = set()
    counts: dict[str, int] = {}
    latest: dict[str, float] = {}
    for event in events:
        if event.type == _LOOP_STARTED_EVENT:
            started.add(event.task_id)
            continue
        if event.type != _STATE_PATCHED_EVENT or event.task_id not in started:
            continue
        for name in _activated_names(event.payload):
            counts[name] = counts.get(name, 0) + 1
            latest[name] = max(latest.get(name, 0.0), float(event.occurred_at))
    return {
        name: SkillUsage(count=counts[name], last_used_at=latest[name])
        for name in sorted(counts)
    }


def decayed_usage_score(
    usage: SkillUsage,
    *,
    now: float,
    half_life_days: float = 7.0,
    floor: float = 0.1,
) -> float:
    """``count × max(0.5 ^ (days since last use / half_life_days), floor)``.

    Claude Code's skill-listing score: recent use counts fully, a week-old use
    half, and a skill used long ago never decays below ``floor`` of its count,
    so a once-favourite stays ahead of a never-used one. ``now`` and
    ``last_used_at`` are epoch seconds (``EventEnvelope.occurred_at``).
    """
    days = max(0.0, (now - usage.last_used_at) / _SECONDS_PER_DAY)
    decay = 0.5 ** (days / half_life_days) if half_life_days > 0 else 1.0
    return usage.count * max(decay, floor)


def rank_skills_by_usage(
    usage: Mapping[str, SkillUsage],
    *,
    now: float,
    half_life_days: float = 7.0,
    floor: float = 0.1,
) -> dict[str, float]:
    """The ``menu_rank`` mapping for a folded usage table: every skill scored
    by :func:`decayed_usage_score`. Hand it to the ``skills`` pack as is."""
    return {
        name: decayed_usage_score(
            entry, now=now, half_life_days=half_life_days, floor=floor
        )
        for name, entry in sorted(usage.items())
    }


class _UsageSource(EventLogReader, EventLogTaskIndex, Protocol):
    """The two event-log capabilities the ranker reads: enumerate the task
    streams, read one."""


class SkillUsageRanker:
    """The SDK host's ``menu_rank`` when the host supplies none: skill usage
    folded from the most recently updated task streams of the whole store.

    Store-wide, so only a single-tenant host uses it — ``SdkHost`` skips it
    when a tenancy resolver is bound. Bounded: an ask within ``ttl_s`` of the
    last fold answers from that snapshot, so the store sees at most
    ``max_streams`` stream reads per ``ttl_s`` however many tasks build;
    concurrent asks wait for one fold instead of each running their own. The
    snapshot is process-local, so another process may fold a different one —
    the host's per-task memo keeps a running task's roster fixed, and a task
    resumed elsewhere composes a different roster at most once.

    A failing read never fails the turn build that asked: it is logged, the
    previous snapshot keeps answering, and the next fold is due after
    ``ttl_s``.
    """

    def __init__(
        self,
        event_log: _UsageSource,
        *,
        clock: Callable[[], float] = time.time,
        max_streams: int = DEFAULT_USAGE_SCAN_STREAMS,
        ttl_s: float = DEFAULT_USAGE_SNAPSHOT_TTL_S,
    ) -> None:
        self._event_log = event_log
        self._clock = clock
        self._max_streams = max_streams
        self._ttl_s = ttl_s
        self._lock = threading.Lock()
        self._rank: dict[str, float] = {}
        self._folded_at: Optional[float] = None

    def rank(self) -> dict[str, float]:
        """``skill → decayed usage score`` for the current snapshot (``{}``
        when nothing in the scanned streams activated a skill)."""
        with self._lock:
            now = self._clock()
            if self._folded_at is not None and now - self._folded_at < self._ttl_s:
                return self._rank
            self._folded_at = now
            try:
                streams = self._event_log.list_task_streams()[: self._max_streams]
                usage = skill_usage_from_events(
                    event
                    for summary in streams
                    for event in self._event_log.read(summary.task_id)
                )
            except Exception:
                _log.warning(
                    "skill usage fold failed; the skill menu keeps its previous "
                    "keep order until the next fold",
                    exc_info=True,
                )
                return self._rank
            self._rank = rank_skills_by_usage(usage, now=now)
            return self._rank
