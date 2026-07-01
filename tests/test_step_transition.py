"""Foundation B — StepTransition continuation tag (pure runtime).

Covers the new judgement-tag mechanism that records *why* a step had a
next step, so the later recovery guards (② error recovery,
④ RepetitionGuard) can read an O(1) field instead of piling logic into
the Engine body. The contract is fixed by README D-B1..D-B6:

* D-B1 a NEW independent ``StepTransitionMarked`` event (not a reuse of
  ``TaskWoken`` / ``TaskSuspended``).
* D-B2 only **non-default** continuations emit a tag (``approval_resume``
  / ``transient_retry`` / ``overflow_recovery`` / ``max_output_recovery``
  / ``compaction_retry``); the implicit ``next_turn`` default does NOT
  emit, keeping the event stream small.
* D-B3 the anti-spiral guard reads only ``RuntimeState.last_transition``
  (last-write-wins, byte-safe optional). No durable attempt counter.
* D-B5 fold tolerates an unknown ``reason`` (warning-not-fatal).
* byte-equal: the payload round-trips canonical bytes stably.
"""

from __future__ import annotations

import dataclasses

import pytest

from noeta.protocols.canonical import from_canonical_bytes, to_canonical_bytes
from noeta.protocols.events import StepTransitionMarkedPayload
from noeta.protocols.step_transition import (
    TRANSITION_REASONS,
    StepTransition,
)
from noeta.protocols.task import RuntimeState


# ---------------------------------------------------------------------------
# TransitionReason vocabulary + StepTransition dataclass
# ---------------------------------------------------------------------------


def test_transition_reasons_are_the_six_locked_values() -> None:
    """The locked vocabulary (README D-B2). ``next_turn`` is the implicit
    default (never emitted); the other five are non-default continuations
    that DO emit a tag."""
    assert TRANSITION_REASONS == (
        "next_turn",
        "approval_resume",
        "transient_retry",
        "overflow_recovery",
        "max_output_recovery",
        "compaction_retry",
    )


def test_step_transition_constructs_for_every_reason() -> None:
    for reason in TRANSITION_REASONS:
        st = StepTransition(reason=reason)
        assert st.reason == reason
        assert st.attempt == 0  # attempt defaults to 0 (reserved for ②)


def test_step_transition_carries_attempt() -> None:
    st = StepTransition(reason="transient_retry", attempt=2)
    assert st.attempt == 2


def test_step_transition_is_frozen() -> None:
    st = StepTransition(reason="approval_resume")
    with pytest.raises(dataclasses.FrozenInstanceError):
        st.reason = "next_turn"  # type: ignore[misc]


def test_step_transition_only_imports_stdlib() -> None:
    """protocols-isolation (L0): step_transition.py may import only the
    standard library / sibling protocols — never a higher layer."""
    import ast
    from pathlib import Path

    import noeta.protocols.step_transition as mod

    tree = ast.parse(Path(mod.__file__).read_text(encoding="utf-8"))
    forbidden = ("noeta.core", "noeta.runtime", "noeta.storage", "noeta.agent")
    bad: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            m = node.module or ""
            if any(m == p or m.startswith(p + ".") for p in forbidden):
                bad.append(m)
        elif isinstance(node, ast.Import):
            for a in node.names:
                if any(
                    a.name == p or a.name.startswith(p + ".") for p in forbidden
                ):
                    bad.append(a.name)
    assert not bad, f"step_transition.py must not import higher layers: {bad}"


# ---------------------------------------------------------------------------
# RuntimeState.last_transition
# ---------------------------------------------------------------------------


def test_runtime_state_last_transition_defaults_none() -> None:
    assert RuntimeState().last_transition is None


def test_runtime_state_last_transition_is_settable() -> None:
    rs = RuntimeState()
    rs.last_transition = "overflow_recovery"
    assert rs.last_transition == "overflow_recovery"


def test_runtime_state_last_transition_is_the_last_field() -> None:
    """Newest optional field appended LAST so an old snapshot dict (without
    the key) rebuilds via the default and a new snapshot stays
    byte-comparable (the set_todos/skill 'optional + last' convention).
    Compaction appended ``last_input_tokens`` after ``last_transition`` —
    it is now the tail field guarded by this convention."""
    names = [f.name for f in dataclasses.fields(RuntimeState)]
    assert names[-1] == "last_input_tokens"


# ---------------------------------------------------------------------------
# StepTransitionMarkedPayload — the replay-durable carrier (D-B1)
# ---------------------------------------------------------------------------


def test_payload_round_trips_canonical_bytes() -> None:
    payload = StepTransitionMarkedPayload(reason="approval_resume", attempt=1)
    restored = from_canonical_bytes(to_canonical_bytes(payload))
    # canonical bytes are a plain dict (no tag registered for this payload —
    # the sqlite restorer rebuilds it), so compare on the wire shape.
    assert restored == {"reason": "approval_resume", "attempt": 1}


def test_payload_attempt_defaults_zero() -> None:
    payload = StepTransitionMarkedPayload(reason="next_turn")
    assert payload.attempt == 0


def test_payload_is_frozen() -> None:
    payload = StepTransitionMarkedPayload(reason="next_turn")
    with pytest.raises(dataclasses.FrozenInstanceError):
        payload.reason = "approval_resume"  # type: ignore[misc]
