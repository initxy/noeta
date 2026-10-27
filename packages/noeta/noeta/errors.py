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


class StoreUnavailable(NoetaError):
    """The store could not be reached, or its connection dropped before a
    write committed. Nothing was written; the call may be made again."""


class UnknownTask(NoetaError):
    pass


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
    cut mid-call, so the call is retried like any transient failure."""
