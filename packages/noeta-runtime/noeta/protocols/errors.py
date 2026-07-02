"""Noeta error hierarchy.

Each error is mapped to a documented runtime concern. The hierarchy is
deliberately flat: callers either need to handle these specifically or
they propagate as programmer errors.
"""

from __future__ import annotations

import random
from typing import Callable


class NoetaError(Exception):
    """Base class for all Noeta-defined errors."""


class CodedError(NoetaError):
    """A :class:`NoetaError` carrying a stable, machine-matchable ``code``.

    ``code`` is a byte-stable public token — never a class name, never a
    message substring — that boundary code matches **structurally** to
    decide client-facing handling. The product's HTTP backend reaches the
    engine only through ``noeta.sdk`` (import-linter ``backend-only-sdk``),
    so it cannot ``isinstance`` runtime-internal exception types; it catches
    this one re-exported base and switches on ``exc.code`` instead of the
    fragile ``type(exc).__name__`` / ``"...substring..." in str(exc)`` it
    used before. The ``code`` vocabulary is part of the public surface and
    must stay stable across releases — same discipline as the ``category``
    constants below. Subclasses set a concrete ``code``; a subclass may also
    inherit a stdlib exception (e.g. ``RuntimeError``) alongside this so an
    ``except RuntimeError`` contract keeps matching.
    """

    #: Stable public error code. Subclasses override with a concrete token.
    code: str = "error"


class ContentNotFound(NoetaError):
    """ContentStore lookup with an unknown ContentRef."""


class StaleSequence(NoetaError):
    """EventLog.append called with an expected_seq that no longer matches.

    Phase 0 surfaces the type for protocol shape only; strict enforcement
    is the job of issue 06.
    """


class InvalidLease(NoetaError):
    """Write attempted with an unknown, expired, or released lease_id.

    Phase 0 surfaces the type for protocol shape only; strict enforcement
    is the job of issue 06.
    """


class TaskCancellationRequested(NoetaError):
    """Cooperative cancellation signal raised mid-step by the Engine.

    A workflow / subtask tree was cancelled (a ``TaskCancelled`` control
    event was written for its root) while a child was mid-flight. The
    Engine polls an injected ``cancelled`` predicate at its turn
    boundaries — the top of the compose→decide loop and again right after
    the Policy decides (i.e. once the in-flight LLM / tool round has
    returned) — and raises this to ABANDON that result without acting on
    it: no assistant message appended, no tools run, no next turn. The
    delegation drain catches it and tears the tree down (cascade-cancel
    the remaining children, release the in-flight lease); the root is
    already terminal from the ``TaskCancelled`` event. Carries the
    offending ``task_id``.

    Never raised on resume: that path injects no predicate, so
    the poll is a no-op and recordings stay byte-identical.
    """

    def __init__(self, task_id: str) -> None:
        self.task_id = task_id
        super().__init__(f"task {task_id!r} cancelled mid-step")


class WakeConsumeMismatch(NoetaError):
    """``Dispatcher.release(consumed_wake_event=X)`` was called but ``X``
    does not equal the task's currently-stored ``matched_wake_event`` (or
    no matched event is stored) — H2.

    The Dispatcher raises this **and commits nothing** (the release rolls
    back) rather than clearing the wrong event or releasing without the
    clear, which would leave a terminal/suspended row carrying a stale
    matched event (a future spurious re-delivery). A consuming release
    must present exactly the wake it consumed.
    """


class PayloadTooLarge(NoetaError):
    """EventLog.append received a payload that exceeds the EventLog payload cap.

    The EventEnvelope payload ceiling is fixed at 4 KB. Any body
    larger than that must be stored in ContentStore and referenced
    inline via a ``ContentRef``. This error is the runtime guard that
    keeps backend implementations honest before Phase 1's real-LLM
    traffic starts producing large bodies.
    """


class ApprovalNotPending(NoetaError):
    """``Engine.resolve_tool_approval`` was called for a ``call_id`` that
    is not in ``governance.pending_approvals`` (Phase 4.5 Issue A).

    This is the fail-closed guard against a stale or duplicate
    resolution: a second resolution for an already-resolved call, or a
    resolution for a call that was never blocked, raises this and emits
    **no** ``ToolCallApprovalResolved`` event — so the EventLog never
    carries two resolutions for one ``call_id``.
    """


class UserQuestionNotPending(NoetaError):
    """``Engine.answer_user_question`` was called for a question_id that is
    not in ``governance.pending_questions`` (CW18d)."""


# ---------------------------------------------------------------------------
# ② error recovery — provider-neutral error taxonomy (README D-2)
# ---------------------------------------------------------------------------
#
# These three classes are the *Noeta-shape* error vocabulary every provider
# adapter (sdk, L2) translates its wire-specific failures into, and that
# the runtime LLM wrapper (``RuntimeLLMClient``) catches to decide whether
# to retry (transient) or surface (overflow / fatal). They sit at L0 so
# both providers (providers-only-protocols) and the runtime
# (runtime-no-providers) can import them without violating the
# topology, and they carry **no** vendor field — a 429 status
# code or an OpenAI ``context_length_exceeded`` body never leaks past the
# adapter; only the neutral class + an optional ``retry_after`` does.

