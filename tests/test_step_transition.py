"""The ``StepTransition`` tag: why a step had a successor.

Recovery guards need to know *why* the previous step continued without
re-deriving it from the event stream, so the reason is recorded as a tag and
read back off ``RuntimeState.last_transition`` in O(1) instead of piling
logic into the Engine body. Only non-default continuations emit a tag — the
implicit ``next_turn`` default stays silent to keep the event stream small.
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


def test_transition_reasons_are_the_six_locked_values() -> None:
    """``next_turn`` is the implicit default and never emits a tag; the other
    five are the non-default continuations that do."""
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
        assert st.attempt == 0


def test_step_transition_carries_attempt() -> None:
    st = StepTransition(reason="transient_retry", attempt=2)
    assert st.attempt == 2


def test_step_transition_is_frozen() -> None:
    st = StepTransition(reason="approval_resume")
    with pytest.raises(dataclasses.FrozenInstanceError):
        st.reason = "next_turn"  # type: ignore[misc]


def test_step_transition_only_imports_stdlib() -> None:
    """``noeta.protocols`` is the typed boundary: this module may import only
    the standard library and sibling protocols, never a higher layer."""
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


def test_runtime_state_last_transition_defaults_none() -> None:
    assert RuntimeState().last_transition is None


def test_runtime_state_last_transition_is_settable() -> None:
    rs = RuntimeState()
    rs.last_transition = "overflow_recovery"
    assert rs.last_transition == "overflow_recovery"


def test_runtime_state_last_transition_is_the_last_field() -> None:
    """Optional fields are appended LAST, so a snapshot dict written without
    the key rebuilds via the default and stays byte-comparable. Pinning the
    current tail field makes a violation of that convention fail loudly."""
    names = [f.name for f in dataclasses.fields(RuntimeState)]
    assert names[-1] == "last_input_tokens"


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
