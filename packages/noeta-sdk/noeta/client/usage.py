"""Cost / usage read model: what a task (and its sub-agents) cost, per model.

A pure projection over the event log. Every LLM round-trip records an
``LLMRequestStarted`` (which model) and an ``LLMRequestFinished`` (its
``Usage``, ``cost_usd`` and ``latency_ms``) paired by ``call_id`` on the
task's own stream; this module folds those pairs into per-model rows and
totals. Nothing is recomputed — ``cost_usd`` is what the pricing callback
charged at call time.

A ``$0`` row is ambiguous on its own: the catalog charges nothing both for a
model it has no rates for and for a model it does not know. The report makes
that visible — each row says whether its model is priced by the current
catalog, and :attr:`UsageReport.unpriced_models` lists every model whose cost
is not a real number.

The catalog is not read here. The caller passes an ``is_priced`` predicate
(the ``Client`` passes the loader-resolved catalog accessor), so this module
stays free of any edge into the built-ins.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Callable, Optional

from noeta.protocols.event_log import EventLogReader
from noeta.protocols.events import EventEnvelope
from noeta.protocols.messages import Usage


__all__ = ["ModelUsage", "UsageReport", "build_usage_report"]


#: The event types that hand a child task id to its parent's stream: a
#: foreground spawn and a background sub-agent spawn.
_SPAWN_EVENTS = ("SubtaskSpawned", "BackgroundSubagentStarted")


@dataclass(frozen=True, slots=True)
class ModelUsage:
    """Accumulated usage for one model across the covered tasks.

    Token counters mirror ``GovernanceState``: ``input_tokens`` is every prompt
    token (``Usage.input`` — uncached plus cache read plus cache write), and
    the two cache counters break out how much of it hit or filled the cache.
    ``reasoning_tokens`` are already included in ``output_tokens``.

    ``requests`` counts finished round-trips (successful or not);
    ``unfinished_requests`` counts ``LLMRequestStarted`` events with no
    matching ``LLMRequestFinished`` — a call still in flight, or one a crash
    cut off. Those carry no usage and no cost.

    ``priced`` is ``False`` when the current catalog has no rates for
    ``model`` (or does not know it): ``cost_usd`` is then ``0.0`` because
    nothing was charged, not because the calls were free.
    """

    model: str
    requests: int = 0
    unfinished_requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms_total: int = 0
    latency_ms_max: int = 0
    priced: bool = True


@dataclass(frozen=True, slots=True)
class UsageReport:
    """What ``task_id`` cost, per model and in total.

    ``tasks`` lists every task whose calls are counted — ``task_id`` first,
    then its sub-agents (foreground and background, any depth) in discovery
    order when children were included. ``per_model`` is sorted by model id.
    The top-level counters are the sums over ``per_model``.

    ``unpriced_models`` names every model whose calls were charged ``$0``
    because the catalog has no rates for it; when it is non-empty,
    ``cost_usd`` is a lower bound. Register the missing rows through
    ``HostConfig.extra_models``.
    """

    task_id: str
    tasks: tuple[str, ...] = ()
    per_model: tuple[ModelUsage, ...] = ()
    requests: int = 0
    unfinished_requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms_total: int = 0
    latency_ms_max: int = 0
    unpriced_models: tuple[str, ...] = ()


class _Acc:
    """Mutable per-model accumulator; frozen into a :class:`ModelUsage`."""

    __slots__ = (
        "requests", "unfinished", "input", "output", "cache_read",
        "cache_write", "reasoning", "cost", "lat_total", "lat_max",
    )

    def __init__(self) -> None:
        self.requests = 0
        self.unfinished = 0
        self.input = 0
        self.output = 0
        self.cache_read = 0
        self.cache_write = 0
        self.reasoning = 0
        self.cost = 0.0
        self.lat_total = 0
        self.lat_max = 0

    def add(self, usage: Usage, cost_usd: float, latency_ms: int) -> None:
        self.requests += 1
        self.input += usage.input
        self.output += usage.output
        self.cache_read += usage.cache_read
        self.cache_write += usage.cache_write
        self.reasoning += usage.reasoning_tokens
        self.cost += cost_usd
        self.lat_total += latency_ms
        self.lat_max = max(self.lat_max, latency_ms)


def _parent_of(events: list[EventEnvelope]) -> Optional[str]:
    """``parent_task_id`` from the stream's genesis ``TaskCreated``."""
    for env in events:
        if env.type == "TaskCreated":
            parent: Optional[str] = getattr(env.payload, "parent_task_id", None)
            return parent
    return None


