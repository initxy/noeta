"""A finalizer release on the thread holding the pool lock must not deadlock.

A built Engine releases its MCP leases from a ``weakref.finalize``; a garbage
collection can run that finalizer inside the pool's own critical section
(``acquire`` allocates under the lock). The pool queues that release and
applies it when the lock is let go.
"""

from __future__ import annotations

import threading

from noeta.builtins.mcp.impl import McpConnectionPool
from noeta.builtins.mcp.impl import pool as pool_mod
from noeta.runtime.mcp import McpServerSpec


class _Client:
    def __init__(self) -> None:
        self.shutdowns = 0

    def start(self) -> None:
        pass

    def shutdown(self) -> None:
        self.shutdowns += 1


def test_release_from_inside_the_lock_is_deferred_not_deadlocked(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(pool_mod, "_connect_client", lambda spec, **kw: _Client())
    pool = McpConnectionPool(idle_ttl=None)
    spec = McpServerSpec(alias="a", argv=("srv",))
    held, _ = pool.acquire(spec)

    original = pool._take_locked  # noqa: SLF001

    def take_and_finalize(key):  # type: ignore[no-untyped-def]
        pool.release(held)  # what a GC-run Engine finalizer does, lock held
        return original(key)

    pool._take_locked = take_and_finalize  # type: ignore[method-assign]  # noqa: SLF001
    done = threading.Event()

    def run() -> None:
        pool.acquire(spec)
        done.set()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    assert done.wait(5.0), "pool.acquire deadlocked on a re-entrant release"
    pool._take_locked = original  # type: ignore[method-assign]  # noqa: SLF001
    # The queued release was applied: one holder left (the second acquire).
    assert pool.holders_of(held) == 1
