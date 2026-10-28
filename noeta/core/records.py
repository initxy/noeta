"""The core's own log entries: ``ExtensionRecorded(extension="core", kind,
data)`` with one typed payload per ``kind``.

Every core entry is written through :func:`event` (or a view's ``record``
with :attr:`CoreRecord.kind` and :meth:`CoreRecord.data`) and read back
through :func:`decode`, so the dict shape on the wire has one owner. Front
ends read ``notice``, ``retry``, ``turn_failed``, ``turn_stopped`` and
``spawned``; the rest are the kernel's bookkeeping.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, ClassVar, Union

from noeta.core.events import Event, ExtensionRecorded
from noeta.core.types import Message, Struct

#: The extension name the core records its own entries under.
CORE = "core"


def _fields(data: Any) -> dict[str, Any]:
    return data if isinstance(data, dict) else {}


class Notice(Struct):
    """Something for the person, never the model (``notify``): ``text``,
    and for structured news its ``kind`` and ``data``."""

    kind: ClassVar[str] = "notice"
    text: str
    notice_kind: str | None = None
    notice_data: Any = None

    def data(self) -> dict[str, Any]:
        out: dict[str, Any] = {"text": self.text}
        if self.notice_kind:
            out["kind"] = self.notice_kind
        if self.notice_data is not None:
            out["data"] = self.notice_data
        return out

    @classmethod
    def parse(cls, data: Any) -> Notice:
        d = _fields(data)
        return cls(text=str(d.get("text") or ""), notice_kind=d.get("kind"),
                   notice_data=d.get("data"))


class Retry(Struct):
    """A failed model call about to be retried."""

    kind: ClassVar[str] = "retry"
    attempt: Any = None
    delay: Any = None
    error: Any = None

    def data(self) -> dict[str, Any]:
        return {"attempt": self.attempt, "delay": self.delay, "error": self.error}

    @classmethod
    def parse(cls, data: Any) -> Retry:
        d = _fields(data)
        return cls(attempt=d.get("attempt"), delay=d.get("delay"), error=d.get("error"))


class Continue(Struct):
    """The model hit its output limit; the entry's message asks it to go on."""

    kind: ClassVar[str] = "continue"

    def data(self) -> None:
        return None

    @classmethod
    def parse(cls, data: Any) -> Continue:
        return cls()


class TurnStopped(Struct):
    """The turn was stopped. Stopped mid-reply, ``partial`` is what streamed
    and ``marker`` opens the person's next turn."""

    kind: ClassVar[str] = "turn_stopped"
    partial: Any = None
    marker: Any = None

    def data(self) -> dict[str, Any] | None:
        if self.partial is None and self.marker is None:
            return None
        return {"partial": self.partial, "marker": self.marker}

    @classmethod
    def parse(cls, data: Any) -> TurnStopped:
        d = _fields(data)
        return cls(partial=d.get("partial"), marker=d.get("marker"))


class TurnFailed(Struct):
    """The turn could not go on; the task waits for the next message."""

    kind: ClassVar[str] = "turn_failed"
    reason: Any = "failed"
    detail: Any = ""

    def data(self) -> dict[str, Any]:
        return {"reason": self.reason, "detail": self.detail}

    @classmethod
    def parse(cls, data: Any) -> TurnFailed:
        d = _fields(data)
        return cls(reason=d.get("reason", "failed"), detail=d.get("detail", ""))

    @property
    def error(self) -> str:
        """``reason: detail``, or the reason alone."""
        return f"{self.reason}: {self.detail}" if self.detail else str(self.reason)


class TurnDone(Struct):
    """A ``before_model`` ended the turn without a model call."""

    kind: ClassVar[str] = "turn_done"

    def data(self) -> None:
        return None

    @classmethod
    def parse(cls, data: Any) -> TurnDone:
        return cls()


class Parked(Struct):
    """A tool call waits: ``on`` what, for which ``keys``, and what to show."""

    kind: ClassVar[str] = "parked"
    call_id: Any = None
    on: Any = None
    keys: Any = None
    detail: Any = None

    def data(self) -> dict[str, Any]:
        return {"call_id": self.call_id, "on": self.on, "keys": self.keys,
                "detail": self.detail}

    @classmethod
    def parse(cls, data: Any) -> Parked:
        d = _fields(data)
        return cls(call_id=d.get("call_id"), on=d.get("on"), keys=d.get("keys"),
                   detail=d.get("detail"))


