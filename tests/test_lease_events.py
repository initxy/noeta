"""Lease-lifecycle and cancellation event payloads.

Guards that ``TaskCancelledPayload`` and ``LeaseGrantedPayload`` are well-formed
dataclasses carrying the fields their consumers read, and that no lease payload
is named with the forbidden ``Run`` prefix — ``scripts/lint-naming.py`` bans
``Run`` as an identifier, so the canonical spelling is ``LeaseGranted``.
"""

from __future__ import annotations

from dataclasses import is_dataclass


def test_task_cancelled_payload_dataclass_is_importable() -> None:
    from noeta.protocols.events import TaskCancelledPayload

    assert is_dataclass(TaskCancelledPayload)
    payload = TaskCancelledPayload(reason="user-cancel")
    assert payload.reason == "user-cancel"


def test_task_cancelled_payload_supports_cascade_flag() -> None:
    """Cancellation cascades to in-flight subtasks; the payload carries the
    ``cascade`` flag the cascade logic keys on."""
    from noeta.protocols.events import TaskCancelledPayload

    payload = TaskCancelledPayload(reason="parent-cancel", cascade=True)
    assert payload.cascade is True


def test_lease_granted_payload_dataclass_is_importable() -> None:
    from noeta.protocols.events import LeaseGrantedPayload

    assert is_dataclass(LeaseGrantedPayload)
    payload = LeaseGrantedPayload(
        lease_id="lease-1", worker_id="w1", expires_at=42.0
    )
    assert payload.lease_id == "lease-1"
    assert payload.worker_id == "w1"
    assert payload.expires_at == 42.0


def test_lease_granted_replaces_forbidden_run_leased_name() -> None:
    """The forbidden ``RunLeased`` name must not appear in the events module —
    ``Run`` is banned as an identifier."""
    import noeta.protocols.events as events_mod

    assert not hasattr(events_mod, "RunLeasedPayload")
    assert not hasattr(events_mod, "RunLeaseHeartbeatPayload")
    assert hasattr(events_mod, "LeaseGrantedPayload")
    assert hasattr(events_mod, "TaskCancelledPayload")
