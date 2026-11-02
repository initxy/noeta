"""Claude Code's permission system end to end: modes, rules, working
directories, protected paths, approval cards, remembered rules, noeta's
guards and ``can_use_tool``."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from noeta.backends.stores.sqlite import SqliteStore
from noeta.core.agent import Agent
from noeta.core.client import Client
from noeta.core.events import ExtensionRecorded, ToolFinished, Waiting
from noeta.core.tool import Park, ToolContext, ToolOutput, text_output
from noeta.host.pending import pending, pending_tree
from noeta.plugins.permissions import (
    MODE_SIGNAL,
    Budget,
    MatchArg,
    Permissions,
    PreToolUseRule,
    next_mode,
)
from noeta.plugins.subtasks import Subtasks
from noeta.plugins.subtasks.extension import SubagentSpec
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
                tools: list[Any], *, cwd: Path | None = None,
                config: dict[str, Any] | None = None) -> tuple[Client, str, Any]:
    ag = agent([perms], tools)
    rt = runtime(store, provider, ag)
    client = Client(store)
    conf = dict(config or {})
    if cwd is not None:
        conf["cwd"] = str(cwd)
    tid = await client.create("a", "go", config=conf)
    await rt.run_until_idle()
    return client, tid, rt


def asked(evs: list[Any]) -> dict[str, dict[str, Any]]:
    last = evs[-1]
    assert isinstance(last, Waiting) and last.on == "approval", last
    return last.detail


# ------------------------------------------------------------------ default mode


async def test_default_mode_reads_freely_and_asks_to_write(store: SqliteStore,
                                                           tmp_path: Path) -> None:
    read, write = Fake("Read"), Fake("Write")
    provider = ScriptedProvider([
        reply("", ("Read", {"file_path": "a.py"}), ("Write", {"file_path": "x.md"})),
        reply("", ("Write", {"file_path": "y.md"})), reply("done")])
    client, tid, rt = await start(store, provider, Permissions(mode="default"), [read, write],
                                  cwd=tmp_path)
    [detail] = asked(await events(client, tid)).values()
    assert detail["title"] == "Create file" and not detail["classify"]
    assert [c["id"] for c in detail["choices"]] == ["allow", "mode:acceptEdits", "deny"]
    assert len(read.runs) == 1 and not write.runs
    [item] = pending(await client.events(tid), tid)
    # "Yes, and switch to accept edits": the next write runs without asking.
    await client.answer(tid, item.request_id, "allow", data={"mode": "acceptEdits"})
    await rt.run_until_idle()
    assert [r["file_path"] for r in write.runs] == ["x.md", "y.md"]
    # A person allowed the first; the mode allowed the second outright.
    assert write.allowed == [(True, False), (False, True)]


async def test_read_only_commands_run_and_others_offer_a_rule(store: SqliteStore,
                                                              tmp_path: Path) -> None:
    bash = Fake("Bash")
    provider = ScriptedProvider([
        reply("", ("Bash", {"command": "git status && cat a | wc -l 2>/dev/null"}),
              ("Bash", {"command": "npm run build --prod"})),
        reply("", ("Bash", {"command": "npm run build --dev"})), reply("ok")])
    client, tid, rt = await start(store, provider,
                                  Permissions(mode="default", trusted_workspace=True), [bash],
                                  cwd=tmp_path)
    [detail] = asked(await events(client, tid)).values()
    assert detail["always"] == ["Bash(npm run *)"]
    assert [c["id"] for c in detail["choices"]] == ["allow", "always", "mode:auto", "deny"]
    [item] = pending(await client.events(tid), tid)
    await client.answer(tid, item.request_id, "allow", always=True)
    await rt.run_until_idle()
    assert len(bash.runs) == 3
    saved = json.loads((tmp_path / ".noeta" / "settings.local.json").read_text())
    assert saved == {"permissions": {"allow": ["Bash(npm run *)"]}}


@pytest.mark.parametrize("trusted", [False, True])
async def test_always_answers_reach_the_project_only_when_the_tasks_cwd_is_trusted(
        store: SqliteStore, tmp_path: Path, trusted: bool) -> None:
    bash = Fake("Bash")
    provider = ScriptedProvider([reply("", ("Bash", {"command": "npm run build"})),
                                 reply("ok")])
    project = tmp_path / "p"
    project.mkdir()
    seen: list[str] = []

    def trusts(directory: str) -> bool:
        seen.append(directory)
        return trusted

    client, tid, rt = await start(store, provider,
                                  Permissions(mode="default", trusted_workspace=trusts),
                                  [bash], cwd=project)
    [item] = pending(await client.events(tid), tid)
    await client.answer(tid, item.request_id, "allow", always=True)
    await rt.run_until_idle()
    assert len(bash.runs) == 1 and str(project) in seen
    assert (project / ".noeta" / "settings.local.json").exists() is trusted


async def test_reads_outside_ask_and_outside_writes_are_refused(store: SqliteStore,
                                                                tmp_path: Path) -> None:
    read, write = Fake("Read"), Fake("Write")
    other = tmp_path.parent / "elsewhere"
    provider = ScriptedProvider([
        reply("", ("Read", {"file_path": str(other / "a")}),
              ("Read", {"file_path": str(tmp_path.parent / "lib" / "b")}),
              ("Write", {"file_path": "/etc/x"})), reply("ok")])
    client, tid, _ = await start(store, provider, Permissions(
        mode="acceptEdits", additional_directories=["../lib"]), [read, write], cwd=tmp_path)
    evs = await events(client, tid)
    assert [r["file_path"] for r in read.runs] == [str(tmp_path.parent / "lib" / "b")]
    [(text, err)] = [x for x in results(evs) if x[1]]
    assert err and "only writes inside the working directories" in text
    [detail] = asked(evs).values()
    assert detail["always"] == [f"Read(/{other}/**)"]


@pytest.mark.parametrize("mode", ["acceptEdits", "bypassPermissions"])
async def test_protected_paths_ask_even_when_edits_are_accepted(
        store: SqliteStore, tmp_path: Path, mode: str) -> None:
    write, bash = Fake("Write"), Fake("Bash")
    provider = ScriptedProvider([reply(
        "", ("Write", {"file_path": ".git/hooks/pre-commit"}),
        ("Write", {"file_path": ".claude/skills/x/SKILL.md"}),
        ("Write", {"file_path": "sub/.bashrc"}),
        ("Bash", {"command": "cp a .noeta/agents/main.md"}))])
    client, tid, _ = await start(store, provider, Permissions(mode=mode), [write, bash],
                                 cwd=tmp_path)
    details = asked(await events(client, tid))
    assert len(details) == 3
    assert [r["file_path"] for r in write.runs] == [".claude/skills/x/SKILL.md"]
    assert not bash.runs


@pytest.mark.parametrize("mode", ["bypassPermissions", "auto"])
@pytest.mark.parametrize("call", [
    ("Write", {"file_path": ".git/hooks/pre-commit"}),
    ("Bash", {"command": "echo x > .git/hooks/pre-commit"}),
    ("Bash", {"command": "echo {} > .noeta/settings.local.json"}),
])
async def test_a_write_to_a_protected_path_asks_in_bypass_and_goes_to_the_classifier_in_auto(
        store: SqliteStore, tmp_path: Path, mode: str, call: tuple[str, dict[str, Any]]) -> None:
    """As Claude Code's safety check: bypass mode does not skip it, and in
    auto mode the classifier may approve it (``classifierApprovable``)."""
    from noeta.plugins.permissions.decision import Decision, in_mode

    tool = Fake(call[0])
    perms = Permissions(mode="bypassPermissions" if mode == "bypassPermissions" else "default")
    client, tid, _ = await start(store, ScriptedProvider([reply("", call)]), perms, [tool],
                                 cwd=tmp_path)
    if mode == "bypassPermissions":
        assert len(asked(await events(client, tid))) == 1 and not tool.runs
    protected = Decision("ask", "x", why="protected")
    assert in_mode(mode, protected, call[0]).classify == (mode == "auto")
    assert in_mode(mode, protected, call[0]).kind == "ask"


@pytest.mark.parametrize("mode", ["acceptEdits", "bypassPermissions"])
async def test_a_symlink_into_a_protected_directory_still_asks(
        store: SqliteStore, tmp_path: Path, mode: str) -> None:
    """The tools write where a link points: a link into ``.git`` or
    ``.noeta`` is judged by its target."""
    (tmp_path / ".git" / "hooks").mkdir(parents=True)
    (tmp_path / ".noeta").mkdir()
    (tmp_path / "hooks").symlink_to(tmp_path / ".git" / "hooks")
    (tmp_path / "conf").symlink_to(tmp_path / ".noeta")
    write, bash = Fake("Write"), Fake("Bash")
    provider = ScriptedProvider([reply(
        "", ("Write", {"file_path": "hooks/pre-commit"}),
        ("Write", {"file_path": "conf/settings.local.json"}),
        ("Bash", {"command": "cp evil hooks/post-checkout"}),
        ("Write", {"file_path": "plain.txt"}))])
    client, tid, _ = await start(store, provider, Permissions(mode=mode), [write, bash],
                                 cwd=tmp_path)
    details = asked(await events(client, tid))
    assert len(details) == 3
    assert [r["file_path"] for r in write.runs] == ["plain.txt"]
    assert not bash.runs


@pytest.mark.parametrize("command", [
    "cp -r dir c && cp payload c/link", "cp -r dir c; cp payload c/link/x",
    "mv dir c && cp payload c/link", "cp -a dir/link out && sed -i s/a/b/ out",
    "cp -r dir c && cd c && cp ../payload link", "mv dir/link l && touch l",
    "cp -r dir c && cat c/link",
])
async def test_a_link_an_earlier_part_copies_is_not_written_through(
        store: SqliteStore, tmp_path: Path, command: str) -> None:
    """Each target is judged before anything runs: a link the same command
    copies or moves first is not on disk yet, so a later write under it asks."""
    outside = tmp_path / "outside"
    outside.mkdir()
    ws = tmp_path / "ws"
    (ws / "dir").mkdir(parents=True)
    (ws / ".git" / "hooks").mkdir(parents=True)
    (ws / "payload").write_text("x")
    (ws / "dir" / "link").symlink_to(outside if "link/x" in command else outside / "f")
    bash = Fake("Bash")
    provider = ScriptedProvider([reply("", ("Bash", {"command": command}))])
    client, tid, _ = await start(store, provider, Permissions(mode="acceptEdits"), [bash],
                                 cwd=ws)
    assert asked(await events(client, tid)) and not bash.runs


async def test_a_link_into_git_hooks_copied_in_the_same_command_asks(
        store: SqliteStore, tmp_path: Path) -> None:
    (tmp_path / ".git" / "hooks").mkdir(parents=True)
    (tmp_path / "dir").mkdir()
    (tmp_path / "dir" / "link").symlink_to(tmp_path / ".git" / "hooks" / "pre-commit")
    bash = Fake("Bash")
    provider = ScriptedProvider([reply(
        "", ("Bash", {"command": "cp -r dir c && cp payload c/link"}))])
    client, tid, _ = await start(store, provider, Permissions(mode="acceptEdits"), [bash],
                                 cwd=tmp_path)
    assert asked(await events(client, tid)) and not bash.runs


async def test_a_write_after_a_part_a_rule_lets_through_asks(store: SqliteStore,
                                                             tmp_path: Path) -> None:
    """What ``npm run build`` leaves behind (a link, perhaps) is unknown."""
    bash = Fake("Bash")
    provider = ScriptedProvider([reply(
        "", ("Bash", {"command": "npm run build && cp payload dist/x"}))])
    perms = Permissions(mode="acceptEdits", allow=["Bash(npm run build)"])
    client, tid, _ = await start(store, provider, perms, [bash], cwd=tmp_path)
    assert asked(await events(client, tid)) and not bash.runs


async def test_ordinary_multi_part_file_commands_still_run(store: SqliteStore,
                                                           tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("a")
    bash = Fake("Bash")
    commands = ["mkdir -p out && cp a.txt out/", "mkdir -p out/sub && touch out/sub/x",
                "cp a.txt b.txt && sed -i s/a/b/ b.txt", "rm -f c.txt && cp a.txt c.txt"]
    provider = ScriptedProvider([reply("", *(("Bash", {"command": c}) for c in commands)),
                                 reply("ok")])
    await start(store, provider, Permissions(mode="acceptEdits"), [bash], cwd=tmp_path)
    assert [r["command"] for r in bash.runs] == commands


def _linked_ssh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A workspace whose ``docs`` links to the home directory's ``.ssh``."""
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh" / "id_rsa").write_text("secret")
    monkeypatch.setenv("HOME", str(home))
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "README").write_text("hi")
    (ws / "docs").symlink_to(home / ".ssh")
    return ws


