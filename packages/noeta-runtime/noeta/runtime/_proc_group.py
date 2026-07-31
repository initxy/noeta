"""POSIX process-group signalling, shared by every kill path in the runtime.

Jobs are spawned with ``start_new_session=True`` so each leads its own group,
and signalling the GROUP is what reaps backgrounded grandchildren
(``bash -c "server & wait"``) a single-PID signal would orphan. Only the raw
"signal the group, else the pid, swallow the race" atom is shared — escalation
and the pid-reuse guard stay at each call site — so no caller can silently drop
the single-PID fallback or pick a different swallowed-error set.
"""

from __future__ import annotations

import os


__all__ = ["send_group_signal"]

#: Races every kill path treats as benign: the process may have exited
#: between a reap and the signal, its pid may not be ours to signal, or a set
#: ``Popen`` returncode makes the send a no-op. The terminal outcome is
#: recorded elsewhere, so a failed signal here is never fatal.
_BENIGN = (ProcessLookupError, PermissionError, OSError, ValueError)


def send_group_signal(pid: int, sig: int, *, require_leader: bool = False) -> None:
    """Send ``sig`` to ``pid``'s process group, falling back to the single PID.

    ``require_leader`` gates the group kill on ``pid`` actually leading its
    own group: a pid that does not is sitting in someone else's group, and
    group-killing it would take unrelated processes down, so a non-leader
    falls back to a single-PID signal. Callers that spawned their own session
    know the group is theirs and leave it ``False``.

    Callers own their own pid-reuse guard (check ``Popen.returncode`` / verify
    process identity) BEFORE calling this — once past that, a signal that
    fails because the process just died is benign and swallowed here.
    """
    try:
        pgid = os.getpgid(pid)
    except _BENIGN:
        pgid = None
    if pgid is not None and (not require_leader or pgid == pid):
        try:
            os.killpg(pgid, sig)
            return
        except _BENIGN:
            pass
    try:
        os.kill(pid, sig)
    except _BENIGN:
        pass
