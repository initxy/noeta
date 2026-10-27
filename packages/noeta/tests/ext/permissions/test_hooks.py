"""PostToolUse / Notification hooks: argv commands off the task's thread,
fired live only — never again when a later runner folds the same log."""

from __future__ import annotations

import sys
import threading
import time
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from noeta.client import Client
from noeta.ext.permissions import (
    Hooks,
    NotificationRule,
    NotifyHandle,
    Permissions,
    PostToolUseRule,
    pending,
)
from noeta.store.sqlite import SqliteStore
from noeta.testing import ScriptedProvider, reply
from tests.ext.permissions.helpers import Fake, agent, runtime


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SqliteStore]:
    s = SqliteStore(str(tmp_path / "t.db"))
    yield s
    await s.close()


class Recorder:
    def __init__(self) -> None:
        self.ran: list[tuple[str, ...]] = []
        self.lock = threading.Lock()

    def __call__(self, argv: tuple[str, ...]) -> NotifyHandle:
        recorder = self

        class Handle:
            def wait(self) -> None:
                with recorder.lock:
                    recorder.ran.append(argv)

            def cancel(self) -> None:
                return None

        return Handle()

    def settled(self, n: int) -> list[tuple[str, ...]]:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with self.lock:
                if len(self.ran) >= n:
                    break
            time.sleep(0.01)
        time.sleep(0.05)
        with self.lock:
            return list(self.ran)


async def test_hooks_fire_live_and_never_on_resume(store: SqliteStore) -> None:
    rec = Recorder()
    logged: list[str] = []
    hooks = Hooks(post_tool_use=[PostToolUseRule("Re*", ("post", "read")),
                                 PostToolUseRule("Write", ("post", "write"), log=True)],
                  notification=[NotificationRule("approval", ("notify",))],
                  runner=rec, log_sink=logged.append)
    read, write = Fake("Read"), Fake("Write")
    provider = ScriptedProvider([reply("", ("Read", {}), ("Write", {})), reply("done")])
    ag = agent([Permissions(), hooks], [read, write])
    client = Client(store)
    tid = await client.create("a", "go")
    await runtime(store, provider, ag).run_until_idle()
    assert rec.settled(2) == [("post", "read"), ("notify",)]
    [item] = pending(await client.events(tid), tid)
    await client.answer(tid, item.request_id, "allow")
    await runtime(store, provider, ag).run_until_idle()  # a new runner folds the log
    assert rec.settled(3) == [("post", "read"), ("notify",), ("post", "write")]
    assert logged == ["hook: post_tool_use Write"]
    hooks.stop()


async def test_a_denied_call_fires_no_post_hook(store: SqliteStore) -> None:
    rec = Recorder()
    hooks = Hooks(post_tool_use=[PostToolUseRule("*", ("post",))], runner=rec)
    provider = ScriptedProvider([reply("", ("Bash", {})), reply("ok")])
    ag = agent([Permissions(disallowed_tools=["Bash"]), hooks], [Fake("Bash")])
    client = Client(store)
    tid = await client.create("a", "go")
    await runtime(store, provider, ag).run_until_idle()
    assert (await client.get(tid)).waiting_on == "input"
    assert rec.settled(0) == []
    hooks.stop()


async def test_real_commands_run_as_argv(tmp_path: Path, store: SqliteStore) -> None:
    out = tmp_path / "hook.txt"
    script = f"open({str(out)!r}, 'a').write('x')"
    hooks = Hooks(post_tool_use=[PostToolUseRule("Read", (sys.executable, "-c", script))],
                  cwd=str(tmp_path))
    provider = ScriptedProvider([reply("", ("Read", {})), reply("ok")])
    client = Client(store)
    await client.create("a", "go")
    await runtime(store, provider, agent([hooks], [Fake("Read")])).run_until_idle()
    deadline = time.monotonic() + 10
    while not out.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert out.read_text() == "x"
    hooks.stop()


def test_hook_configuration_is_checked() -> None:
    with pytest.raises(ValueError, match="does nothing"):
        Hooks(post_tool_use=[PostToolUseRule("Read")])
    with pytest.raises(ValueError, match="approval"):
        Hooks(notification=[NotificationRule("question", ("x",))])
    with pytest.raises(ValueError, match="argv"):
        Hooks(post_tool_use=[PostToolUseRule("Read", "echo hi")])  # type: ignore[arg-type]
