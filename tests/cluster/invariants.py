"""Log invariants every task must keep, however its workers died.

Each check returns a list of violations (empty = fine), phrased so a failing
run says which task and which seq. They read only what the public client
returns: the task row and its event log.

What a log cannot show: events carry no claim epoch, so "one writer per
append range" is checked two ways instead: structurally (a second writer
would break the pairing of model calls and replies, of tool starts and
finishes, or take inbox items twice) and by :class:`EpochWatch`, which
samples task rows while the cluster runs (an epoch never goes back and
names one owner).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from noeta.core.events import (
    ChildEnded,
    ExtensionRecorded,
    InputTaken,
    ModelCalled,
    ModelReplied,
    Recorded,
    Say,
    TaskCreated,
    TaskEnded,
    TimerFired,
    ToolFinished,
    ToolStarted,
)
from noeta.core.store import TaskRow
from noeta.core.tool import MAX_ATTEMPTS

SHELL = "shell"
#: The tools the scripts call that are ``retry_safe`` (``ToolSearch`` loads the
#: cron tools): a crash during one starts it
#: again (up to ``MAX_ATTEMPTS`` times).
RETRY_SAFE = frozenset({"Lookup", "ToolSearch"})


def check_log(row: TaskRow, records: Sequence[Recorded], *, settled: bool) -> list[str]:
    """The invariants of one task's log. ``settled``: the task is at rest
    (waiting or ended, its inbox read), so nothing may be left open."""
    tid = row.task_id
    out: list[str] = []

    def bad(seq: int, what: str) -> None:
        out.append(f"{tid} seq {seq}: {what}")

    seqs = [r.seq for r in records]
    if seqs != list(range(1, len(records) + 1)):
        gaps = [s for a, s in zip(range(1, len(seqs) + 1), seqs, strict=False) if a != s]
        bad(gaps[0] if gaps else 0, f"seqs are not 1..{len(records)} without gaps")
    if row.last_seq != len(records):
        bad(row.last_seq, f"row.last_seq {row.last_seq} != {len(records)} events")
    if not records or not isinstance(records[0].event, TaskCreated):
        bad(1, "the log does not start with TaskCreated")

    through = 0
    taken: Counter[int] = Counter()
    called = False
    use_ids: Counter[str] = Counter()
    started: dict[str, int] = {}
    starts: Counter[str] = Counter()
    finished: Counter[str] = Counter()
    ended_at: int | None = None
    for r in records:
        ev = r.event
        if ended_at is not None:
            bad(r.seq, f"{type(ev).__name__} after TaskEnded at seq {ended_at}")
        if isinstance(ev, InputTaken):
            items = [i.seq for i in ev.items]
            if items != list(range(through + 1, ev.through + 1)):
                bad(r.seq, f"InputTaken items {items} do not continue from {through} "
                           f"through {ev.through}")
            taken.update(items)
            through = max(through, ev.through)
        elif isinstance(ev, ModelCalled):
            called = True
        elif isinstance(ev, ModelReplied):
            if not called:
                bad(r.seq, "ModelReplied with no ModelCalled since the previous reply")
            called = False
            use_ids.update(u.id for u in ev.response.message.tool_uses)
        elif isinstance(ev, ToolStarted):
            starts[ev.call.id] += 1
            if ev.call.id in started and (ev.call.name not in RETRY_SAFE
                                          or starts[ev.call.id] > MAX_ATTEMPTS):
                bad(r.seq, f"call {ev.call.id} started again (first at {started[ev.call.id]})")
            if finished[ev.call.id]:
                bad(r.seq, f"call {ev.call.id} started after it finished")
            started.setdefault(ev.call.id, r.seq)
        elif isinstance(ev, ToolFinished):
            finished[ev.result.call_id] += 1
        elif isinstance(ev, TaskEnded):
            ended_at = r.seq
    for seq, n in taken.items():
        if n > 1:
            bad(seq, f"inbox item {seq} taken {n} times")
    for cid, n in use_ids.items():
        if n > 1:
            bad(0, f"tool_use id {cid} in {n} replies")
    for cid, n in finished.items():
        if n > 1:
            bad(0, f"call {cid} finished {n} times")
    if settled:
        if row.inbox_read_seq != row.inbox_seq or through != row.inbox_seq:
            bad(0, f"inbox not fully read: read {row.inbox_read_seq} / taken {through} / "
                   f"posted {row.inbox_seq}")
        for cid, seq in started.items():
            if finished[cid] != 1:
                bad(seq, f"call {cid} started but never finished")
    out += check_jobs(tid, records)
    return out


def jobs_of(records: Iterable[Recorded]) -> dict[str, list[tuple[int, str]]]:
    """Background job id -> its shell records ``(seq, kind)`` in order."""
    jobs: dict[str, list[tuple[int, str]]] = {}
    for r in records:
        ev = r.event
        if isinstance(ev, ExtensionRecorded) and ev.extension == SHELL \
                and isinstance(ev.data, dict) and "job_id" in ev.data:
            jobs.setdefault(str(ev.data["job_id"]), []).append((r.seq, ev.kind))
    return jobs


def check_jobs(tid: str, records: Sequence[Recorded]) -> list[str]:
    """A background job is started once, settled (ended or lost) at most
    once, never both, and the model is told how it ended at most once (a
    job called lost never reports an end after)."""
    out = []
    for job, n in job_notices(records).items():
        if n > 1:
            out.append(f"{tid}: job {job} told to the model {n} times")
    for job, marks in jobs_of(records).items():
        kinds = [k for _s, k in marks]
        ends = [k for k in kinds if k in ("ended", "lost")]
        if kinds.count("started") != 1:
            out.append(f"{tid}: job {job} started {kinds.count('started')} times")
        if len(ends) > 1:
            out.append(f"{tid}: job {job} settled more than once: {kinds}")
    return out


def jobs_running(tid: str, records: Sequence[Recorded]) -> list[str]:
    """Jobs the log still shows running: once their command must be over,
    each is a job silently missing (neither ended nor marked lost)."""
    return [f"{tid}: job {job} never ended and was never marked lost"
            for job, marks in jobs_of(records).items()
            if not any(k in ("ended", "lost") for _s, k in marks)]


def job_notices(records: Sequence[Recorded],
                jobs: Iterable[str] | None = None) -> Counter[str]:
    """Job id -> how many notices of its end (or loss) the task took: a
    ``<task-notification>`` naming the job with a status. ``jobs``: the jobs
    to count (default: the ones this log started)."""
    jobs = set(jobs_of(records) if jobs is None else jobs)
    out: Counter[str] = Counter()
    for text in taken_texts(records):
        if "<task-notification>" not in text or "<status>" not in text:
            continue
        for job in jobs:
            if f"<task-id>{job}</task-id>" in text:
                out[job] += 1
    return out


def jobs_untold(tid: str, records: Sequence[Recorded]) -> list[str]:
    """Jobs settled in the log whose end (or loss) the model never heard."""
    told = job_notices(records)
    return [f"{tid}: job {job} {[k for _s, k in marks]} but the model was never told"
            for job, marks in jobs_of(records).items()
            if any(k in ("ended", "lost") for _s, k in marks) and told[job] == 0]


def lost_jobs_ended_late(records: Sequence[Recorded]) -> list[str]:
    """Jobs the log calls lost whose end notice reached the task anyway (the
    job was alive all along: the model was told two stories)."""
    lost = {job for job, marks in jobs_of(records).items()
            for _seq, kind in marks if kind == "lost"}
    return [f"job {job}: lost, and its end ({status}) reached the task too"
            for text in taken_texts(records) for job in lost
            if f"<task-id>{job}</task-id>" in text
            for status in ("completed", "killed") if f"<status>{status}</status>" in text]


def taken_texts(records: Iterable[Recorded]) -> list[str]:
    """The texts of every message the task took from its inbox."""
    return [item.body.message.text for r in records if isinstance(r.event, InputTaken)
            for item in r.event.items if isinstance(item.body, Say)]


def child_ends(records: Iterable[Recorded]) -> Counter[str]:
    return Counter(item.body.child_id for r in records if isinstance(r.event, InputTaken)
                   for item in r.event.items if isinstance(item.body, ChildEnded))


def timers_taken(records: Iterable[Recorded]) -> list[str]:
    return [item.body.name for r in records if isinstance(r.event, InputTaken)
            for item in r.event.items if isinstance(item.body, TimerFired)]


def last_reply(records: Sequence[Recorded]) -> str | None:
    for r in reversed(records):
        if isinstance(r.event, ModelReplied):
            return r.event.response.message.text
    return None


@dataclass
class EpochWatch:
    """Samples of task rows over time: an owner epoch never decreases, one
    epoch has one owner, and the log never shrinks."""

    seen: dict[str, tuple[int, int]] = field(default_factory=dict)
    owners: dict[tuple[str, int], str] = field(default_factory=dict)
    violations: list[str] = field(default_factory=list)
    samples: int = 0

    def observe(self, rows: Iterable[TaskRow]) -> None:
        self.samples += 1
        for row in rows:
            epoch, last = row.owner_epoch, row.last_seq
            prev = self.seen.get(row.task_id)
            if prev is not None:
                if epoch < prev[0]:
                    self.violations.append(f"{row.task_id}: epoch went back {prev[0]} -> {epoch}")
                if last < prev[1]:
                    self.violations.append(f"{row.task_id}: last_seq went back {prev[1]} -> {last}")
            self.seen[row.task_id] = (max(epoch, prev[0] if prev else 0),
                                      max(last, prev[1] if prev else 0))
            if row.owner and row.status == "running":
                key = (row.task_id, epoch)
                was = self.owners.setdefault(key, row.owner)
                if was != row.owner:
                    self.violations.append(f"{row.task_id}: epoch {epoch} owned by {was} and "
                                           f"{row.owner}")


def check_journal(lines: Iterable[str]) -> list[str]:
    """No side-effect token was journalled twice."""
    counts = Counter(line.split(" ", 1)[0] for line in lines if line.strip())
    return [f"token {tok} journalled {n} times" for tok, n in sorted(counts.items()) if n > 1]
