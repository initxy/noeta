"""A background command of any length records a ``BackgroundShellStarted``.

The event's payload is capped (``EVENT_PAYLOAD_MAX_BYTES``); a long heredoc
command once blew that cap and crashed the spawn. The event records the
command's start, and the job runs the whole command.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from noeta.protocols.canonical import to_canonical_bytes
from noeta.protocols.values import EVENT_PAYLOAD_MAX_BYTES
from noeta.runtime.background_shell import ProcessRegistry
from noeta.storage.memory import InMemoryContentStore, InMemoryEventLog


def test_a_long_heredoc_command_starts_and_records_its_start(tmp_path: Path) -> None:
    log = InMemoryEventLog()
    reg = ProcessRegistry(event_log=log, content_store=InMemoryContentStore())
    script = "print('ok')\n" + "# padding 你好\n" * 600
    command = f"python3 - <<'EOF'\n{script}EOF"
    assert len(to_canonical_bytes(command)) > EVENT_PAYLOAD_MAX_BYTES

    out = reg.spawn(
        argv=[sys.executable, "-c", script],
        cwd=tmp_path,
        env={},
        command=command,
        spawned_by_task_id="t",
        trace_id="tr",
    )

    started = next(e for e in log.read("t") if e.type == "BackgroundShellStarted")
    recorded = started.payload.command
    assert recorded.endswith("...")
    assert command.startswith(recorded[:-3])
    assert len(to_canonical_bytes(recorded)) <= 2048
    deadline = time.monotonic() + 8
    while reg.poll(out["job_id"])["status"] == "running":
        assert time.monotonic() < deadline
        time.sleep(0.01)


def test_a_short_command_is_recorded_whole(tmp_path: Path) -> None:
    log = InMemoryEventLog()
    reg = ProcessRegistry(event_log=log, content_store=InMemoryContentStore())
    reg.spawn(
        argv=[sys.executable, "-c", "pass"],
        cwd=tmp_path,
        env={},
        command="python -c pass",
        spawned_by_task_id="t",
        trace_id="tr",
    )
    started = next(e for e in log.read("t") if e.type == "BackgroundShellStarted")
    assert started.payload.command == "python -c pass"
