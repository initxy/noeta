"""The notes a permission-mode switch owes the model, as Claude Code sends
them: the shell-first note in auto and bypass-permissions mode (once until
both are left or the task compacts), the exit note on leaving them, and the
plan-exit note on any way out of plan mode."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from noeta.backends.stores.sqlite import SqliteStore
from noeta.core.client import Client
from noeta.plugins.permissions import Permissions
from noeta.plugins.permissions import wording as w
from noeta.testing import ScriptedProvider, reply
from tests.plugins.permissions.helpers import Fake, agent, runtime, with_plans

GUIDANCE = "Auto mode is on"
BYPASS = "Bypass-permissions mode is on"
EXITED = "## Auto mode has ended"
PLAN_EXITED = "## Plan mode has ended"


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SqliteStore]:
    s = SqliteStore(str(tmp_path / "t.db"))
    yield s
    await s.close()


def notes(request: Any) -> str:
    """The system notes this request adds after its newest prompt."""
    last = max(i for i, m in enumerate(request.messages) if m.from_person)
    return "\n".join(m.text for m in request.messages[last:] if m.role == "system")


async def _start(store: SqliteStore, tmp_path: Path, mode: str, *, tools: tuple[str, ...] = (
        "Bash", "Write"), extra: tuple[Any, ...] = (),
                 **kw: Any) -> tuple[Client, str, Any, Any]:
    provider = ScriptedProvider([reply("ok") for _ in range(12)])
    plan_kw = {k: kw.pop(k) for k in ("auto_during_plan",) if k in kw}
    perms = Permissions(mode=mode, plans_dir=str(tmp_path / "plans"), **kw)
    rt = runtime(store, provider, agent([*with_plans(perms, **plan_kw), *extra],
                                        [Fake(t) for t in tools]))
    client = Client(store)
    tid = await client.create("a", "p1", config={"cwd": str(tmp_path)})
    await rt.run_until_idle()
    return client, tid, provider, rt


async def _prompt(client: Client, tid: str, rt: Any, text: str,
                  mode: str | None = None) -> None:
    if mode is not None:
        await client.signal(tid, "permission_mode", {"mode": mode})
    await client.send(tid, text)
    await rt.run_until_idle()


async def test_leaving_auto_mode_is_told_and_coming_back_tells_the_note_again(
        store: SqliteStore, tmp_path: Path) -> None:
    client, tid, provider, rt = await _start(store, tmp_path, "auto")
    assert GUIDANCE in notes(provider.requests[0])
    await _prompt(client, tid, rt, "p2")
    assert GUIDANCE not in notes(provider.requests[1])
    await _prompt(client, tid, rt, "p3", "default")
    assert notes(provider.requests[2]).startswith(EXITED)
    assert w.AUTO_MODE_EXITED in notes(provider.requests[2])
    await _prompt(client, tid, rt, "p4")
    assert EXITED not in notes(provider.requests[3])
    await _prompt(client, tid, rt, "p5", "auto")
    assert GUIDANCE in notes(provider.requests[4])


async def test_auto_to_plan_and_back_sends_only_the_plan_exit_note(
        store: SqliteStore, tmp_path: Path) -> None:
    client, tid, provider, rt = await _start(store, tmp_path, "auto")
    await _prompt(client, tid, rt, "p2", "plan")
    assert EXITED not in notes(provider.requests[1])
    await _prompt(client, tid, rt, "p3", "auto")
    third = notes(provider.requests[2])
    assert PLAN_EXITED in third and GUIDANCE not in third and EXITED not in third
    # The plan file was never written: the note names no file.
    assert "plan file" not in third


async def test_planning_without_the_classifier_leaves_auto_mode(
        store: SqliteStore, tmp_path: Path) -> None:
    # Claude Code with useAutoModeDuringPlan off: entering plan mode from
    # auto mode ends auto mode (the exit note), and coming back to auto mode
    # sends its note again.
    client, tid, provider, rt = await _start(store, tmp_path, "auto", auto_during_plan=False)
    await _prompt(client, tid, rt, "p2", "plan")
    assert EXITED in notes(provider.requests[1])
    await _prompt(client, tid, rt, "p3", "auto")
    third = notes(provider.requests[2])
    assert PLAN_EXITED in third and GUIDANCE in third and EXITED not in third


async def test_planning_from_default_mode_owes_no_auto_mode_note(
        store: SqliteStore, tmp_path: Path) -> None:
    # A plan begun in default mode (no classifier) owes no auto-mode note,
    # nor an exit note.
    client, tid, provider, rt = await _start(store, tmp_path, "default")
    await _prompt(client, tid, rt, "p2", "plan")
    assert GUIDANCE not in notes(provider.requests[1])
    await _prompt(client, tid, rt, "p3", "default")
    third = notes(provider.requests[2])
    assert PLAN_EXITED in third and EXITED not in third and GUIDANCE not in third


async def test_planning_after_auto_then_leaving_for_default_ends_auto_mode(
        store: SqliteStore, tmp_path: Path) -> None:
    client, tid, provider, rt = await _start(store, tmp_path, "auto")
    await _prompt(client, tid, rt, "p2", "plan")
    await _prompt(client, tid, rt, "p3", "default")
    third = notes(provider.requests[2])
    assert PLAN_EXITED in third and EXITED in third


async def test_a_switch_and_back_before_any_request_tells_nothing(
        store: SqliteStore, tmp_path: Path) -> None:
    client, tid, provider, rt = await _start(store, tmp_path, "auto")
    await client.signal(tid, "permission_mode", {"mode": "default"})
    await _prompt(client, tid, rt, "p2", "auto")
    second = notes(provider.requests[1])
    assert EXITED not in second and GUIDANCE not in second


async def test_bypass_permissions_mode_gets_the_shell_first_note(
        store: SqliteStore, tmp_path: Path) -> None:
    _client, _tid, provider, _rt = await _start(store, tmp_path, "bypassPermissions")
    first = notes(provider.requests[0])
    assert BYPASS in first and GUIDANCE not in first


async def test_auto_to_bypass_and_back_sends_no_exit_note(
        store: SqliteStore, tmp_path: Path) -> None:
    # The shell-first note holds in both modes: an exit note followed by the
    # bypass note would tell the model two opposite things.
    client, tid, provider, rt = await _start(store, tmp_path, "auto")
    await _prompt(client, tid, rt, "p2", "bypassPermissions")
    second = notes(provider.requests[1])
    assert EXITED not in second and BYPASS not in second
    await _prompt(client, tid, rt, "p3", "auto")
    third = notes(provider.requests[2])
    assert EXITED not in third and GUIDANCE not in third
    await _prompt(client, tid, rt, "p4", "bypassPermissions")
    await _prompt(client, tid, rt, "p5", "default")
    assert notes(provider.requests[4]).startswith(w.BYPASS_MODE_EXITED)


async def test_leaving_bypass_permissions_mode_is_told(
        store: SqliteStore, tmp_path: Path) -> None:
    client, tid, provider, rt = await _start(store, tmp_path, "bypassPermissions")
    await _prompt(client, tid, rt, "p2", "acceptEdits")
    second = notes(provider.requests[1])
    assert w.BYPASS_MODE_EXITED in second and EXITED not in second
    await _prompt(client, tid, rt, "p3", "bypassPermissions")
    assert BYPASS in notes(provider.requests[2])


async def test_an_agent_without_a_writing_tool_gets_no_auto_mode_note(
        store: SqliteStore, tmp_path: Path) -> None:
    _client, _tid, provider, _rt = await _start(store, tmp_path, "auto", tools=("Bash", "Read"))
    assert GUIDANCE not in notes(provider.requests[0])


async def test_a_compaction_while_planning_brings_the_note_back_at_plan_exit(
        store: SqliteStore, tmp_path: Path) -> None:
    from noeta.plugins.compaction import Compaction
    from noeta.plugins.compaction.extension import command_text

    client, tid, provider, rt = await _start(store, tmp_path, "auto", extra=(Compaction(),))
    await _prompt(client, tid, rt, "p2", "plan")
    await _prompt(client, tid, rt, command_text(""))
    await _prompt(client, tid, rt, "p3", "auto")
    last = notes(provider.requests[-1])
    assert GUIDANCE in last and PLAN_EXITED in last
