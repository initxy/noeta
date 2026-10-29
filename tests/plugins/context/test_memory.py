"""Auto memory: the per-project directory, the prompt section that names it,
``MEMORY.md`` loaded within its limits, and recall through a selector."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from noeta.backends.execenvs.local import LocalExecEnv
from noeta.core.agent import Agent
from noeta.core.types import ModelRequest, ModelResponse
from noeta.plugins.hooks import Hooks
from noeta.plugins.instructions import Instructions
from noeta.plugins.memory import INDEX_FILE, Memory, load_index, memory_dir_for
from noeta.plugins.memory.extension import (
    INDEX_MAX_BYTES,
    INDEX_MAX_LINES,
    RECALL_HEAD,
    RECALL_MAX_FILES,
)
from noeta.testing import reply
from noeta.toolkit.paths import project_slug
from noeta.toolkit.tenant_fence import TenantFence
from tests.plugins.context.conftest import Harness


def agent(home: Path, **kw: object) -> Agent:
    return Agent(name="a", model="m", extensions=[Memory(home=str(home), **kw)])  # type: ignore[arg-type]


def test_the_directory_is_claude_codes_layout(tmp_path: Path) -> None:
    assert memory_dir_for("/Users/me/my.proj", tmp_path) == \
        f"{tmp_path}/projects/-Users-me-my-proj/memory/"
    long = "/x" * 150
    assert project_slug(long).startswith("-x-x") and len(project_slug(long)) > 200


async def test_the_prompt_names_the_directory_and_it_exists(h: Harness, tmp_path: Path) -> None:
    home = tmp_path / "home"
    h.provider.add(reply("ok"))
    await h.start(agent(home))
    directory = memory_dir_for(str(h.work), home)
    assert Path(directory).is_dir()
    system = "".join(p.text for p in h.provider.requests[0].system)
    assert f"`{directory}`" in system
    for fact in ("Write", "MEMORY.md", "user | feedback | project | reference", "[[",
                 "**Why:**", "<system-reminder>"):
        assert fact in system, fact
    # No MEMORY.md yet: nothing attached.
    assert [m.text for m in h.provider.requests[0].messages] == ["go"]


async def test_the_section_goes_before_the_environment_section(
        h: Harness, tmp_path: Path) -> None:
    a = Agent(name="a", model="m", extensions=[Memory(home=str(tmp_path / "home"))],
              system_prompt="# Guidance\n - one\n\n# Environment\n - here")
    h.provider.add(reply("ok"))
    await h.start(a)
    prompt = h.provider.requests[0].system[-1].text
    assert prompt.startswith("# Guidance\n - one\n\n# Memory\n")
    assert prompt.endswith("\n\n# Environment\n - here")


async def test_a_repository_shares_one_memory_across_worktrees(
        h: Harness, tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(h.work)], check=True)
    (h.work / "sub").mkdir()
    home = tmp_path / "home"
    h.provider.add(reply("ok"))
    await h.start(agent(home), config={"cwd": str(h.work / "sub")})
    assert Path(memory_dir_for(str(h.work), home)).is_dir()


async def test_a_tenant_cannot_borrow_another_repositorys_memory(
        h: Harness, tmp_path: Path) -> None:
    other = tmp_path / "beta"
    subprocess.run(["git", "init", "-q", str(other)], check=True)
    home = tmp_path / "home"
    theirs = Path(memory_dir_for(str(other), home))
    theirs.mkdir(parents=True)
    (theirs / INDEX_FILE).write_text("- [Secret](s.md) — beta's\n")
    # A .git file naming the other repository makes it this directory's main checkout.
    (h.work / ".git").write_text(f"gitdir: {other / '.git'}\n")
    h.client = h.client.scoped("acme")
    h.provider.add(reply("ok"))
    await h.start(agent(home))
    assert all("beta's" not in m.text for m in h.provider.requests[0].messages)
    assert Path(memory_dir_for(str(h.work), home, "acme")).is_dir()


async def test_a_tenants_memory_is_its_own(h: Harness, tmp_path: Path) -> None:
    home = tmp_path / "home"
    # The default tenant's memory of the same directory.
    theirs = Path(memory_dir_for(str(h.work), home))
    theirs.mkdir(parents=True)
    (theirs / INDEX_FILE).write_text("- [Secret](s.md) — the operator's\n")
    h.client = h.client.scoped("acme")
    h.provider.add(reply("ok"))
    await h.start(agent(home))
    assert all("operator's" not in m.text for m in h.provider.requests[0].messages)
    mine = Path(memory_dir_for(str(h.work), home, "acme"))
    assert mine.is_dir() and mine.parent.parent.name == "acme"
    assert mine != theirs and theirs.parent.parent.name == "projects"


async def test_a_tenants_user_scope_agent_memory_stays_in_its_workspace(
        h: Harness, tmp_path: Path) -> None:
    home = tmp_path / "home"
    h.client = h.client.scoped("acme")
    h.provider.add(reply("ok"))
    await h.start(agent(home, agent_scope="user", agent_name="rev"))
    assert Path(f"{h.work}/.noeta/agent-memory/rev/").is_dir()
    assert not (home / "agent-memory").exists()


async def test_memory_md_is_loaded(h: Harness, tmp_path: Path) -> None:
    home = tmp_path / "home"
    directory = Path(memory_dir_for(str(h.work), home))
    directory.mkdir(parents=True)
    (directory / INDEX_FILE).write_text("- [Tabs](tabs.md) — the user wants tabs\n")
    h.provider.add(reply("ok"))
    await h.start(agent(home))
    first = h.provider.requests[0].messages[0]
    assert first.origin == "system"
    assert first.text.startswith(f"Contents of {directory / INDEX_FILE} (")
    assert first.text.endswith("- [Tabs](tabs.md) — the user wants tabs")


def test_the_index_is_cut_at_its_limits() -> None:
    assert load_index("a\nb\n") == "a\nb"
    long = "\n".join(f"- line {i}" for i in range(INDEX_MAX_LINES + 50))
    cut = load_index(long)
    assert cut.count("\n- line") == INDEX_MAX_LINES - 1
    assert f"{INDEX_MAX_LINES + 50} lines (limit {INDEX_MAX_LINES})" in cut
    wide = "\n".join("x" * 1000 for _ in range(40))
    cut = load_index(wide)
    assert len(cut.split("\n> Note")[0].encode()) <= INDEX_MAX_BYTES
    assert f"(limit {INDEX_MAX_BYTES})" in cut


def _memory(directory: Path, name: str, desc: str, body: str, kind: str = "feedback") -> None:
    (directory / name).write_text(
        f"---\nname: {name[:-3]}\ndescription: {desc}\nmetadata:\n  type: {kind}\n---\n\n{body}\n")


async def test_recall_attaches_what_the_selector_picks(h: Harness, tmp_path: Path) -> None:
    home = tmp_path / "home"
    directory = Path(memory_dir_for(str(h.work), home))
    directory.mkdir(parents=True)
    _memory(directory, "tabs.md", "indent with tabs", "Use tabs.")
    _memory(directory, "deploy.md", "how deploys run", "Deploy on Fridays.", "project")
    a = agent(home, recall_model="small")
    h.provider.add(reply(json.dumps({"selected_memories": ["tabs.md", "nope.md"]})),
                   reply("ok"), reply("again"))
    tid = await h.start(a, goal="format this file")
    selector, main = h.provider.requests[:2]
    assert selector.model == "small" and selector.output_schema is not None
    listing = selector.messages[0].text
    assert "- [feedback] tabs.md (" in listing and "): indent with tabs" in listing
    assert "format this file" in listing
    recalled = [m for m in main.messages if m.origin == "memory"]
    assert len(recalled) == 1
    assert recalled[0].text == f"{RECALL_HEAD}\n\nMemory {directory / 'tabs.md'}:\n" \
        + (directory / "tabs.md").read_text().strip()
    # An attached memory is not offered again.
    h.provider.add(reply(json.dumps({"selected_memories": []})), reply("more"))
    await h.say(a, tid, "and this one")
    assert "tabs.md" not in h.provider.requests[-2].messages[0].text
    assert RECALL_MAX_FILES == 5


async def test_without_a_recall_model_nothing_is_selected(h: Harness, tmp_path: Path) -> None:
    home = tmp_path / "home"
    directory = Path(memory_dir_for(str(h.work), home))
    directory.mkdir(parents=True)
    _memory(directory, "tabs.md", "indent with tabs", "Use tabs.")
    h.provider.add(reply("ok"))
    await h.start(agent(home))
    assert len(h.provider.requests) == 1


async def test_an_agent_files_memory_scope_is_its_own_directory(
        h: Harness, tmp_path: Path) -> None:
    home = tmp_path / "home"
    h.provider.add(reply("ok"))
    await h.start(agent(home, agent_scope="project", agent_name="rev"))
    directory = f"{h.work}/.noeta/agent-memory/rev/"
    assert Path(directory).is_dir()
    assert f"`{directory}`" in "".join(p.text for p in h.provider.requests[0].system)
    with pytest.raises(ValueError, match="agent_scope"):
        Memory(agent_scope="team", agent_name="rev")


async def test_an_agents_project_memory_is_the_main_checkouts_from_a_worktree(
        h: Harness, tmp_path: Path) -> None:
    for cmd in (["init", "-q"], ["-c", "user.email=a@b", "-c", "user.name=a", "commit", "-q",
                                 "--allow-empty", "-m", "init"]):
        subprocess.run(["git", *cmd], cwd=h.work, check=True)
    tree = h.work / ".noeta" / "worktrees" / "agent-1"
    subprocess.run(["git", "worktree", "add", "-q", "-b", "wt", str(tree)], cwd=h.work,
                   check=True)
    home = tmp_path / "home"
    h.provider.add(reply("ok"))
    await h.start(agent(home, agent_scope="project", agent_name="rev"),
                  config={"cwd": str(tree)})
    assert Path(f"{h.work}/.noeta/agent-memory/rev/").is_dir()
    assert not (tree / ".noeta").exists()


def test_agent_memory_scopes_name_noetas_places(tmp_path: Path) -> None:
    from types import SimpleNamespace

    view: Any = SimpleNamespace(cwd="/w", env=None, state=SimpleNamespace(config={}))
    dirs = {scope: Memory(home=str(tmp_path), agent_scope=scope,
                          agent_name="rev")._agent_dir(view, "/w")
            for scope in ("user", "project", "local")}
    assert dirs == {"user": f"{tmp_path}/agent-memory/rev/",
                    "project": "/w/.noeta/agent-memory/rev/",
                    "local": "/w/.noeta/agent-memory-local/rev/"}


def _fenced(h: Harness, home: Path, server: Path) -> None:
    """The harness's tasks of a non-default tenant get the env a worker gives
    them: one that checks each file it opens against the tenant fence."""
    fence = TenantFence(home=str(home), workspaces={"acme": str(h.work)},
                        server_dirs=(str(server),))

    def env_for(state: Any) -> LocalExecEnv:
        def check(path: str, write: bool) -> str | None:
            return fence.refusal(path, tenant=state.tenant, task_id=state.task_id,
                                 cwd=str(h.work), write=write)
        return LocalExecEnv(str(h.work), fence=check)

    h.env_for = env_for
    h.client = h.client.scoped("acme")


@pytest.mark.parametrize("instructions", [False, True])
async def test_a_tenants_agent_memory_cannot_link_to_a_protected_file(
        h: Harness, tmp_path: Path, instructions: bool) -> None:
    home, server = tmp_path / "home", tmp_path / "server"
    server.mkdir()
    (server / "secret.md").write_text("---\ndescription: top secret\n---\nTOP-SECRET\n")
    directory = h.work / ".noeta" / "agent-memory" / "rev"
    directory.mkdir(parents=True)
    (directory / INDEX_FILE).symlink_to(server / "secret.md")
    (directory / "s.md").symlink_to(server / "secret.md")
    _fenced(h, home, server)
    exts: list[Any] = [Memory(home=str(home), agent_scope="project", agent_name="rev",
                              recall_model="small")]
    if instructions:
        exts.append(Instructions(walk=False))
    h.provider.add(reply(json.dumps({"selected_memories": ["s.md"]})), reply("ok"))
    await h.start(Agent(name="a", model="m", extensions=exts))
    sent = json.dumps([[m.text for m in r.messages] for r in h.provider.requests])
    assert "TOP-SECRET" not in sent and "top secret" not in sent


async def test_a_tenants_memory_directory_is_not_made_through_a_link_out(
        h: Harness, tmp_path: Path) -> None:
    home, server = tmp_path / "home", tmp_path / "server"
    server.mkdir()
    (h.work / ".noeta").mkdir()
    (h.work / ".noeta" / "agent-memory").symlink_to(server)
    _fenced(h, home, server)
    h.provider.add(reply("ok"))
    await h.start(Agent(name="a", model="m", extensions=[
        Memory(home=str(home), agent_scope="project", agent_name="rev")]))
    assert list(server.iterdir()) == []


async def test_a_tenants_own_memory_is_still_read(h: Harness, tmp_path: Path) -> None:
    home, server = tmp_path / "home", tmp_path / "server"
    server.mkdir()
    directory = h.work / ".noeta" / "agent-memory" / "rev"
    directory.mkdir(parents=True)
    (directory / INDEX_FILE).write_text("- [Tabs](tabs.md) — tabs please\n")
    _memory(directory, "tabs.md", "indent with tabs", "Use tabs.")
    _fenced(h, home, server)
    h.provider.add(reply(json.dumps({"selected_memories": ["tabs.md"]})), reply("ok"))
    await h.start(Agent(name="a", model="m", extensions=[
        Memory(home=str(home), agent_scope="project", agent_name="rev", recall_model="small")]))
    selector, main = h.provider.requests[:2]
    assert "indent with tabs" in selector.messages[0].text
    sent = "\n".join(m.text for m in main.messages)
    assert "tabs please" in sent and "Use tabs." in sent


async def test_a_blocked_prompt_is_never_recalled_for(h: Harness, tmp_path: Path) -> None:
    home = tmp_path / "home"
    directory = Path(memory_dir_for(str(h.work), home))
    directory.mkdir(parents=True)
    _memory(directory, "vault.md", "the vault", "The vault code is 1234.")

    def answer(request: ModelRequest) -> ModelResponse:
        if request.model == "small":
            picked = ["vault.md"] if "vault please" in request.messages[0].text else []
            return reply(json.dumps({"selected_memories": picked}))
        return reply("ok")

    h.provider.add(*[answer] * 4)
    hooks = {"UserPromptSubmit": [{"matcher": "", "hooks": [{
        "type": "command", "command": "grep -q SECRET && { echo no >&2; exit 2; }; exit 0"}]}]}
    a = Agent(name="a", model="m", extensions=[
        Hooks(hooks=hooks), Memory(home=str(home), recall_model="small")])
    tid = await h.start(a, goal="SECRET vault please", config={"cwd": str(h.work)})
    await h.say(a, tid, "hello")
    assert not any("SECRET" in m.text for r in h.provider.requests for m in r.messages)
    main = h.provider.requests[-1]
    assert main.model == "m" and not [m for m in main.messages if m.origin == "memory"]
