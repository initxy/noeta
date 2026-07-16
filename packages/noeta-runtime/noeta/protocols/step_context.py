"""StepContext: read-only context propagated Engine → Policy → RuntimeLLMClient.

Every Policy decision and every runtime LLM call carries
an explicit ``StepContext`` instead of reading process-globals. The
field set is intentionally minimal in Phase 1 first-slice; the
``frozen=True`` dataclass shape means later Phases can add fields (e.g.
``retries_so_far`` / ``deadline_at`` / ``cumulative_tokens``) as pure
additions without breaking existing callers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Optional

if TYPE_CHECKING:  # pragma: no cover - typing-only, keeps this module import-light
    from noeta.protocols.events import EventEnvelope


@dataclass(frozen=True, slots=True)
class StepContext:
    """Identifies the current Engine step for downstream callees.

    ``task_id`` / ``lease_id`` / ``trace_id`` are the three identifiers
    every downstream emit needs so the recorded event row can be joined
    back to the Engine cycle that produced it.

    ``last_input_tokens`` carries the REAL input-token count the
    provider reported for the previous LLM round-trip
    (``RuntimeState.last_input_tokens``), injected by the Engine at step setup.
    A compaction-aware Policy uses it as the deterministic history baseline for
    its trigger math (real recorded usage + chars/4 of only the newly-appended
    messages), instead of estimating the whole prompt with chars/4 — which
    systematically under-counts cache / structured blocks / images. It is a
    pure additive field (the ``frozen`` dataclass note above): ``0`` on the
    first turn (no prior usage yet) → the Policy falls back to a pure estimate.
    StepContext is a transient in-process value (never serialized / never on an
    event), so adding this field is byte-neutral for resume.

    ``apply_event`` closes the emit/apply gap for the emit-sites that live
    OUTSIDE the Engine. Every event the Engine itself emits it also folds onto
    the in-memory task (``emit`` + ``apply_event`` pairs in ``core.engine``);
    ``RuntimeLLMClient`` cannot, because it appends straight to the EventLog and
    holds no Task. So mid tool-loop ``fold(events) → state`` and the runtime
    state DIVERGE — the very equation ADR ``single-writer-invariant`` rests on —
    and ``RuntimeState.last_input_tokens`` stays frozen at whatever the entry
    fold produced (``0`` on a first turn) however many round-trips the loop
    makes. The Engine injects a callback bound to the task it is stepping; the
    client invokes it right after each emit. The Engine remains the sole
    physical writer (it supplies the applier and owns the Task); the client only
    notifies. ``None`` keeps the legacy emit-only behaviour for any caller that
    builds a StepContext without one (tests, embedders).
    """

    task_id: str
    lease_id: str
    trace_id: str
    last_input_tokens: int = 0
    apply_event: Optional[Callable[["EventEnvelope"], None]] = None
