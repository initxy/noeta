"""StepContext: read-only context propagated Engine → Policy → RuntimeLLMClient.

Every Policy decision and every runtime LLM call carries an explicit
``StepContext`` instead of reading process-globals. The field set is kept
minimal and the dataclass frozen, so a new field is a pure addition that
cannot break an existing caller.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Optional

if TYPE_CHECKING:  # pragma: no cover - typing-only, keeps this module import-light
    from noeta.protocols.events import EventEnvelope


@dataclass(frozen=True, slots=True)
class StepContext:
    """Identifies the current Engine step for downstream callees.

    ``task_id`` / ``lease_id`` / ``trace_id`` are what every downstream emit
    needs to join its recorded event row back to the Engine cycle that produced
    it. StepContext is transient and in-process: never serialized, never on an
    event.

    ``last_input_tokens`` is the input-token count the provider actually
    reported for the previous round-trip. A compaction-aware Policy uses it as
    a deterministic baseline (real usage plus a chars/4 estimate of only the
    newly-appended messages) rather than estimating the whole prompt with
    chars/4, which systematically under-counts cache / structured blocks /
    images. ``0`` on the first turn, where the Policy falls back to a pure
    estimate.

    ``apply_event`` closes the emit/apply gap for emit-sites outside the
    Engine. The Engine folds every event it emits onto the in-memory task;
    ``RuntimeLLMClient`` cannot, since it appends straight to the EventLog and
    holds no Task. Without a callback, ``fold(events)`` and the runtime state
    diverge mid tool-loop — breaking the equation ADR
    ``single-writer-invariant`` rests on — and ``last_input_tokens`` stays
    frozen at whatever the entry fold produced however many round-trips the
    loop makes. The Engine injects a callback bound to the task it is stepping
    and remains the sole physical writer; the client only notifies. ``None``
    means emit-only, for callers that build a StepContext without one.

    ``cancelled`` is the Engine's cooperative-cancel predicate, handed down so
    a blocking callee — the LLM round-trip and its retry backoff — can notice
    a human stop *while* it waits instead of only at the Engine's own turn
    boundaries. It is the same predicate ``run_one_step`` polls; ``None``
    (resume, replay, hosts that wire no cancel seam) disarms every downstream
    abort site, which is what keeps recordings byte-identical on those paths.

    ``steps_in_turn`` is how many ``decide()`` calls the current driven turn
    (one ``Engine.run_one_step`` drive) has already made — ``0`` on the turn's
    first decide. It is the step-cap's SOLE counter: a Policy compares it
    against its ``max_steps`` ceiling so a runaway tool loop is bounded PER
    TURN. The count deliberately does NOT live on the Policy instance — a
    cached Engine's Policy outlives turns and even tasks (the Engine cache key
    omits ``task_id``), so an instance counter silently accumulates across a
    whole conversation (and across conversations sharing the cache slot) until
    every later turn dies at the ceiling. Threaded here, the budget resets at
    each turn by construction.
    """

    task_id: str
    lease_id: str
    trace_id: str
    last_input_tokens: int = 0
    apply_event: Optional[Callable[["EventEnvelope"], None]] = None
    cancelled: Optional[Callable[[], bool]] = None
    steps_in_turn: int = 0