def build_usage_report(
    reader: EventLogReader,
    task_id: str,
    *,
    is_priced: Callable[[str], bool],
    include_children: bool = True,
) -> UsageReport:
    """Fold the LLM round-trips of ``task_id`` (and its sub-agents) into a report.

    Children are found through the spawn records on each parent's stream
    (``SubtaskSpawned`` / ``BackgroundSubagentStarted``) and admitted only
    when the child's own ``TaskCreated.parent_task_id`` names that parent —
    the genesis record is the authority, the spawn record is the index. A
    spawn whose child stream does not exist (a denied or never-started
    child) contributes nothing. The walk is breadth-first and cycle-safe.
    An unknown ``task_id`` yields an empty report with ``tasks == ()``.

    ``is_priced(model)`` judges each model against the catalog as it stands
    now; the recorded ``cost_usd`` is reported unchanged.
    """
    order: list[str] = []
    seen: set[str] = set()
    queue: deque[tuple[str, Optional[str]]] = deque([(task_id, None)])
    accs: dict[str, _Acc] = {}

    while queue:
        current, parent = queue.popleft()
        if current in seen:
            continue
        events = reader.read(current)
        if not events:
            continue
        if parent is not None and _parent_of(events) != parent:
            continue
        seen.add(current)
        order.append(current)
        started: dict[str, str] = {}
        for env in events:
            if env.type == "LLMRequestStarted":
                started[env.payload.call_id] = env.payload.model
            elif env.type == "LLMRequestFinished":
                model = started.pop(env.payload.call_id, None)
                if model is None:
                    # A Finished with no Started on this stream has no model
                    # to attribute to; it cannot come from the recorded trio.
                    continue
                acc = accs.setdefault(model, _Acc())
                acc.add(
                    getattr(env.payload, "usage", None) or Usage(),
                    float(env.payload.cost_usd),
                    int(env.payload.latency_ms),
                )
            elif include_children and env.type in _SPAWN_EVENTS:
                child = env.payload.subtask_id
                if child not in seen:
                    queue.append((child, current))
        for model in started.values():
            accs.setdefault(model, _Acc()).unfinished += 1

    rows = tuple(
        ModelUsage(
            model=model,
            requests=a.requests,
            unfinished_requests=a.unfinished,
            input_tokens=a.input,
            output_tokens=a.output,
            cache_read_tokens=a.cache_read,
            cache_write_tokens=a.cache_write,
            reasoning_tokens=a.reasoning,
            cost_usd=a.cost,
            latency_ms_total=a.lat_total,
            latency_ms_max=a.lat_max,
            priced=is_priced(model),
        )
        for model, a in sorted(accs.items())
    )
    return UsageReport(
        task_id=task_id,
        tasks=tuple(order),
        per_model=rows,
        requests=sum(r.requests for r in rows),
        unfinished_requests=sum(r.unfinished_requests for r in rows),
        input_tokens=sum(r.input_tokens for r in rows),
        output_tokens=sum(r.output_tokens for r in rows),
        cache_read_tokens=sum(r.cache_read_tokens for r in rows),
        cache_write_tokens=sum(r.cache_write_tokens for r in rows),
        reasoning_tokens=sum(r.reasoning_tokens for r in rows),
        cost_usd=sum(r.cost_usd for r in rows),
        latency_ms_total=sum(r.latency_ms_total for r in rows),
        latency_ms_max=max((r.latency_ms_max for r in rows), default=0),
        unpriced_models=tuple(r.model for r in rows if not r.priced),
    )
