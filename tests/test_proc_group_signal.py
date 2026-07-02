"""Unit tests for the shared process-group signalling primitive.

``send_group_signal`` is the single atom the three kill paths
(``_subprocess._kill_process_group``, ``background_shell._terminate``,
``background_shell._posix_kill_pid``) now funnel their raw signal-send
through. These pin its three behaviours — group-first, single-PID fallback,
and the leader gate — plus the swallowed-race contract, with the real
``os`` calls monkeypatched so no real process is signalled.
"""

from __future__ import annotations

import os
import signal
from typing import Any

import pytest

from noeta.runtime._proc_group import send_group_signal


def _spy_os(monkeypatch, *, pgid, killpg_exc=None, kill_exc=None):
    """Patch os.getpgid/killpg/kill to record calls; ``pgid`` may be an int or
    an exception instance to raise from getpgid."""
    calls: dict[str, list[Any]] = {"getpgid": [], "killpg": [], "kill": []}

    def _getpgid(pid: int) -> int:
        calls["getpgid"].append(pid)
        if isinstance(pgid, BaseException):
            raise pgid
        return pgid

    def _killpg(pg: int, sig: int) -> None:
        calls["killpg"].append((pg, sig))
        if killpg_exc is not None:
            raise killpg_exc

    def _kill(pid: int, sig: int) -> None:
        calls["kill"].append((pid, sig))
        if kill_exc is not None:
            raise kill_exc

    monkeypatch.setattr(os, "getpgid", _getpgid)
    monkeypatch.setattr(os, "killpg", _killpg)
    monkeypatch.setattr(os, "kill", _kill)
    return calls


def test_group_send_success_does_not_fall_back(monkeypatch) -> None:
    calls = _spy_os(monkeypatch, pgid=4242)
    send_group_signal(4242, signal.SIGTERM)
    assert calls["killpg"] == [(4242, signal.SIGTERM)]
    assert calls["kill"] == []  # group succeeded → no single-PID fallback


def test_group_send_failure_falls_back_to_single_pid(monkeypatch) -> None:
    calls = _spy_os(monkeypatch, pgid=4242, killpg_exc=ProcessLookupError())
    send_group_signal(99, signal.SIGKILL)
    assert calls["killpg"] == [(4242, signal.SIGKILL)]
    assert calls["kill"] == [(99, signal.SIGKILL)]  # fell back to the pid


def test_getpgid_failure_falls_back_to_single_pid(monkeypatch) -> None:
    calls = _spy_os(monkeypatch, pgid=ProcessLookupError())
    send_group_signal(99, signal.SIGTERM)
    assert calls["killpg"] == []  # no pgid → never tried the group
    assert calls["kill"] == [(99, signal.SIGTERM)]


def test_require_leader_non_leader_uses_single_pid(monkeypatch) -> None:
    """A confirmed orphan that does NOT lead its own group (pgid != pid) must
    NOT be group-killed — that would nuke unrelated processes."""
    calls = _spy_os(monkeypatch, pgid=7000)  # pgid 7000 != pid 99
    send_group_signal(99, signal.SIGKILL, require_leader=True)
    assert calls["killpg"] == []  # group skipped: not the leader
    assert calls["kill"] == [(99, signal.SIGKILL)]


def test_require_leader_leader_uses_group(monkeypatch) -> None:
    calls = _spy_os(monkeypatch, pgid=99)  # pgid == pid → is the leader
    send_group_signal(99, signal.SIGKILL, require_leader=True)
    assert calls["killpg"] == [(99, signal.SIGKILL)]
    assert calls["kill"] == []


def test_all_races_swallowed(monkeypatch) -> None:
    """Both the group send and the single-PID fallback dying is benign — the
    terminal outcome is recorded elsewhere, so no exception propagates."""
    calls = _spy_os(
        monkeypatch,
        pgid=4242,
        killpg_exc=PermissionError(),
        kill_exc=ProcessLookupError(),
    )
    send_group_signal(99, signal.SIGTERM)  # must not raise
    assert calls["killpg"] == [(4242, signal.SIGTERM)]
    assert calls["kill"] == [(99, signal.SIGTERM)]