async def test_a_read_through_a_link_out_of_the_workspace_asks(
        store: SqliteStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ws = _linked_ssh(tmp_path, monkeypatch)
    read, bash = Fake("Read"), Fake("Bash")
    provider = ScriptedProvider([reply(
        "", ("Read", {"file_path": "docs/id_rsa"}), ("Bash", {"command": "cat docs/id_rsa"}),
        ("Bash", {"command": "cd docs && cat id_rsa"}), ("Bash", {"command": "ls docs/"}),
        ("Read", {"file_path": "README"}), ("Bash", {"command": "cat README"}))])
    client, tid, _ = await start(store, provider, Permissions(mode="default"), [read, bash],
                                 cwd=ws)
    details = asked(await events(client, tid))
    assert len(details) == 4
    assert any(str(tmp_path / "home" / ".ssh" / "id_rsa") in d["prompt"]
               for d in details.values())
    assert [r["file_path"] for r in read.runs] == ["README"]
    assert [r["command"] for r in bash.runs] == ["cat README"]


async def test_a_read_deny_rule_holds_through_a_link(
        store: SqliteStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ws = _linked_ssh(tmp_path, monkeypatch)
    read, write = Fake("Read"), Fake("Write")
    provider = ScriptedProvider([reply(
        "", ("Read", {"file_path": "docs/id_rsa"}), ("Write", {"file_path": "docs/config"}),
        ("Read", {"file_path": "README"})), reply("ok")])
    perms = Permissions(mode="bypassPermissions", deny=["Read(~/.ssh/**)", "Edit(~/.ssh/**)"])
    client, tid, _ = await start(store, provider, perms, [read, write], cwd=ws)
    denied = [text for text, err in results(await events(client, tid)) if err]
    assert len(denied) == 2 and "Read(~/.ssh/**)" in denied[0] and "Edit(~/.ssh/**)" in denied[1]
    assert [r["file_path"] for r in read.runs] == ["README"] and not write.runs


async def test_a_directory_change_in_the_command_moves_its_paths(store: SqliteStore,
                                                                 tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    bash = Fake("Bash")
    provider = ScriptedProvider([reply(
        "", ("Bash", {"command": "cd .git && sed -i s/a/b/ config"}),
        ("Bash", {"command": "cd src; rm ../.gitmodules"}),
        ("Bash", {"command": "cd src && touch new.py"}))])
    client, tid, _ = await start(store, provider, Permissions(mode="acceptEdits"), [bash],
                                 cwd=tmp_path)
    assert len(asked(await events(client, tid))) == 2
    assert [r["command"] for r in bash.runs] == ["cd src && touch new.py"]


async def test_the_shells_directory_resolves_relative_paths(tmp_path: Path) -> None:
    """A ``cd .git`` in one call moves where the next call's paths land."""
    from types import SimpleNamespace

    from noeta.plugins.permissions import scope as sc
    from noeta.plugins.permissions.shell_check import shell_scope

    scope = sc.Scope(dirs=(str(tmp_path),), home="/home/u")
    shell = SimpleNamespace(cwd=str(tmp_path / ".git"))
    view: Any = SimpleNamespace(ext_state=lambda name: shell if name == "shell" else None)
    here = shell_scope(view, scope)
    assert here.cwd == str(tmp_path / ".git")
    [part] = sc.parts("sed -i s/a/b/ config", here) or []
    assert part.writes == (str(tmp_path / ".git" / "config"),)
    shell.cwd = "/etc"  # outside: the working directory stands
    assert shell_scope(view, scope).cwd == str(tmp_path)


async def test_disabled_bypass_mode_holds_against_a_task_config(store: SqliteStore,
                                                                 tmp_path: Path) -> None:
    write = Fake("Write")
    provider = ScriptedProvider([reply("", ("Write", {"file_path": "a"})), reply("ok")])
    perms = Permissions(mode="default", disable_bypass_mode=True)
    client, tid, _ = await start(store, provider, perms, [write], cwd=tmp_path,
                                 config={"permission_mode": "bypassPermissions"})
    assert asked(await events(client, tid)) and not write.runs
    modes = [e.data["mode"] for e in await events(client, tid)
             if isinstance(e, ExtensionRecorded) and e.kind == "mode"]
    assert modes == ["default"]


async def test_a_fork_of_a_bypass_task_does_not_bypass_where_bypass_is_disabled(
        store: SqliteStore, tmp_path: Path) -> None:
    client = Client(store)
    # The source ran in bypass where it is allowed (the CLI).
    allowed = runtime(store, ScriptedProvider([reply("ok")]),
                      agent([Permissions(mode="default")], [Fake("Write")]))
    src = await client.create("a", "go", config={
        "cwd": str(tmp_path), "permission_mode": "bypassPermissions"})
    await allowed.run_until_idle()
    # A server with bypass disabled forks it.
    write = Fake("Write")
    server = runtime(store, ScriptedProvider([reply("", ("Write", {"file_path": "x"})),
                                              reply("ok")]),
                     agent([Permissions(mode="default", disable_bypass_mode=True)], [write]))
    fork = await client.create("a", "write", config={"cwd": str(tmp_path)}, history_from=src)
    await server.run_until_idle()
    assert asked(await events(client, fork)) and not write.runs


def test_settings_files_can_disable_bypass_mode(tmp_path: Path) -> None:
    from noeta.cli.settings_files import load, permission_arguments

    user, project = tmp_path / "user.json", tmp_path / "project.json"
    user.write_text(json.dumps({"permissions": {"disableBypassPermissionsMode": "disable"}}))
    project.write_text(json.dumps({"permissions": {"defaultMode": "bypassPermissions",
                                                   "disableBypassPermissionsMode": "enable"}}))
    args = permission_arguments(load([user, project]), {"mode", "disable_bypass_mode"})
    assert args == {"disable_bypass_mode": True}
    Permissions(**args)


def test_settings_files_can_keep_plan_mode_off_the_classifier(tmp_path: Path) -> None:
    """Claude Code's top-level ``useAutoModeDuringPlan``: any file that turns
    it off turns it off."""
    from noeta.cli.settings_files import load, permission_arguments

    user, project = tmp_path / "user.json", tmp_path / "project.json"
    user.write_text(json.dumps({"useAutoModeDuringPlan": False}))
    project.write_text(json.dumps({"useAutoModeDuringPlan": True}))
    args = permission_arguments(load([user, project]), {"auto_during_plan"})
    assert args == {"auto_during_plan": False}
    assert not Permissions(**args).auto_during_plan
    assert permission_arguments(load([project]), {"auto_during_plan"}) == {}


@pytest.mark.parametrize(("command", "rule"), [
    ("git -c core.pager=sh log", "Bash(git -c core.pager=sh log)"),
    ("git push origin main", "Bash(git push *)"),
    ("awk NR==1 a", "Bash(awk NR==1 a)"),
    ("find . -name x", "Bash(find . -name x)"),
    ("sed -n 1p a", "Bash(sed -n 1p a)"),
    ("npm run build", "Bash(npm run *)"),
])
def test_always_remembers_no_more_than_the_command_shows(command: str, rule: str) -> None:
    from noeta.plugins.permissions import scope as sc
    from noeta.plugins.permissions.shell_check import shell_always as _shell_always

    scope = sc.Scope(dirs=("/proj",), home="/home/u")
    always, _label = _shell_always(sc.parts(command, scope) or [], scope)
    assert always == (rule,)


async def test_accept_edits_runs_file_commands_inside(store: SqliteStore,
                                                      tmp_path: Path) -> None:
    bash = Fake("Bash")
    provider = ScriptedProvider([reply(
        "", ("Bash", {"command": "mkdir -p out && touch out/x && ls out"}),
        ("Bash", {"command": "rm -rf /tmp/elsewhere"}),
        ("Bash", {"command": f"rm -r {tmp_path}"}))])
    client, tid, _ = await start(store, provider, Permissions(mode="acceptEdits"), [bash],
                                 cwd=tmp_path)
    assert len(asked(await events(client, tid))) == 2
    assert [r["command"] for r in bash.runs] == ["mkdir -p out && touch out/x && ls out"]


async def test_always_allow_access_to_a_directory(store: SqliteStore, tmp_path: Path) -> None:
    bash = Fake("Bash")
    provider = ScriptedProvider([reply("", ("Bash", {"command": "rm notes/todo.md"})),
                                 reply("", ("Bash", {"command": "rm notes/other.md"})),
                                 reply("ok")])
    client, tid, rt = await start(store, provider, Permissions(mode="default"), [bash],
                                  cwd=tmp_path)
    [detail] = asked(await events(client, tid)).values()
    assert detail["always"] == [f"Edit(/{tmp_path}/notes/**)"]
    assert detail["always_label"].endswith(f"{tmp_path}/notes from this project")
    [item] = pending(await client.events(tid), tid)
    await client.answer(tid, item.request_id, "allow", always=True)
    await rt.run_until_idle()
    assert len(bash.runs) == 2


# ------------------------------------------------------------------ rules and modes


async def test_rule_precedence_deny_ask_allow(store: SqliteStore, tmp_path: Path) -> None:
    bash, read = Fake("Bash"), Fake("Read")
    provider = ScriptedProvider([reply(
        "", ("Read", {"file_path": ".env"}), ("Bash", {"command": "git push origin main"}),
        ("Bash", {"command": "make build"}))])
    perms = Permissions(mode="bypassPermissions", deny=["Read(./.env)"],
                        ask=["Bash(git push *)"], allow=["Bash(make *)"])
    client, tid, _ = await start(store, provider, perms, [bash, read], cwd=tmp_path)
    evs = await events(client, tid)
    [(text, err)] = [x for x in results(evs) if x[1]]
    assert err and "Read(./.env)" in text
    [detail] = asked(evs).values()
    assert "Bash(git push *)" in detail["prompt"]
    assert [r["command"] for r in bash.runs] == ["make build"]


@pytest.mark.parametrize(("mode", "command"), [
    ("acceptEdits", "\\rm -rf x"), ("acceptEdits", "'rm' -rf x"),
    ("bypassPermissions", "command rm -rf x"), ("bypassPermissions", "X=1 rm -rf x")])
async def test_a_deny_rule_is_not_dodged_by_quoting(store: SqliteStore, tmp_path: Path,
                                                    mode: str, command: str) -> None:
    bash = Fake("Bash")
    provider = ScriptedProvider([reply("", ("Bash", {"command": command})), reply("ok")])
    client, tid, _ = await start(store, provider, Permissions(mode=mode, deny=["Bash(rm *)"]),
                                 [bash], cwd=tmp_path)
    [(text, err)] = results(await events(client, tid))
    assert err and "Bash(rm *)" in text and not bash.runs


@pytest.mark.parametrize("command", ["\\git push origin main", "g'i't push origin main",
                                     "env git push origin main"])
async def test_an_ask_rule_is_not_dodged_by_quoting(store: SqliteStore, tmp_path: Path,
                                                    command: str) -> None:
    bash = Fake("Bash")
    provider = ScriptedProvider([reply("", ("Bash", {"command": command}))])
    perms = Permissions(mode="bypassPermissions", ask=["Bash(git push *)"])
    client, tid, _ = await start(store, provider, perms, [bash], cwd=tmp_path)
    [detail] = asked(await events(client, tid)).values()
    assert "Bash(git push *)" in detail["prompt"] and not bash.runs


async def test_dont_ask_refuses_what_would_ask(store: SqliteStore, tmp_path: Path) -> None:
    write, bash = Fake("Write"), Fake("Bash")
    provider = ScriptedProvider([reply(
        "", ("Write", {"file_path": "a"}), ("Bash", {"command": "ls"}),
        ("Bash", {"command": "make"})), reply("ok")])
    client, tid, _ = await start(store, provider, Permissions(mode="dontAsk", allow=["Write"]),
                                 [write, bash], cwd=tmp_path)
    evs = await events(client, tid)
    [(text, _)] = [x for x in results(evs) if x[1]]
    assert "don't-ask mode" in text
    assert len(write.runs) == 1 and [r["command"] for r in bash.runs] == ["ls"]


async def test_the_mode_signal_switches_modes(store: SqliteStore, tmp_path: Path) -> None:
    write = Fake("Write")
    provider = ScriptedProvider([reply("hi"), reply("", ("Write", {"file_path": "a"})),
                                 reply("ok")])
    client, tid, rt = await start(store, provider, Permissions(mode="default"), [write],
                                  cwd=tmp_path)
    await client.signal(tid, MODE_SIGNAL, {"mode": "acceptEdits"})
    await client.send(tid, "write it")
    await rt.run_until_idle()
    assert len(write.runs) == 1
    modes = [e.data["mode"] for e in await events(client, tid)
             if isinstance(e, ExtensionRecorded) and e.kind == "mode"]
    assert modes == ["default", "acceptEdits"]
    assert next_mode("auto") == "bypassPermissions"
    assert next_mode("auto", bypass=False) == "default"


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


async def test_webfetch_asks_per_host_and_remembers_it(store: SqliteStore) -> None:
    fetch = Fake("WebFetch")
    provider = ScriptedProvider([
        reply("", ("WebFetch", {"url": "https://docs.example.com/a"}),
              ("WebFetch", {"url": "https://example.com@evil.test/"})),
        reply("", ("WebFetch", {"url": "https://evil.test/again"})), reply("ok")])
    client, tid, rt = await start(store, provider, Permissions(
        mode="default", webfetch_allowed_hosts=["*.example.com"]), [fetch])
    [item] = pending(await client.events(tid), tid)
    assert item.detail["always"] == ["WebFetch(domain:evil.test)"]
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


async def test_the_usd_cap_counts_calls_outside_the_conversation(store: SqliteStore) -> None:
    """``max_cost_usd`` counts what ``/cost`` counts: a compaction's
    summarize calls (``spent`` records) too, not only the replies."""
    from noeta.core import records as core_records
    from noeta.core.extension import Extension
    from noeta.core.types import Usage

    class Summarize(Extension):
        name = "summarize"

        async def before_model(self, view: Any, request: Any) -> Any:
            if not view.state.steps_in_turn:
                spent = core_records.Spent(model="claude-opus-5-5",
                                           usage=Usage(uncached=1_000_000), purpose="compaction")
                view.record(core_records.CORE, spent.kind, spent.data())
            return request

    read = Fake("Read")
    perms = Permissions(budget=Budget(max_cost_usd=0.5))
    provider = ScriptedProvider([reply("", ("Read", {})), reply("ok")])
    ag = agent([Summarize(), perms], [read])
    rt = runtime(store, provider, ag)
    client = Client(store)
    tid = await client.create("a", "go")
    await rt.run_until_idle()
    assert results(await events(client, tid)) == [("max_cost_usd=0.5 reached", True)]


async def test_budget_caps_spawn_depth(store: SqliteStore) -> None:
    task = Fake("Task")
    provider = ScriptedProvider([reply("", ("Task", {})), reply("ok")])
    client, tid, _ = await start(store, provider, Permissions(mode="default"), [task],
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


async def test_can_use_tool_answers_asks(store: SqliteStore) -> None:
    write = Fake("Write")
    seen: list[str] = []

    async def decide(tool: str, args: dict[str, Any]) -> bool:
        seen.append(tool)
        return args.get("ok") is True

    provider = ScriptedProvider([reply("", ("Write", {"file_path": "a", "ok": True}),
                                       ("Write", {"file_path": "b", "ok": False})),
                                 reply("done")])
    client, tid, _ = await start(store, provider, Permissions(mode="default",
                                                              can_use_tool=decide), [write])
    evs = await events(client, tid)
    assert sorted(results(evs)) == [("denied by human", True), ("ran Write", False)]
    autos = [e.data for e in evs if isinstance(e, ExtensionRecorded) and e.kind == "auto"]
    assert [a["decision"] for a in autos] == ["allow", "deny"]
    assert seen == ["Write", "Write"]


def test_disallowing_bash_removes_its_companions() -> None:
    perms = Permissions(disallowed_tools=["Bash"])
    assert perms.disallowed == {"Bash", "Monitor", "TaskStop"}


async def test_a_disallowed_mcp_tool_by_its_old_name_is_still_refused() -> None:
    from noeta.core.extension import Deny, TaskView
    from noeta.core.loop.state import TaskState
    from noeta.core.types import ToolUse

    perms = Permissions(disallowed_tools=["mcp__github__delete_repo"])
    call = ToolUse(id="c", name="mcp__GitHub__delete_repo", input={})
    gate = await perms.before_tool(TaskView(state=TaskState()), call)
    assert isinstance(gate, Deny)
    assert not perms._offered("mcp__GitHub__delete_repo")
    assert perms._offered("mcp__GitHub__list")


# ------------------------------------------------------------------ sub-agents


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
    parent = agent([Permissions(mode="default", risk={"Spawn": "low"})], [Spawn()])
    child = agent([Permissions()], [write], name="child", one_shot=True)
    rt = runtime(store, provider, parent, child)
    client = Client(store)
    tid = await client.create("a", "go")
    await rt.run_until_idle()
    [item] = await pending_tree(client, tid)
    assert item.task_id != tid and item.tool == "Write" and item.agent == "child"
    await client.answer(item.task_id, item.request_id, "allow")
    await rt.run_until_idle()
    assert len(write.runs) == 1
    assert (await client.get(tid)).waiting_on == "input"


async def test_a_child_runs_in_its_parents_mode_with_its_rules(store: SqliteStore) -> None:
    write = Fake("Write")
    provider = ScriptedProvider([
        reply("", ("Write", {"file_path": "a"})),                    # parent asks
        reply("", ("Task", {"description": "do b", "prompt": "b",
                            "subagent_type": "child"})),            # parent delegates
        reply("", ("Write", {"file_path": "b"})),                    # child: no ask
        reply("wrote b"), reply("done")])
    parent = Agent(name="a", model="m", tools=[write], extensions=(
        Permissions(mode="default"), Subtasks([SubagentSpec(name="child", description="c")])))
    child = Agent(name="child", model="m", tools=[write], one_shot=True,
                  extensions=(Permissions(), Subtasks()))
    rt = runtime(store, provider, parent, child)
    client = Client(store)
    tid = await client.create("a", "go")
    await rt.run_until_idle()
    [item] = pending(await client.events(tid), tid)
    await client.answer(tid, item.request_id, "allow", data={"mode": "acceptEdits"})
    await rt.run_until_idle()
    assert await pending_tree(client, tid) == []
    assert [r["file_path"] for r in write.runs] == ["a", "b"]


async def test_a_glob_that_takes_in_a_link_out_of_the_workspace_asks(
        store: SqliteStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ws = _linked_ssh(tmp_path, monkeypatch)
    (ws / "k").symlink_to(tmp_path / "home" / ".ssh" / "id_rsa")
    (ws / "notes.txt").write_text("n")
    bash = Fake("Bash")
    provider = ScriptedProvider([reply(
        "", ("Bash", {"command": "cat *"}), ("Bash", {"command": "grep -r k *"}),
        ("Bash", {"command": "cat *.txt"}), ("Bash", {"command": "cat [kn]*"}))])
    client, tid, _ = await start(store, provider, Permissions(mode="default"), [bash], cwd=ws)
    assert len(asked(await events(client, tid))) == 3
    assert [r["command"] for r in bash.runs] == ["cat *.txt"]


async def test_a_quoted_bracket_names_a_file_and_is_checked_as_one(
        store: SqliteStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ws = _linked_ssh(tmp_path, monkeypatch)
    (ws / "n[1]").symlink_to(tmp_path / "home" / ".ssh" / "id_rsa")
    bash = Fake("Bash")
    provider = ScriptedProvider([reply(
        "", ("Bash", {"command": "cat 'n[1]'"}), ("Bash", {"command": "cat n\\[1\\]"}),
        ("Bash", {"command": 'cat "n[1]"'}), ("Bash", {"command": "cat README"}))])
    client, tid, _ = await start(store, provider, Permissions(mode="default"), [bash], cwd=ws)
    assert len(asked(await events(client, tid))) == 3
    assert [r["command"] for r in bash.runs] == ["cat README"]



def test_the_working_directories_are_the_cwd_then_the_additional_ones() -> None:
    from noeta.core.extension import TaskView
    from noeta.core.loop.state import TaskState
    from noeta.plugins.permissions import Permissions

    ext = Permissions(additional_directories=["../shared", "/srv/docs", "/srv/docs"])
    view = TaskView(state=TaskState(task_id="t", config={"cwd": "/w/proj"}))
    assert ext.working_directories(view) == ("/w/proj", "/w/shared", "/srv/docs")
