"""Permission modes, the shell and WebFetch gates, "always" rules, guards,
``can_use_tool`` and the pending-request listing, end to end."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from noeta.backends.stores.sqlite import SqliteStore
from noeta.core.client import Client
from noeta.core.events import ExtensionRecorded, ToolFinished, Waiting
from noeta.core.pending import always_option, pending, pending_tree
from noeta.core.tool import Park, ToolContext, ToolOutput, text_output
from noeta.plugins.permissions import (
    Budget,
    MatchArg,
    Permissions,
    PreToolUseRule,
    UntrustedProjectShellAllowlistWarning,
)
from noeta.testing import ScriptedProvider, reply
from tests.plugins.permissions.helpers import Fake, agent, events, runtime


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SqliteStore]:
    s = SqliteStore(str(tmp_path / "t.db"))
    yield s
    await s.close()


def results(evs: list[Any]) -> list[tuple[str, bool]]:
    return [(e.result.text, e.result.is_error) for e in evs if isinstance(e, ToolFinished)]


async def start(store: SqliteStore, provider: ScriptedProvider, perms: Permissions,
                tools: list[Any], *, config: dict[str, Any] | None = None
                ) -> tuple[Client, str, Any]:
    ag = agent([perms], tools)
    rt = runtime(store, provider, ag)
    client = Client(store)
    tid = await client.create("a", "go", config=config)
    await rt.run_until_idle()
    return client, tid, rt


# ------------------------------------------------------------------ modes


async def test_default_mode_asks_for_risky_tools_only(store: SqliteStore) -> None:
    read, write = Fake("Read"), Fake("Write")
    provider = ScriptedProvider([reply("", ("Read", {}), ("Write", {"file_path": "x"})),
                                 reply("done")])
    client, tid, rt = await start(store, provider, Permissions(), [read, write])
    last = (await events(client, tid))[-1]
    assert isinstance(last, Waiting) and last.on == "approval"
    [rid] = last.ids
    assert last.detail[rid]["prompt"] == "tool 'Write' requires human approval"
    assert len(read.runs) == 1 and not write.runs
    [item] = pending(await client.events(tid), tid)
    assert (item.kind, item.tool, item.input) == ("approval", "Write", {"file_path": "x"})
    assert item.detail["always"] == {"kind": "tool", "tool": "Write"}
    await client.answer(tid, rid, "allow")
    await rt.run_until_idle()
    assert len(write.runs) == 1
    assert pending(await client.events(tid), tid) == []


async def test_accept_edits_exempts_edit_and_write(store: SqliteStore) -> None:
    write, kill = Fake("Write"), Fake("TaskStop")
    provider = ScriptedProvider([reply("", ("Write", {}), ("TaskStop", {})), reply("ok")])
    client, tid, _ = await start(store, provider, Permissions(mode="acceptEdits"), [write, kill])
    last = (await events(client, tid))[-1]
    assert isinstance(last, Waiting) and last.on == "approval" and len(last.ids) == 1
    assert len(write.runs) == 1 and not kill.runs


async def test_bypass_never_asks_and_a_task_can_pick_its_mode(store: SqliteStore) -> None:
    bash = Fake("Bash")
    provider = ScriptedProvider([reply("", ("Bash", {"command": "rm -rf / | x"})), reply("ok")])
    client, tid, _ = await start(store, provider, Permissions(), [bash],
                                 config={"permission_mode": "bypassPermissions"})
    assert len(bash.runs) == 1
    assert (await client.get(tid)).waiting_on == "input"


def test_bad_configuration_raises() -> None:
    with pytest.raises(ValueError, match="permission_mode"):
        Permissions(mode="planning")
    with pytest.raises(ValueError, match="ambiguous"):
        Permissions(budget=Budget(max_iterations=2), max_turns=3)
    with pytest.raises(ValueError, match=r"hostname|scheme"):
        Permissions(webfetch_allowed_hosts=["https://x.com"])
    with pytest.raises(ValueError, match="regex"):
        Permissions(pre_tool_use=[PreToolUseRule("Bash", "deny", MatchArg(("command",),
                                                                           "regex", "("))])


# ------------------------------------------------------------------ shell gate


async def test_shell_gate_runs_allowlisted_commands_and_asks_for_the_rest(
        store: SqliteStore) -> None:
    bash, monitor = Fake("Bash"), Fake("Monitor")
    provider = ScriptedProvider([reply(
        "", ("Bash", {"command": "git status"}), ("Bash", {"command": "npm start"}),
        ("Bash", {"command": ""}), ("Monitor", {"command": "tail -f log"}),
        ("Bash", {"command": "make"}))])
    client, tid, _ = await start(store, provider, Permissions(
        shell_allowlist=[{"program": "make"}]), [bash, monitor])
    last = (await events(client, tid))[-1]
    assert isinstance(last, Waiting) and len(last.ids) == 3
    assert {d["prompt"] for d in last.detail.values()} == {
        "tool 'Bash' call requires human approval", "tool 'Monitor' call requires human approval"}
    assert [r["command"] for r in bash.runs] == ["git status", "make"]


async def test_always_remembers_the_shown_rule_and_writes_the_trusted_project_file(
        store: SqliteStore, tmp_path: Path) -> None:
    bash = Fake("Bash")
    provider = ScriptedProvider([reply("", ("Bash", {"command": "npm start --x"})),
                                 reply("", ("Bash", {"command": "npm start --y"})),
                                 reply("done")])
    client, tid, rt = await start(store, provider, Permissions(trusted_workspace=True), [bash],
                                  config={"cwd": str(tmp_path)})
    [item] = pending(await client.events(tid), tid)
    assert item.detail["always"] == {"kind": "shell", "spec": {"rule": "npm start *"}}
    assert item.detail["always_label"] == "Bash(npm start *)"
    assert always_option(item.detail) == "Always allow Bash(npm start *)"
    await client.answer(tid, item.request_id, "allow", always=True)
    await rt.run_until_idle()
    assert [r["command"] for r in bash.runs] == ["npm start --x", "npm start --y"]
    rules = [e for e in await events(client, tid)
             if isinstance(e, ExtensionRecorded) and e.kind == "rule"]
    assert len(rules) == 1
    saved = json.loads((tmp_path / ".noeta" / "shell-allowlist.json").read_text())
    assert saved == [{"rule": "npm start *"}]


async def test_always_on_a_destructive_command_remembers_only_that_command(
        store: SqliteStore, tmp_path: Path) -> None:
    bash = Fake("Bash")
    provider = ScriptedProvider([reply("", ("Bash", {"command": "rm -rf build"})),
                                 reply("", ("Bash", {"command": "rm -rf /"})),
                                 reply("done")])
    client, tid, rt = await start(store, provider, Permissions(trusted_workspace=True), [bash],
                                  config={"cwd": str(tmp_path)})
    [item] = pending(await client.events(tid), tid)
    assert item.detail["always_label"] == "Bash(rm -rf build)"
    await client.answer(tid, item.request_id, "allow", always=True)
    await rt.run_until_idle()
    assert [r["command"] for r in bash.runs] == ["rm -rf build"]
    [again] = pending(await client.events(tid), tid)
    assert again.input == {"command": "rm -rf /"}
    saved = json.loads((tmp_path / ".noeta" / "shell-allowlist.json").read_text())
    assert saved == [{"rule": "rm -rf build"}]


def test_always_option_names_the_rule_or_offers_none() -> None:
    assert always_option({"always": None}) is None
    assert always_option({}) == "Always allow"
    assert always_option({"always": {"kind": "tool", "tool": "Write"},
                          "always_label": "Write"}) == "Always allow Write"


async def test_project_file_needs_a_trusted_workspace(store: SqliteStore,
                                                      tmp_path: Path) -> None:
    (tmp_path / ".noeta").mkdir()
    (tmp_path / ".noeta" / "shell-allowlist.json").write_text('[{"program": "make"}]')
    bash = Fake("Bash")
    provider = ScriptedProvider([reply("", ("Bash", {"command": "make"}),
                                       ("Bash", {"command": "pytest -q"})), reply("ok")])
    with pytest.warns(UntrustedProjectShellAllowlistWarning):
        client, tid, _rt = await start(store, provider, Permissions(), [bash],
                                      config={"cwd": str(tmp_path)})
    last = (await events(client, tid))[-1]
    assert isinstance(last, Waiting) and len(last.ids) == 2  # file and test runner ignored

    provider2 = ScriptedProvider([reply("", ("Bash", {"command": "make"}),
                                        ("Bash", {"command": "pytest -q"})), reply("ok")])
    bash2 = Fake("Bash")
    client, tid, _ = await start(store, provider2, Permissions(trusted_workspace=True), [bash2],
                                 config={"cwd": str(tmp_path)})
    assert [r["command"] for r in bash2.runs] == ["make", "pytest -q"]


async def test_always_without_trust_stays_in_the_task(store: SqliteStore,
                                                      tmp_path: Path) -> None:
    bash = Fake("Bash")
    provider = ScriptedProvider([reply("", ("Bash", {"command": "npm start"})),
                                 reply("", ("Bash", {"command": "npm start"})), reply("ok")])
    client, tid, rt = await start(store, provider, Permissions(), [bash],
                                  config={"cwd": str(tmp_path)})
    [item] = pending(await client.events(tid), tid)
    await client.answer(tid, item.request_id, "allow", always=True)
    await rt.run_until_idle()
    assert len(bash.runs) == 2
    assert not (tmp_path / ".noeta").exists()


async def test_disallowing_bash_removes_its_companions(store: SqliteStore) -> None:
    tools = [Fake(n) for n in ("Bash", "Monitor", "TaskStop", "Read")]
    provider = ScriptedProvider([reply("", ("TaskStop", {})), reply("ok")])
    client, tid, _ = await start(store, provider, Permissions(disallowed_tools=["Bash"]), tools)
    assert [t.name for t in provider.requests[0].tools] == ["Read"]
    assert results(await events(client, tid)) == [("tool 'TaskStop' denied by policy", True)]


# ------------------------------------------------------------------ web gate


async def test_webfetch_asks_for_unlisted_hosts_and_remembers_always(
        store: SqliteStore) -> None:
    fetch = Fake("WebFetch")
    provider = ScriptedProvider([
        reply("", ("WebFetch", {"url": "https://docs.example.com/a"}),
              ("WebFetch", {"url": "https://example.com@evil.test/"})),
        reply("", ("WebFetch", {"url": "https://evil.test/again"})), reply("ok")])
    client, tid, rt = await start(store, provider, Permissions(
        webfetch_allowed_hosts=["*.example.com"]), [fetch])
    [item] = pending(await client.events(tid), tid)
    assert item.prompt == "tool 'WebFetch' call requires human approval"
    assert item.detail["always"] == {"kind": "host", "host": "evil.test"}
    await client.answer(tid, item.request_id, "allow", always=True)
    await rt.run_until_idle()
    assert len(fetch.runs) == 3


# ------------------------------------------------------------------ guards


async def test_budget_caps_tool_calls_and_iterations(store: SqliteStore) -> None:
    read = Fake("Read")
    provider = ScriptedProvider([reply("", ("Read", {}), ("Read", {"n": 2})),
                                 reply("", ("Read", {"n": 3})), reply("ok")])
    client, tid, _ = await start(store, provider, Permissions(
        budget=Budget(max_tool_calls=1, max_iterations=1)), [read])
    assert sorted(results(await events(client, tid))) == [
        ("max_iterations=1 exceeded", True), ("max_tool_calls=1 reached", True),
        ("ran Read", False)]


async def test_budget_caps_spawn_depth(store: SqliteStore) -> None:
    task = Fake("Task")
    provider = ScriptedProvider([reply("", ("Task", {})), reply("ok")])
    client, tid, _ = await start(store, provider, Permissions(), [task],
                                 config={"subtask_depth": 3})
    assert results(await events(client, tid)) == [("max_subtask_depth=3 reached", True)]


async def test_repetition_guard_asks_on_the_third_identical_call(store: SqliteStore) -> None:
    read = Fake("Read")
    provider = ScriptedProvider([reply("", ("Read", {"p": 1})), reply("", ("Read", {"p": 1})),
                                 reply("", ("Read", {"p": 1}))])
    client, tid, _ = await start(store, provider, Permissions(repetition_threshold=3), [read])
    [item] = pending(await client.events(tid), tid)
    assert item.prompt == ("tool 'Read' called with identical arguments 3 times in a row "
                           "(repetition threshold 3); suspected loop")
    assert item.detail["always"] is None


async def test_pre_tool_use_rules_tighten_and_bash_rules_cover_monitor(
        store: SqliteStore) -> None:
    monitor, read = Fake("Monitor"), Fake("Read")
    rules = [PreToolUseRule("Read", "allow"),
             PreToolUseRule("Bash", "deny", MatchArg(("command",), "contains", "rm "),
                            reason="no rm")]
    provider = ScriptedProvider([reply("", ("Monitor", {"command": "rm x"}), ("Read", {})),
                                 reply("ok")])
    client, tid, _ = await start(store, provider, Permissions(mode="bypassPermissions",
                                                              pre_tool_use=rules),
                                 [monitor, read])
    assert sorted(results(await events(client, tid))) == [("no rm", True), ("ran Read", False)]


async def test_deny_wins_over_an_ask(store: SqliteStore) -> None:
    write = Fake("Write")
    provider = ScriptedProvider([reply("", ("Write", {"file_path": "/etc/x"})), reply("ok")])
    rule = PreToolUseRule("W*", "deny", MatchArg(("file_path",), "regex", "^/etc"))
    client, tid, _ = await start(store, provider, Permissions(pre_tool_use=[rule]), [write])
    assert results(await events(client, tid)) == [("hook rule on 'W*'", True)]


async def test_can_use_tool_answers_approvals(store: SqliteStore) -> None:
    write = Fake("Write")
    seen: list[str] = []

    async def decide(tool: str, args: dict[str, Any]) -> bool:
        seen.append(tool)
        return args.get("ok") is True

    provider = ScriptedProvider([reply("", ("Write", {"ok": True}), ("Write", {"ok": False})),
                                 reply("done")])
    client, tid, _ = await start(store, provider, Permissions(can_use_tool=decide), [write])
    evs = await events(client, tid)
    assert sorted(results(evs)) == [("denied by human", True), ("ran Write", False)]
    autos = [e.data for e in evs if isinstance(e, ExtensionRecorded) and e.kind == "auto"]
    assert [a["decision"] for a in autos] == ["allow", "deny"]
    assert all(a["resolver"] == "can_use_tool" for a in autos)
    assert seen == ["Write", "Write"]


# ------------------------------------------------------------------ pending tree


class Spawn:
    name = "Spawn"
    description = "spawn"
    input_schema: dict[str, Any] = {"type": "object"}
    read_only = False
    clearable = False

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        assert ctx.spawn is not None
        return ToolOutput(park=Park(on="children", keys=[ctx.spawn("child", "work")]))

    async def resume(self, input: dict[str, Any], ctx: ToolContext, outcome: Any) -> ToolOutput:
        return text_output("child done")


async def test_a_sub_agents_approval_surfaces_at_the_root(store: SqliteStore) -> None:
    write = Fake("Write")
    provider = ScriptedProvider([reply("", ("Spawn", {})), reply("", ("Write", {})),
                                 reply("wrote"), reply("all done")])
    parent = agent([Permissions(risk={"Spawn": "low"})], [Spawn()])
    child = agent([Permissions()], [write], name="child", one_shot=True)
    rt = runtime(store, provider, parent, child)
    client = Client(store)
    tid = await client.create("a", "go")
    await rt.run_until_idle()
    [item] = await pending_tree(client, tid)
    assert item.task_id != tid and item.tool == "Write"
    await client.answer(item.task_id, item.request_id, "allow")
    await rt.run_until_idle()
    assert len(write.runs) == 1
    assert (await client.get(tid)).waiting_on == "input"
