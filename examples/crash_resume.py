"""Runtime demo — kill -9 a live worker mid-task; fold brings the task back.

Demonstrated runtime capability
-------------------------------
The core durability claim: a task's entire state is folded from its
append-only EventLog, never held across runs in process memory. This
script proves it the blunt way:

1. **Process A** creates a task on a durable SQLite store, advances it
   (a recorded progress message, then a ``wait_timer`` suspend), and
   idles. The task is now mid-flight: real work recorded, a wake
   scheduled, nothing finished.
2. The orchestrator sends **SIGKILL** — no shutdown hook, no flush, the
   process is simply gone.
3. **Process B** reopens the same SQLite file, ``fold``s the event
   stream back into the exact suspended state, polls the dispatcher
   until the timer comes due, and the woken task runs to completion —
   exactly once.

No API key and no network: decisions come from a scripted policy (a
``Policy`` is just "given the current View, return a Decision" — an LLM
is one implementation, a script is another). The mechanism exercised —
EventLog + fold + Dispatcher wake — is byte-identical to what a real
LLM-driven agent runs on.

Running it
----------
    python examples/crash_resume.py

Phases can also be driven by hand (see ``--phase start`` / ``--phase
resume``), e.g. to hold the crash window open and inspect the SQLite
file between the two processes.
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from noeta.core.engine import Engine
from noeta.core.fold import fold
from noeta.core.wiring import wire_default_observers
from noeta.policies.stub import StubScriptedPolicy
from noeta.protocols.decisions import (
    FinishDecision,
    StatePatchDecision,
    WaitTimerDecision,
)
from noeta.protocols.events import TaskCompletedPayload, answer_from_payload
from noeta.protocols.messages import Message, TextBlock
from noeta.storage.sqlite import (
    SqliteContentStore,
    SqliteDispatcher,
    SqliteEventLog,
)
from noeta.testing.composer import trivial_three_segment

_TIMER_SECONDS = 6.0
_SUSPENDED_MARKER = "TASK-SUSPENDED"


def _say(text: str) -> None:
    print(f"[pid {os.getpid()}] {text}", flush=True)


def _open_runtime(db: str, policy) -> tuple[Engine, SqliteEventLog, SqliteContentStore, SqliteDispatcher]:
    """Assemble the durable trio + Engine (the deploy-worker wiring)."""
    dispatcher = SqliteDispatcher(db)
    store = SqliteContentStore(db)
    log = SqliteEventLog(db, lease_validator=dispatcher)
    wire_default_observers(log, dispatcher)
    engine = Engine(
        event_log=log,
        content_store=store,
        # The trivial composer keeps the demo free of LLM concerns; a real
        # deployment wires ThreeSegmentComposer via the SDK builder.
        composer=trivial_three_segment(store),
        policy=policy,
    )
    return engine, log, store, dispatcher


def phase_start(db: str) -> None:
    """Process A: create the task, do real work, suspend on a timer, idle."""
    progress = Message(
        role="assistant",
        content=[TextBlock(text="Outline drafted; waiting on external data.")],
    )
    policy = StubScriptedPolicy(
        [
            StatePatchDecision(messages_before=(progress,)),
            WaitTimerDecision(seconds=_TIMER_SECONDS),
        ]
    )
    engine, log, _store, dispatcher = _open_runtime(db, policy)

    task = engine.create_task(goal="Prepare the weekly report", policy_name="scripted")
    _say(f'task {task.task_id} created: "Prepare the weekly report"')
    dispatcher.enqueue(task.task_id)
    lease = dispatcher.lease(worker_id="worker-a", task_id=task.task_id)
    assert lease is not None

    task = engine.run_one_step(task, lease_id=lease.lease_id)
    assert task.status == "suspended", task.status
    dispatcher.release(
        lease.lease_id,
        next_state="suspended",
        wake_on=task.wake_on,
        suspend_reason="waiting_timer",
    )
    _say("step recorded progress, then wait_timer → task suspended durably")
    _say(f"{len(log.read(task.task_id))} events on disk; wake scheduled: {task.wake_on}")
    _say("nothing about this task lives in process memory now — kill me")
    print(f"{_SUSPENDED_MARKER} {task.task_id}", flush=True)
    time.sleep(120)  # the orchestrator SIGKILLs us long before this returns


def phase_resume(db: str, task_id: str) -> None:
    """Process B: reopen the store, fold state back, drain the timer wake."""
    policy = StubScriptedPolicy([FinishDecision(answer="Weekly report ready.")])
    engine, log, store, dispatcher = _open_runtime(db, policy)

    events = log.read(task_id)
    task = fold(log, store, task_id)
    _say(f"reopened {Path(db).name}: fold({len(events)} events) → status={task.status!r}")
    _say(f"recovered mid-task: {len(task.runtime.messages)} messages, wake_on={task.wake_on}")

    while True:
        fired = dispatcher.fire_due_timers(now=time.time())
        if task_id in fired:
            break
        time.sleep(0.2)
    _say("timer came due → dispatcher woke the task (exactly once)")

    lease = dispatcher.lease(worker_id="worker-b", task_id=task_id)
    assert lease is not None
    task = engine.note_woken(task, lease_id=lease.lease_id, wake_event=lease.wake_event)
    task = engine.run_one_step(task, lease_id=lease.lease_id)
    assert task.status == "terminal", task.status
    dispatcher.release(
        lease.lease_id,
        next_state="terminal",
        consumed_wake_event=lease.wake_event,
    )

    answer = ""
    for env in log.read(task_id):
        if env.type == "TaskCompleted":
            assert isinstance(env.payload, TaskCompletedPayload)
            answer = str(answer_from_payload(env.payload, store))
    _say(f"task completed: {answer!r}")
    history = " → ".join(e.type for e in log.read(task_id))
    _say(f"one log, two processes: {history}")


def orchestrate(db: str) -> int:
    """Run phase A, SIGKILL it once suspended, then run phase B."""
    child = subprocess.Popen(
        [sys.executable, "-u", __file__, "--phase", "start", "--db", db],
        stdout=subprocess.PIPE,
        text=True,
    )
    task_id = None
    assert child.stdout is not None
    for line in child.stdout:
        print(line, end="", flush=True)
        if line.startswith(_SUSPENDED_MARKER):
            task_id = line.split()[1]
            break
    if task_id is None:
        child.wait()
        print("phase A never reached the suspend point", file=sys.stderr)
        return 1

    time.sleep(0.5)
    os.kill(child.pid, signal.SIGKILL)
    child.wait()
    print(f"\n>>> kill -9 {child.pid} — the worker is gone, mid-task <<<\n", flush=True)

    return subprocess.call(
        [sys.executable, "-u", __file__, "--phase", "resume", "--db", db, "--task", task_id]
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=["start", "resume"], default=None)
    parser.add_argument("--db", default=None, help="SQLite file shared by both phases")
    parser.add_argument("--task", default=None, help="task id (resume phase)")
    args = parser.parse_args(argv)

    if args.phase == "start":
        phase_start(args.db)
        return 0
    if args.phase == "resume":
        if not args.task:
            parser.error("--phase resume requires --task")
        phase_resume(args.db, args.task)
        return 0

    db = args.db or str(Path(tempfile.mkdtemp(prefix="noeta-crash-resume-")) / "demo.sqlite")
    return orchestrate(db)


if __name__ == "__main__":
    sys.exit(main())
