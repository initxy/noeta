"""Shared process-group signalling primitive (tools m4 / issue 06).

Three kill paths independently open-coded the same POSIX incantation —
``os.getpgid`` → ``os.killpg`` with a single-PID fallback, swallowing the
dead-process / not-permitted races:

* :func:`noeta.tools.fs._subprocess._kill_process_group` — the exec-tool
  timeout teardown (synchronous TERM → grace → KILL).
* :meth:`noeta.runtime.background_shell.ProcessRegistry._terminate` — the
  background-job single-signal send (escalation orchestrated by the caller).
* :func:`noeta.runtime.background_shell._posix_kill_pid` — the confirmed-orphan
  reaper (leader-gated group kill).

They differ in escalation model (sync vs daemon-thread) and in their
pid-reuse guard (a ``Popen.returncode`` check versus an upstream
``IdentityProbe == CONFIRMED`` gate), so those stay at each call site. Only
the raw "signal the group, else the pid, swallow the race" atom is shared —
this one function — so a future adapter cannot silently pick a different
swallowed-error set or drop the single-PID fallback.

POSIX-only, exactly as the three call sites were: every job is spawned with
``start_new_session=True`` so it leads its own group, and signalling the
GROUP reaps backgrounded grandchildren (``bash -c "server & wait"``) a
single-PID signal would orphan.
"""

from __future__ import annotations

import os


__all__ = ["send_group_signal"]

#: The dead-process / not-permitted / bad-handle races every kill path
#: treats as benign — the process may have exited between a reap and the
#: signal, its pid may not be ours to signal, or (via ``Popen``) a set
#: returncode makes the send a no-op. The terminal outcome is recorded
#: elsewhere (the watcher / the durable Lost mark), so a failed signal here
#: is never fatal.
_BENIGN = (ProcessLookupError, PermissionError, OSError, ValueError)


def send_group_signal(pid: int, sig: int, *, require_leader: bool = False) -> None:
    """Send ``sig`` to ``pid``'s process group, falling back to the single
    PID, swallowing the dead-process / not-permitted races.

    ``require_leader`` gates the group kill on ``pid`` actually leading its
    own group (``os.getpgid(pid) == pid``). The confirmed-orphan reaper sets
    it: an orphan recorded by a pre-group-spawn host shares the old host's
    group, and group-killing it would nuke unrelated processes — so a
    non-leader falls back to a single-PID signal. The live paths leave it
    ``False`` (they always spawned their own session, so the group is theirs).

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
