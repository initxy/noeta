"""3A-I3 — graceful shutdown + signal handling (best-effort).

`install_stop_signals` wires SIGTERM/SIGINT to `WorkerLoop.stop`
(main-thread only) and restores prior handlers. The boundary (B5):
Noeta only promises stop-leasing + the current synchronous step finishes
+ clean exit 0 — NO in-process interrupt, NO hard deadline. The
subprocess test asserts "step finished + exit 0", NOT a fixed deadline.

Per the architect note, the signal handler lands as an L2-callable
helper here; the subprocess harness is inline (does NOT depend on the
I4 `noeta serve` command).
"""

from __future__ import annotations

import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from noeta.runtime.worker import WorkerLoop, install_stop_signals


_REPO_ROOT = Path(__file__).resolve().parents[1]


class _NoopRT:
    """Minimal WorkerRuntime — never actually leased in signal tests."""

    def __init__(self) -> None:
        self.engine = None
        self.event_log = None
        self.content_store = None

        class _D:
            def lease(self, **_k: Any) -> Any:
                return None

        self.dispatcher = _D()


def test_install_stop_signals_flips_running_flag_on_sigterm() -> None:
    loop = WorkerLoop(_NoopRT(), worker_id="w", heartbeat_interval=0)
    assert loop.running is True
    restore = install_stop_signals(loop)
    try:
        signal.raise_signal(signal.SIGTERM)
        # The handler runs at the next bytecode boundary; a tiny spin
        # lets it land deterministically.
        for _ in range(1000):
            if not loop.running:
                break
        assert loop.running is False
    finally:
        restore()


def test_install_stop_signals_restores_previous_handler() -> None:
    loop = WorkerLoop(_NoopRT(), worker_id="w", heartbeat_interval=0)
    original = signal.getsignal(signal.SIGTERM)
    restore = install_stop_signals(loop)
    assert signal.getsignal(signal.SIGTERM) is not original
    restore()
    assert signal.getsignal(signal.SIGTERM) is original


def test_install_stop_signals_off_main_thread_is_noop() -> None:
    loop = WorkerLoop(_NoopRT(), worker_id="w", heartbeat_interval=0)
    result: dict[str, Any] = {}

    def worker() -> None:
        # signal.signal raises ValueError off the main thread → helper
        # returns a no-op restore + warns; loop still stoppable via stop().
        restore = install_stop_signals(loop)
        result["restore"] = restore
        restore()  # must not raise

    t = threading.Thread(target=worker)
    t.start()
    t.join(timeout=5.0)
    assert callable(result["restore"])
    assert loop.running is True  # unchanged — no handler installed


# ---------------------------------------------------------------------------
# Subprocess best-effort shutdown (inline harness, no I4 dependency)
# ---------------------------------------------------------------------------


_HARNESS = '''
import sys, time
from noeta.testing.profile import (
    build_runtime, build_tools, default_budget, default_permission_policy,
)
from noeta.runtime.worker import WorkerLoop


class _SlowProvider:
    """end_turn after a ~1s sleep so a single step is slow-but-finishes;
    lets the test send SIGTERM mid-step."""
    def complete(self, request):
        from noeta.protocols.messages import LLMResponse, TextBlock, Usage
        time.sleep(1.0)
        return LLMResponse(
            stop_reason="end_turn",
            content=[TextBlock(text="done")],
            usage=Usage(uncached=1, output=1),
        )


def main():
    sqlite_path = sys.argv[1]
    bundle = build_runtime(
        provider=_SlowProvider(), model="m", system_prompt="p",
        tools=build_tools(), sqlite_path=sqlite_path, sse_broadcaster=None,
        max_steps=5, permission_policy=default_permission_policy(),
        budget=default_budget(),
    )
    task = bundle.engine.create_task(goal="g", policy_name="react")
    bundle.dispatcher.enqueue(task.task_id)
    print("TASK " + task.task_id, flush=True)
    loop = WorkerLoop(bundle, worker_id="w", poll_interval=0.05,
                      heartbeat_interval=0)
    loop.run_forever(install_signals=True)
    bundle.shutdown()
    print("EXITED", flush=True)


if __name__ == "__main__":
    main()
'''


def test_sigterm_during_step_finishes_step_then_exits_clean(tmp_path: Any) -> None:
    """B5 boundary: SIGTERM mid-step → the current step still finishes
    (task reaches terminal), the process exits 0. The test asserts the
    step completed + clean exit, NOT a fixed shutdown deadline."""
    harness = tmp_path / "harness.py"
    harness.write_text(_HARNESS)
    db = tmp_path / "shutdown.sqlite"

    proc = subprocess.Popen(
        [sys.executable, "-u", str(harness), str(db)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=str(_REPO_ROOT),
    )
    try:
        assert proc.stdout is not None
        # Wait for the task to be created + the loop to begin the step.
        task_line = proc.stdout.readline().strip()
        assert task_line.startswith("TASK "), task_line
        task_id = task_line.split(" ", 1)[1]
        # Send SIGTERM while the ~1s step is in flight.
        time.sleep(0.2)
        proc.send_signal(signal.SIGTERM)
        # The in-flight step must still finish, then the loop exits.
        out, err = proc.communicate(timeout=15.0)
        assert proc.returncode == 0, err
        assert "EXITED" in out
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5.0)

    # The step finished despite the SIGTERM → task reached terminal.
    from noeta.testing.profile import build_sqlite_stack
    from noeta.core.fold import fold

    event_log, content_store, _ = build_sqlite_stack(str(db))
    folded = fold(event_log, content_store, task_id)
    assert folded.status == "terminal"
