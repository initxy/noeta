"""New event payloads required by issue 06.

Issue 06 introduces the lease-lifecycle and cancellation events. The
issue text used the legacy ``RunLeased`` name; per CONTEXT.md
"Run" is forbidden, so the kernel adopts ``LeaseGranted``,
``LeaseHeartbeat``, ``LeaseExpired``, ``TaskRequeued`` (and the
already-listed ``TaskCancelled``).

Phase 0 only requires ``TaskCancelled`` + ``LeaseGranted`` to be
defined; the rest are introduced as the relevant emitters arrive in
later phases (Worker daemon, server-side dispatcher metrics, etc.).
"""

from __future__ import annotations

from dataclasses import is_dataclass


def test_task_cancelled_payload_dataclass_is_importable() -> None:
    from noeta.protocols.events import TaskCancelledPayload

    assert is_dataclass(TaskCancelledPayload)
    payload = TaskCancelledPayload(reason="user-cancel")
    assert payload.reason == "user-cancel"


def test_task_cancelled_payload_supports_cascade_flag() -> None:
    """Cancellation often cascades to in-flight subtasks (a
    consequence). The payload carries the documentary flag; the
    actual cascade mechanism lands with the Worker daemon."""
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
    """Sanity: the legacy ``RunLeased`` name from issue 06 prose must
    not appear in the events module — ``Run`` is forbidden."""
    import noeta.protocols.events as events_mod

    assert not hasattr(events_mod, "RunLeasedPayload")
    assert not hasattr(events_mod, "RunLeaseHeartbeatPayload")
    assert hasattr(events_mod, "LeaseGrantedPayload")
    assert hasattr(events_mod, "TaskCancelledPayload")