#: The closed set of error category labels. They double as the
#: ``raw['category']`` value the runtime stamps onto an error
#: ``LLMResponse`` (so Policy can branch without re-deriving the class) and
#: are byte-stable so old recordings keep matching.
CATEGORY_TRANSIENT = "transient"
CATEGORY_OVERFLOW = "overflow"
CATEGORY_FATAL = "fatal"


class TransientError(NoetaError):
    """A retryable failure — rate limits (429), overloaded (529), 5xx,
    connection / timeout errors. The runtime retries these internally
    (LIVE-only, see README D-2d) up to a small budget before giving up
    and translating to an error response.

    ``retry_after`` carries a provider-supplied delay hint in **seconds**
    (e.g. from a ``Retry-After: 5`` header) when available; ``None`` lets
    :func:`retry_policy` fall back to exponential backoff.
    """

    category = CATEGORY_TRANSIENT

    def __init__(
        self, *args: object, retry_after: float | None = None
    ) -> None:
        super().__init__(*args)
        self.retry_after = retry_after


class ContextOverflowError(NoetaError):
    """The request exceeded the model's context window (prompt too long /
    ``context_length_exceeded`` / ``max tokens``). Not retryable as-is —
    the recovery is compaction (③), driven by Policy reading
    ``raw['category'] == 'overflow'``; the runtime does not retry it.
    """

    category = CATEGORY_OVERFLOW

    def __init__(
        self, *args: object, retry_after: float | None = None
    ) -> None:
        super().__init__(*args)
        self.retry_after = retry_after


class FatalError(NoetaError):
    """A non-retryable client error (4xx other than 429 / overflow:
    auth, malformed request, ...). Surfaced to Policy as a fatal error
    response → ``FailDecision(retryable=False)``.
    """

    category = CATEGORY_FATAL

    def __init__(
        self, *args: object, retry_after: float | None = None
    ) -> None:
        super().__init__(*args)
        self.retry_after = retry_after


class MalformedToolArgumentsError(TransientError, ValueError):
    """A provider returned a tool call whose ``arguments`` is not decodable JSON.

    In practice this is a **mid-stream truncation**: the model began emitting
    the arguments string and the connection dropped (or the gateway closed /
    garbled the body) before it closed — the same class of transport failure as
    a stalled stream, and frequently paired with a pathological latency. It is
    therefore bucketed ``transient`` (category inherited from
    :class:`TransientError`) so the runtime re-issues the request up to its
    retry budget instead of failing the whole task on a single flaky response.
    A genuinely model-malformed body simply re-samples and, if it never parses,
    still surfaces as an error once the budget is spent — bounded, not looping.

    It also subclasses :class:`ValueError` so the shared codec's historical
    contract ("raises ``ValueError`` worded ``'<label> not JSON-decodable: …'``")
    and any ``except ValueError`` caller keep matching byte-for-byte.
    """

    # category + retry_after come from TransientError; ValueError contributes
    # only the isinstance compatibility, no __init__ of its own.


#: Exponential-backoff base (seconds) and ceiling for transient retries.
_BACKOFF_BASE_SECONDS = 1.0
_BACKOFF_CAP_SECONDS = 30.0


def retry_policy(
    error: NoetaError,
    *,
    attempt: int,
    rng: Callable[[], float] = random.random,
) -> float | None:
    """Backoff policy: how long to wait before the next attempt, or ``None``
    for "do not retry".

    * :class:`TransientError` with an explicit ``retry_after`` → that value
      verbatim (the provider's hint is authoritative; no jitter is layered on
      top — both wire forms, delta-seconds and HTTP-date, are resolved to
      seconds by the adapter's ``parse_retry_after``).
    * :class:`TransientError` without a hint → exponential backoff
      ``base * 2**attempt`` capped at ~30s, with **equal jitter**: half the
      computed delay is fixed and half is uniformly random
      (``temp/2 + rng()*temp/2``). The fixed half keeps a meaningful floor (a
      rate limit needs a real wait), while the random half decorrelates
      sibling retriers — e.g. two subagents that hit the same 429 in lockstep
      no longer retry in unison and re-collide.
    * Anything else (overflow / fatal / a bare NoetaError) → ``None``.

    The retry loop is LIVE-only and writes no events (README D-2d), so the
    chosen delay is never observed downstream — the jitter has no fold/resume
    consequence. ``rng`` is injectable purely so tests can pin the draw.
    """
    if not isinstance(error, TransientError):
        return None
    if error.retry_after is not None:
        return error.retry_after
    temp = min(_BACKOFF_BASE_SECONDS * float(2**attempt), _BACKOFF_CAP_SECONDS)
    return temp / 2.0 + rng() * (temp / 2.0)
