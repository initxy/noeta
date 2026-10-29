"""Errors that cross module boundaries. Providers map their vendor's failures
onto ``ModelError`` subclasses so the runner's retry rule is provider-neutral."""

from __future__ import annotations


class NoetaError(Exception):
    pass


class Conflict(NoetaError):
    """A conditional append lost: another runner owns the task now, or the
    caller's view of the log is stale. The caller stops writing."""


class CommitUnknown(Conflict):
    """The store's connection dropped while a write was committing, so it may
    or may not have landed. It is never retried blindly: the caller stops as
    if its claim were lost, and whoever runs the task next reads the log."""


class InboxUnread(Conflict):
    """An append that ends the task found inbox items its runner has not
    taken: a task never ends over a message it did not read. The runner
    takes them and decides again."""


class StoreUnavailable(NoetaError):
    """The store could not be reached, or its connection dropped before a
    write committed. Nothing was written; the call may be made again."""


class UnknownTask(NoetaError):
    pass


class HeldElsewhere(NoetaError):
    """A hold on a task tree was refused: another worker holds the tree
    (``Store.hold``), and a tree has at most one holding worker. What needed
    the hold (a background job) cannot start in this process."""


class ModelError(NoetaError):
    def __init__(self, message: str, *, status: int | None = None, body: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.body = body


class Transient(ModelError):
    """Worth retrying as is: network failures, 5xx, overloaded."""


class RateLimited(Transient):
    def __init__(self, message: str, *, retry_after: float | None = None, **kw: object) -> None:
        super().__init__(message, **kw)  # type: ignore[arg-type]
        self.retry_after = retry_after


class ContextOverflow(ModelError):
    """The request does not fit the model's window; compact and retry."""


class Fatal(ModelError):
    """Retrying the same request cannot help (auth, bad request, refusal)."""


class MalformedToolArguments(Transient):
    """Tool-call arguments that are not a JSON object. Almost always a stream
    cut mid-call, so the call is asked for again — once
    (:data:`noeta.core.retry.MALFORMED_RETRIES`)."""


class TurnFailed(Exception):
    """Raised by an extension hook (``before_model``, ``on_context_overflow``)
    when the turn cannot go on, e.g. a compaction that cannot make progress.
    The runner fails the turn with ``reason`` and ``detail``: an interactive
    task parks, a one-shot task ends failed."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


class TurnDone(Exception):
    """Raised by ``before_model`` when the turn is complete without a model
    call (a local command such as ``/compact`` did its work). The runner
    records what the hook recorded and closes the turn."""


class RequestStale(Exception):
    """Raised by ``before_model`` after it changed the conversation the
    request was built from (a compaction): the runner records what the hooks
    recorded and builds the request again, every hook seeing the new state."""
