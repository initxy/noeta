"""Five-field cron expressions in local time, as Claude Code reads them:
parsing, the next matching minute, a readable form, and the per-job offset
the scheduler adds so jobs created for the same minute do not all fire at
once."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

#: minute, hour, day of month, month, day of week (0 or 7 is Sunday).
_FIELDS = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 6))
#: The search gives up after a year of minutes (366 days).
_SEARCH_MINUTES = 527_040

#: A recurring job runs up to this fraction of its period late...
RECURRING_FRACTION = 0.5
#: ...but never more than 30 minutes.
RECURRING_CAP_SECONDS = 1800
#: A one-shot job landing on a :00 or :30 minute runs up to 90 s early.
ONE_SHOT_MAX_SECONDS = 90
ONE_SHOT_MINUTE_MOD = 30
#: A recurring job expires after 7 days (it fires once more, then goes).
RECURRING_MAX_AGE_SECONDS = 7 * 86_400
#: A 5-minute ``*/N`` job fires this much early, so its wake-up still reads
#: the prompt cache (5 minutes).
CACHE_LEAD_SECONDS = 15
CACHE_TTL_SECONDS = 300

_EVERY_N_MINUTES = re.compile(r"^\*/\d+ \* \* \* \*$")
_DAYS = ("Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday")


@dataclass(frozen=True)
class Cron:
    minute: frozenset[int]
    hour: frozenset[int]
    day: frozenset[int]
    month: frozenset[int]
    weekday: frozenset[int]

    def _day_matches(self, t: datetime) -> bool:
        any_day, any_weekday = len(self.day) == 31, len(self.weekday) == 7
        weekday = (t.weekday() + 1) % 7  # Python's Monday=0 -> cron's Sunday=0
        if any_day and any_weekday:
            return True
        if any_day:
            return weekday in self.weekday
        if any_weekday:
            return t.day in self.day
        return t.day in self.day or weekday in self.weekday  # cron's "either"

    def matches(self, t: datetime) -> bool:
        return (t.month in self.month and self._day_matches(t) and t.hour in self.hour
                and t.minute in self.minute)


def _field(text: str, lo: int, hi: int) -> frozenset[int] | None:
    weekday = (lo, hi) == (0, 6)
    out: set[int] = set()
    for part in text.split(","):
        if m := re.fullmatch(r"\*(?:/(\d+))?", part):
            step = int(m.group(1) or 1)
            if step < 1:
                return None
            out.update(range(lo, hi + 1, step))
        elif m := re.fullmatch(r"(\d+)-(\d+)(?:/(\d+))?", part):
            a, b, step = int(m.group(1)), int(m.group(2)), int(m.group(3) or 1)
            if a > b or step < 1 or a < lo or b > (7 if weekday else hi):
                return None
            out.update(0 if weekday and v == 7 else v for v in range(a, b + 1, step))
        elif re.fullmatch(r"\d+", part):
            v = int(part)
            if weekday and v == 7:
                v = 0
            if v < lo or v > hi:
                return None
            out.add(v)
        else:
            return None
    return frozenset(out) if out else None


def parse(expr: str) -> Cron | None:
    """The expression, or ``None`` when it is not five valid fields."""
    parts = expr.split()
    if len(parts) != 5:
        return None
    fields = [_field(p, lo, hi) for p, (lo, hi) in zip(parts, _FIELDS, strict=True)]
    if any(f is None for f in fields):
        return None
    minute, hour, day, month, weekday = fields
    assert minute and hour and day and month and weekday
    return Cron(minute, hour, day, month, weekday)


def next_match(expr: str, after: float) -> float | None:
    """The first matching minute strictly after ``after`` (epoch seconds),
    in local time; ``None`` when nothing matches within a year."""
    cron = parse(expr)
    if cron is None:
        return None
    t = datetime.fromtimestamp((int(after) // 60) * 60 + 60).replace(second=0, microsecond=0)
    end = t + timedelta(minutes=_SEARCH_MINUTES)
    while t < end:
        if t.month not in cron.month:
            t = (t.replace(day=1, hour=0, minute=0) + timedelta(days=32)).replace(day=1)
            continue
        if not cron._day_matches(t):
            t = (t + timedelta(days=1)).replace(hour=0, minute=0)
            continue
        if t.hour not in cron.hour:
            t = (t + timedelta(hours=1)).replace(minute=0)
            continue
        if t.minute not in cron.minute:
            t += timedelta(minutes=1)
            continue
        return t.timestamp()
    return None


def job_fraction(job_id: str) -> float:
    """A fixed number in [0, 1) per job, from its id (hex)."""
    try:
        return int(job_id[:8], 16) / 4_294_967_296
    except ValueError:
        return 0.0


def next_fire(expr: str, after: float, job_id: str, *, recurring: bool) -> float | None:
    """When the job runs next: the next match plus the job's fixed offset (a
    recurring job runs late by part of its period; a one-shot job on a :00
    or :30 minute runs a little early)."""
    first = next_match(expr, after)
    if first is None:
        return None
    frac = job_fraction(job_id)
    if not recurring:
        if datetime.fromtimestamp(first).minute % ONE_SHOT_MINUTE_MOD != 0:
            return first
        return first - frac * ONE_SHOT_MAX_SECONDS
    second = next_match(expr, first)
    if second is None:
        return first
    period = second - first
    if _EVERY_N_MINUTES.match(" ".join(expr.split())) and CACHE_LEAD_SECONDS < period \
            and period >= CACHE_TTL_SECONDS and period - CACHE_LEAD_SECONDS < CACHE_TTL_SECONDS:
        return after + period - CACHE_LEAD_SECONDS
    return first + min(frac * RECURRING_FRACTION * period, RECURRING_CAP_SECONDS)


def _clock(minute: int, hour: int) -> str:
    return datetime(2000, 1, 1, hour, minute).strftime("%I:%M %p").lstrip("0")


def describe(expr: str) -> str:
    """A readable form of the common shapes; the expression itself otherwise."""
    parts = expr.split()
    if len(parts) != 5:
        return expr
    mi, h, dom, mon, dow = parts
    rest = (dom, mon, dow) == ("*", "*", "*")
    if h == "*" and rest:
        if mi == "*":
            return "Every minute"
        if m := re.fullmatch(r"\*/(\d+)", mi):
            n = int(m.group(1))
            return "Every minute" if n == 1 else f"Every {n} minutes"
        if mi.isdigit():
            return "Every hour" if int(mi) == 0 else f"Every hour at :{int(mi):02d}"
    if (m := re.fullmatch(r"\*/(\d+)", h)) and mi.isdigit() and rest:
        n, at = int(m.group(1)), int(mi)
        suffix = f" at :{at:02d}" if at else ""
        return f"Every hour{suffix}" if n == 1 else f"Every {n} hours{suffix}"
    if not (mi.isdigit() and h.isdigit()):
        return expr
    clock = _clock(int(mi), int(h))
    if rest:
        return f"Every day at {clock}"
    if (dom, mon) == ("*", "*") and re.fullmatch(r"\d", dow):
        return f"Every {_DAYS[int(dow) % 7]} at {clock}"
    if (dom, mon, dow) == ("*", "*", "1-5"):
        return f"Weekdays at {clock}"
    return expr