class Spawned(Struct):
    """Children this task staged: each ``{"id", "call_id", "background"}``."""

    kind: ClassVar[str] = "spawned"
    children: list[dict[str, Any]] = []

    def data(self) -> dict[str, Any]:
        return {"children": self.children}

    @classmethod
    def parse(cls, data: Any) -> Spawned:
        return cls(children=list(_fields(data).get("children") or []))


class Timer(Struct):
    """A timer set: wake the idle task at ``at`` with ``text``."""

    kind: ClassVar[str] = "timer"
    name: str
    at: float
    text: str = ""

    def data(self) -> dict[str, Any]:
        return {"name": self.name, "at": self.at, "text": self.text}

    @classmethod
    def parse(cls, data: Any) -> Timer:
        d = _fields(data)
        return cls(name=str(d.get("name") or ""), at=float(d.get("at") or 0),
                   text=str(d.get("text") or ""))


class TimerCleared(Struct):
    kind: ClassVar[str] = "timer_cleared"
    name: str

    def data(self) -> dict[str, Any]:
        return {"name": self.name}

    @classmethod
    def parse(cls, data: Any) -> TimerCleared:
        return cls(name=str(_fields(data).get("name") or ""))


class Prompts(Struct):
    """The prompt override files read when the task first started, by name."""

    kind: ClassVar[str] = "prompts"
    files: dict[str, str] = {}

    def data(self) -> dict[str, str]:
        return self.files

    @classmethod
    def parse(cls, data: Any) -> Prompts:
        return cls(files={str(k): v for k, v in _fields(data).items() if isinstance(v, str)})


class Plugins(Struct):
    """The plugins the task runs with (``registry.pin`` of each extension),
    recorded when it first starts and again when a pickup finds them
    changed; a worker missing one of them fails the task instead of running
    it without."""

    kind: ClassVar[str] = "plugins"
    pins: list[dict[str, str]] = []

    def data(self) -> dict[str, Any]:
        return {"pins": self.pins}

    @classmethod
    def parse(cls, data: Any) -> Plugins:
        return cls(pins=[p for p in _fields(data).get("pins") or [] if isinstance(p, dict)])


CoreRecord = Union[Notice, Retry, Continue, TurnStopped, TurnFailed, TurnDone, Parked,
                   Spawned, Timer, TimerCleared, Prompts, Plugins]

_KINDS: dict[str, Any] = {cls.kind: cls for cls in (
    Notice, Retry, Continue, TurnStopped, TurnFailed, TurnDone, Parked, Spawned, Timer,
    TimerCleared, Prompts, Plugins)}


def event(record: CoreRecord, *, message: Message | None = None) -> ExtensionRecorded:
    """The log entry for ``record``."""
    return ExtensionRecorded(extension=CORE, kind=record.kind, data=record.data(),
                             message=message)


def decode(ev: Event) -> CoreRecord | None:
    """``ev`` as a core record, or ``None`` when it is not one."""
    if not isinstance(ev, ExtensionRecorded) or ev.extension != CORE:
        return None
    cls = _KINDS.get(ev.kind)
    return cls.parse(ev.data) if cls is not None else None


class CoreNotices:
    """``notify``, ``set_timer`` and ``clear_timer`` for whatever has
    ``record(extension, kind, data)``: a tool's context and an extension's
    view record the same core entries."""

    if TYPE_CHECKING:
        record: Callable[..., None]

    def notify(self, text: str, *, kind: str | None = None, data: Any = None) -> None:
        """Tell the person ``text``; front ends show it, the model never sees
        it (a ``core`` / ``notice`` record). ``kind`` and ``data`` carry the
        same news as structured data for a front end that renders it
        (``text`` stays the plain fallback)."""
        self._record_core(Notice(text=text, notice_kind=kind, notice_data=data))

    def set_timer(self, name: str, at: float, text: str = "") -> None:
        """Wake the task at ``at`` (epoch seconds) once it is idle, with
        ``text`` as the next message (``TimerFired(name)`` either way); a
        timer of the same name is replaced. Durable: any worker fires it."""
        self._record_core(Timer(name=name, at=at, text=text))

    def clear_timer(self, name: str) -> None:
        self._record_core(TimerCleared(name=name))

    def _record_core(self, entry: CoreRecord) -> None:
        self.record(CORE, entry.kind, entry.data())
