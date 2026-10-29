"""The tenant fence at the gate: a task of a tenant other than the default
one never reaches noeta's home, the store's files, ``/etc/noeta``, ``/proc``
or another tenant's workspace with a file tool — not by rule, mode or
approval — while the default tenant's tasks behave as before."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from noeta.backends.stores.sqlite import SqliteStore
from noeta.core import records
from noeta.core.client import Client
from noeta.core.events import ExtensionRecorded, TaskEnded, ToolFinished, Waiting
from noeta.core.tool import ToolContext
from noeta.plugins.permissions import MODE_SIGNAL, Permissions
from noeta.testing import ScriptedProvider, reply
from noeta.toolkit.paths import project_slug
from noeta.toolkit.tenant_fence import TenantFence, store_files
from noeta.toolkit.tools import spill_path
from tests.plugins.permissions.helpers import Fake, agent, events, runtime


@pytest.fixture
def places(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    home = tmp_path / "noeta-home"
    (home / "tenants" / "beta").mkdir(parents=True)
    (home / "tokens.json").write_text("{}")
    (home / "tenants" / "beta" / "config.toml").write_text('api_key = "secret"\n')
    (home / "skills").mkdir()
    (home / "skills" / "s.md").write_text("skill")
    monkeypatch.setenv("NOETA_HOME", str(home))
    acme, beta = tmp_path / "ws" / "acme", tmp_path / "ws" / "beta"
    acme.mkdir(parents=True)
    beta.mkdir(parents=True)
    (beta / "notes.txt").write_text("beta's")
    (acme / "README").write_text("acme's")
    return {"home": home, "acme": acme, "beta": beta, "db": tmp_path / "store" / "t.db"}


@pytest.fixture
async def store(places: dict[str, Path]) -> AsyncIterator[SqliteStore]:
    places["db"].parent.mkdir()
    s = SqliteStore(str(places["db"]))
    yield s
    await s.close()


def perms(places: dict[str, Path], **kw: Any) -> Permissions:
    return Permissions(tenant_workspaces={"acme": str(places["acme"]),
                                          "beta": str(places["beta"])},
                       store_files=store_files(f"sqlite:///{places['db']}"),
                       spill_root=str(places["home"] / "tool-results"),
                       plans_dir=str(places["home"] / "plans"), **kw)


async def run(store: SqliteStore, places: dict[str, Path], calls: list[tuple[str, dict[str, Any]]],
              *, tenant: str = "acme", **kw: Any) -> tuple[list[Any], dict[str, Fake]]:
    tools = {n: Fake(n) for n in ("Read", "Write", "Edit", "NotebookEdit", "Workflow")}
    provider = ScriptedProvider([reply("", *calls), reply("done")])
    rt = runtime(store, provider, agent([perms(places, **kw)], list(tools.values())))
    client = Client(store).scoped(tenant)
    tid = await client.create("a", "go", config={"cwd": str(places[tenant] if tenant in places
                                                            else places["acme"])})
    await rt.run_until_idle()
    return await events(client, tid), tools


def outcomes(evs: list[Any]) -> list[tuple[str, bool]]:
    return [(e.result.text, e.result.is_error) for e in evs if isinstance(e, ToolFinished)]


@pytest.mark.parametrize("mode", ["bypassPermissions", "acceptEdits", "auto", "default"])
async def test_every_fenced_place_is_denied_in_every_mode(
        store: SqliteStore, places: dict[str, Path], mode: str) -> None:
    home, db = places["home"], places["db"]
    os.symlink(home / "tokens.json", places["acme"] / "link")
    calls = [
        ("Read", {"file_path": str(home / "tokens.json")}),
        ("Read", {"file_path": str(home / "tenants" / "beta" / "config.toml")}),
        ("Edit", {"file_path": str(home / "settings.json")}),
        ("Read", {"file_path": str(db)}),
        ("Read", {"file_path": f"{db}-wal"}),
        ("Write", {"file_path": f"{db}-shm"}),
        ("Read", {"file_path": "/etc/noeta/config.toml"}),
        ("Read", {"file_path": "/proc/self/environ"}),
        ("Read", {"file_path": str(places["beta"] / "notes.txt")}),
        ("NotebookEdit", {"notebook_path": str(places["beta"] / "n.ipynb")}),
        ("Read", {"file_path": "link"}),                               # a symlink into home
        ("Read", {"file_path": "../../noeta-home/tokens.json"}),      # .. out of the workspace
        ("Workflow", {"scriptPath": str(home / "tokens.json")}),
        ("Read", {"file_path": "README"}),
    ]
    evs, tools = await run(store, places, calls, mode=mode,
                           allow=["Read(//**)", "Edit(//**)", "Workflow"])
    denied = [text for text, err in outcomes(evs) if err]
    assert len(denied) == len(calls) - 1, outcomes(evs)
    assert all("tenant 'acme' may not" in text for text in denied)
    assert not any(isinstance(e, Waiting) and e.on == "approval" for e in evs)
    assert [r["file_path"] for r in tools["Read"].runs] == ["README"]
    assert not tools["Write"].runs and not tools["Edit"].runs and not tools["Workflow"].runs


async def test_an_approval_does_not_lift_the_fence(store: SqliteStore,
                                                   places: dict[str, Path]) -> None:
    async def yes(_name: str, _input: dict[str, Any]) -> bool:
        return True

    evs, tools = await run(store, places, [
        ("Read", {"file_path": str(places["home"] / "tokens.json")}),
        ("Read", {"file_path": str(places["beta"] / "notes.txt")})],
        mode="default", can_use_tool=yes)
    assert [err for _, err in outcomes(evs)] == [True, True]
    assert not tools["Read"].runs
    assert not any(isinstance(e, Waiting) and e.on == "approval" for e in evs)


async def test_a_task_keeps_its_own_corners(store: SqliteStore, places: dict[str, Path]) -> None:
    home = places["home"]
    other_spill = spill_path(str(home / "tool-results"), "someone-else", "c1")
    calls = [("Read", {"file_path": str(home / "skills" / "s.md")}),
             ("Write", {"file_path": str(home / "skills" / "s.md")}),
             ("Write", {"file_path": str(home / "plans" / "acme" / "p.md")}),
             ("Write", {"file_path": str(home / "plans" / "beta" / "p.md")}),
             ("Read", {"file_path": other_spill})]
    evs, tools = await run(store, places, calls, mode="bypassPermissions")
    denied = sorted(text.split(" may not ")[1].split(":")[0] for text, err in outcomes(evs)
                    if err)
    assert denied == sorted([f"write {home / 'skills' / 's.md'}",
                             f"write {home / 'plans' / 'beta' / 'p.md'}", f"read {other_spill}"])
    assert [r["file_path"] for r in tools["Write"].runs] == [str(home / "plans" / "acme" / "p.md")]
    assert [r["file_path"] for r in tools["Read"].runs] == [str(home / "skills" / "s.md")]


async def test_a_tenants_plan_file_goes_in_its_own_directory(
        store: SqliteStore, places: dict[str, Path]) -> None:
    provider = ScriptedProvider([reply("hi"), reply("planning")])
    rt = runtime(store, provider, agent([perms(places)]))
    client = Client(store).scoped("acme")
    tid = await client.create("a", "go", config={"cwd": str(places["acme"])})
    await rt.run_until_idle()
    await client.signal(tid, MODE_SIGNAL, {"mode": "plan"})
    await client.send(tid, "plan it")
    await rt.run_until_idle()
    plans = [e.data["plan"]["path"] for e in await events(client, tid)
             if isinstance(e, ExtensionRecorded) and e.kind == "mode" and "plan" in e.data]
    assert len(plans) == 1 and Path(plans[0]).parent == places["home"] / "plans" / "acme"


@pytest.mark.parametrize("one_shot", [False, True])
async def test_a_worker_not_set_up_for_tenants_fails_their_turns(
        store: SqliteStore, places: dict[str, Path], one_shot: bool) -> None:
    read = Fake("Read")
    provider = ScriptedProvider([reply("", ("Read", {"file_path": "README"})), reply("done")])
    ag = agent([perms(places, tenant_refusal="no sandbox here")], [read], one_shot=one_shot)
    rt = runtime(store, provider, ag)
    theirs = await Client(store).scoped("acme").create("a", "go",
                                                       config={"cwd": str(places["acme"])})
    await rt.run_until_idle()
    assert not provider.requests and not read.runs
    evs = await events(Client(store), theirs)
    if one_shot:
        ended = [e for e in evs if isinstance(e, TaskEnded)]
        assert ended and ended[0].status == "failed"
        assert ended[0].error.startswith("tenant_not_isolated: ")
        why = ended[0].error
    else:
        failed = [d for e in evs if isinstance(d := records.decode(e), records.TurnFailed)]
        assert failed and failed[0].reason == "tenant_not_isolated"
        why = failed[0].detail
    assert "tenant 'acme'" in why and "no sandbox here" in why
    mine = await Client(store).create("a", "go", config={"cwd": str(places["acme"])})
    await rt.run_until_idle()
    assert read.runs and (await Client(store).get(mine)).status != "failed"


def test_own_spill_and_project_directories_stay_open(places: dict[str, Path]) -> None:
    home = places["home"]
    fence = TenantFence(spill_root=str(home / "tool-results"),
                        workspaces={"acme": str(places["acme"])})
    own = spill_path(str(home / "tool-results"), "t1", "c")
    root = spill_path(str(home / "tool-results"), "r1", "c")
    slug = project_slug(str(places["acme"]))
    project = home / "projects" / "acme" / slug
    kw: dict[str, Any] = {"tenant": "acme", "task_id": "t1", "root_id": "r1",
                          "cwd": str(places["acme"])}
    assert fence.refusal(own, **kw) is None
    assert fence.refusal(root, **kw) is None
    assert fence.refusal(str(project / "memory" / "MEMORY.md"), write=True, **kw) is None
    # The default tenant's and another tenant's project directory of that cwd.
    for theirs in (home / "projects" / slug, home / "projects" / "beta" / slug):
        assert fence.refusal(str(theirs / "memory" / "MEMORY.md"), **kw) is not None
    # Of its project directory, only its tree's transcripts, to read.
    assert fence.refusal(str(project / "t1.jsonl"), **kw) is None
    assert fence.refusal(str(project / "r1.jsonl"), **kw) is None
    assert fence.refusal(str(project / "t1.jsonl"), write=True, **kw) is not None
    for other in ("other-task.jsonl", "x"):
        assert fence.refusal(str(project / other), **kw) is not None
    assert fence.refusal(str(home / "projects" / slug / "default-task.jsonl"), **kw) is not None
    assert fence.refusal(str(home / "projects" / "-elsewhere" / "x"), **kw) is not None
    assert fence.refusal(str(home / "tokens.json"), **{**kw, "tenant": "default"}) is None


async def test_the_default_tenant_is_not_fenced(store: SqliteStore,
                                                places: dict[str, Path]) -> None:
    evs, tools = await run(store, places, [
        ("Read", {"file_path": str(places["home"] / "tokens.json")}),
        ("Read", {"file_path": str(places["beta"] / "notes.txt")})],
        tenant="default", mode="bypassPermissions")
    assert [err for _, err in outcomes(evs)] == [False, False]
    assert len(tools["Read"].runs) == 2


def test_the_operators_saved_workflows_are_readable_like_skills(tmp_path: Path) -> None:
    # The Workflow tool reads them by name in-process; the fence says the same.
    fence = TenantFence(home=str(tmp_path))
    saved = str(tmp_path / "workflows" / "review.js")
    assert fence.refusal(saved, tenant="acme", task_id="t") is None
    assert fence.refusal(saved, tenant="acme", task_id="t", write=True) is not None
    assert fence.refusal(str(tmp_path / "tokens.json"), tenant="acme", task_id="t")


def test_a_started_workflows_script_is_kept_where_its_task_may_read_it(
        tmp_path: Path) -> None:
    from noeta.plugins.workflow.tool import WorkflowTool

    spill = tmp_path / "tool-results"
    ctx = ToolContext(task_id="t1", call_id="c", env=None,  # type: ignore[arg-type]
                      state={}, record=lambda *_: None, spill_dir=str(spill))
    path = WorkflowTool._save(ctx, "wf_1", "export default 1")
    assert Path(path).read_text() == "export default 1"
    fence = TenantFence(home=str(tmp_path / "home"), spill_root=str(spill))
    assert fence.refusal(path, tenant="acme", task_id="t1") is None
    assert fence.refusal(path, tenant="acme", task_id="t2") is not None


@pytest.fixture
def operator(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """The operator's credentials in a home directory, the server's own
    directory (its working directory, with a ``.env``), an additional
    directory of the operator's settings and a shared file elsewhere."""
    home = tmp_path / "userhome"
    for path in (home / ".ssh" / "id_rsa", home / ".aws" / "credentials",
                 tmp_path / "server" / ".env", tmp_path / "extra" / "f",
                 tmp_path / "shared" / "f.txt"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("secret")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(tmp_path / "server")
    return {"home": home, "server": tmp_path / "server", "extra": tmp_path / "extra",
            "shared": tmp_path / "shared"}


def _reach(places: dict[str, Path],
           operator: dict[str, Path]) -> list[tuple[str, dict[str, Any]]]:
    from noeta.toolkit.paths import tenant_temp_dir

    return [
        ("Read", {"file_path": "~/.ssh/id_rsa"}),
        ("Read", {"file_path": str(operator["home"] / ".aws" / "credentials")}),
        ("Read", {"file_path": str(operator["server"] / ".env")}),
        ("Read", {"file_path": str(operator["extra"] / "f")}),
        ("Write", {"file_path": str(operator["extra"] / "g")}),
        ("Read", {"file_path": str(operator["shared"] / "f.txt")}),
        ("Edit", {"file_path": "/usr/bin/env"}),
        ("Write", {"file_path": "/etc/hosts"}),
        # What stays open: the machine's programs and settings to read, the
        # workspace, the tenant's own temporary directory.
        ("Read", {"file_path": "/usr/bin/env"}),
        ("Read", {"file_path": "/etc/hosts"}),
        ("Read", {"file_path": "README"}),
        ("Write", {"file_path": str(places["acme"] / "new.txt")}),
        ("Read", {"file_path": str(Path(tenant_temp_dir("acme")) / "tmp" / "x")}),
    ]


@pytest.mark.parametrize("how", ["bypass", "approved"])
async def test_a_tenant_reaches_only_its_workspace_own_places_and_system_files(
        store: SqliteStore, places: dict[str, Path], operator: dict[str, Path],
        how: str) -> None:
    async def yes(_name: str, _input: dict[str, Any]) -> bool:
        return True

    kw: dict[str, Any] = {"mode": "bypassPermissions", "allow": ["Read(//**)", "Edit(//**)"]} \
        if how == "bypass" else {"mode": "default", "can_use_tool": yes}
    calls = _reach(places, operator)
    evs, tools = await run(store, places, calls,
                           additional_directories=[str(operator["extra"])], **kw)
    results = outcomes(evs)
    assert sorted(err for _, err in results) == [False] * 5 + [True] * 8, results
    assert all("tenant 'acme' may not" in text for text, err in results if err)
    ran = [r["file_path"] for t in ("Read", "Write", "Edit") for r in tools[t].runs]
    assert sorted(ran) == sorted(str(c[1]["file_path"]) for c in calls[8:])


async def test_the_default_tenant_still_reaches_the_operators_files(
        store: SqliteStore, places: dict[str, Path], operator: dict[str, Path]) -> None:
    calls = _reach(places, operator)[:6]
    evs, _tools = await run(store, places, calls, tenant="default", mode="bypassPermissions",
                            additional_directories=[str(operator["extra"])])
    assert [err for _, err in outcomes(evs)] == [False] * 6, outcomes(evs)


async def test_the_operators_additional_directories_are_not_a_tenants(
        store: SqliteStore, places: dict[str, Path], operator: dict[str, Path]) -> None:
    # A read-only command in a working directory runs without asking; the
    # additional directory is one for the default tenant only.
    asked = {}
    for tenant in ("default", "acme"):
        bash = Fake("Bash")
        provider = ScriptedProvider([reply("", ("Bash", {"command": f"cat {operator['extra']}/f"})),
                                     reply("done")])
        rt = runtime(store, provider, agent(
            [perms(places, mode="default", additional_directories=[str(operator["extra"])])],
            [bash]))
        client = Client(store).scoped(tenant)
        tid = await client.create("a", "go", config={"cwd": str(places["acme"])})
        await rt.run_until_idle()
        asked[tenant] = any(isinstance(e, Waiting) and e.on == "approval"
                            for e in await events(client, tid))
    assert asked == {"default": False, "acme": True}


def test_the_server_directories_and_homes_are_not_system_files(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fence = TenantFence(home=str(tmp_path / "h"), server_dirs=("/usr/share/doc",))
    kw: dict[str, Any] = {"tenant": "acme", "task_id": "t"}
    assert fence.refusal("/usr/bin/env", **kw) is None
    assert fence.refusal("/usr/share/doc/README", **kw) is not None
    monkeypatch.setenv("HOME", "/usr/local/someone")
    assert fence.refusal("/usr/local/someone/.ssh/id_rsa", **kw) is not None
    assert fence.refusal("/usr/local/bin/tool", **kw) is None
    for elsewhere in ("/opt/tool/config", "/srv/data", "/var/log/syslog", "/tmp/f"):
        assert fence.refusal(elsewhere, **kw) is not None
    # A tenant whose workspace this worker does not know reaches none.
    assert fence.refusal(str(tmp_path / "anywhere"), **kw) is not None
