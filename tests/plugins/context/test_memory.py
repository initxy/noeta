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
from noeta.plugins.fs import FileTools
from noeta.plugins.hooks import Hooks
from noeta.plugins.memory import INDEX_FILE, Memory, load_index, memory_dir_for
from noeta.plugins.memory.extension import (
    INDEX_MAX_CHARS,
    INDEX_MAX_LINES,
    RECALL_HEAD,
    RECALL_MAX_FILES,
)
from noeta.testing import reply
from noeta.toolkit.paths import project_slug
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


@pytest.mark.parametrize("mode", ["default", "auto", "plan"])
async def test_the_model_saves_a_memory_without_asking(
        h: Harness, tmp_path: Path, mode: str) -> None:
    """Claude Code's auto-memory carve-out: the memory directory lies
    outside the working directory, yet its markdown files are written
    without a prompt, in every mode (and the file tools' fence lets them)."""
    from noeta.plugins.permissions import Permissions

    home = tmp_path / "home"
    directory = memory_dir_for(str(h.work), home)
    note, other = f"{directory}role.md", f"{directory}notes.txt"
    a = Agent(name="a", model="m", extensions=[
        Memory(home=str(home)), FileTools(), Permissions(mode=mode)])
    h.provider.add(reply("", ("Write", {"file_path": note, "content": "user is a chemist\n"})),
                   reply("", ("Write", {"file_path": other, "content": "x\n"})),
                   reply("saved"))
    tid = await h.start(a, config={"unattended": True} if mode == "default" else None)
    assert Path(note).read_text() == "user is a chemist\n"
    first = (await h.results(tid))[0].result
    assert not first.is_error, first.text
    # Only markdown files are carved out.
    assert not Path(other).exists()


async def test_a_link_in_the_memory_directory_carries_no_write_or_read_past_the_rules(
        h: Harness, tmp_path: Path) -> None:
    """The carve-out is judged where a write lands: a repository's planted
    link ``.noeta/agent-memory/a -> ..`` would turn a memory write into a
    write of ``.noeta/agents/evil.md`` (an agent file, which can carry hooks)
    (a read through a link is the next test)."""
    from noeta.plugins.permissions import Permissions

    memory = h.work / ".noeta" / "agent-memory"
    memory.mkdir(parents=True)
    (memory / "a").symlink_to("..")
    (h.work / ".noeta" / "agents").mkdir()
    evil = memory / "a" / "agents" / "evil.md"
    a = Agent(name="a", model="m", extensions=[
        Memory(agent_scope="project", agent_name="a"), FileTools(), Permissions(mode="default")])
    h.provider.add(reply("", ("Write", {"file_path": str(evil), "content": "x"})),
                   reply("done"))
    tid = await h.start(a, config={"cwd": str(h.work)})
    assert not (h.work / ".noeta" / "agents" / "evil.md").exists()
    # Asked, as any write into .noeta/agents is.
    assert (await h.client.get(tid)).status == "waiting"
    assert await h.results(tid) == []


async def test_a_memory_file_read_through_a_link_out_of_it_asks(
        h: Harness, tmp_path: Path) -> None:
    from noeta.plugins.permissions import Permissions

    home = tmp_path / "home"
    directory = Path(memory_dir_for(str(h.work), home))
    directory.mkdir(parents=True)
    outside = tmp_path / "secret.md"
    outside.write_text("the key")
    (directory / "note.md").symlink_to(outside)
    a = Agent(name="a", model="m", extensions=[
        Memory(home=str(home)), FileTools(), Permissions(mode="default")])
    h.provider.add(reply("", ("Read", {"file_path": str(directory / "note.md")})),
                   reply("done"))
    tid = await h.start(a)
    assert await h.results(tid) == []
    assert (await h.client.get(tid)).status == "waiting"


class Elsewhere(LocalExecEnv):
    """An exec env on another machine (a container every worker reaches):
    its files are under ``jail`` here, out of this machine's paths."""

    local = False

    def __init__(self, cwd: str, jail: Path, mapped: Path) -> None:
        super().__init__(cwd)
        self.jail, self.mapped = str(jail), str(mapped)

    def _p(self, path: str) -> str:
        return self.jail + path if path.startswith(self.mapped) else path

    async def read_bytes(self, path: str) -> bytes:
        return await super().read_bytes(self._p(path))

    async def write_bytes(self, path: str, data: bytes) -> None:
        await super().write_bytes(self._p(path), data)

    async def stat(self, path: str) -> Any:
        return await super().stat(self._p(path))

    async def realpath(self, path: str) -> str:
        return path

    async def run(self, argv: Any, **kw: Any) -> Any:
        return await super().run([self._p(a) for a in argv], **kw)


async def test_with_a_remote_exec_env_memory_lives_in_it(h: Harness, tmp_path: Path) -> None:
    """The model saves memories with its file tools, through the task's exec
    env; the plugin reads them there too (not on the worker), so the index
    and recall see them on any worker that reaches the env."""
    home = tmp_path / "home"
    jail = tmp_path / "jail"
    h.env_for = lambda _s: Elsewhere(str(h.work), jail, home)
    directory = memory_dir_for(str(h.work), home)
    note = f"{directory}role.md"
    a = Agent(name="a", model="m", extensions=[
        Memory(home=str(home), recall_model="small"), FileTools()])
    h.provider.add(
        reply("", ("Write", {"file_path": note,
                             "content": "---\ndescription: their job\n---\nchemist\n"})),
        reply("saved"))
    tid = await h.start(a)
    assert not Path(note).exists()
    assert (jail / note.lstrip("/")).read_text().endswith("chemist\n")
    h.provider.add(reply(json.dumps({"selected_memories": ["role.md"]})),
                   reply("you are a chemist"))
    await h.say(a, tid, "what do I do for a living?")
    selector, main = h.provider.requests[-2:]
    assert "role.md (" in selector.messages[0].text and "): their job" in selector.messages[0].text
    [recalled] = [m for m in main.messages if m.origin == "memory"]
    assert recalled.text.endswith("chemist")


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
    assert len(cut.split("\n> Note")[0]) <= INDEX_MAX_CHARS
    assert f"(limit {INDEX_MAX_CHARS})" in cut


def test_the_index_limit_counts_characters_not_bytes() -> None:
    """Claude Code measures the index by its length in characters: a CJK
    index of 20,000 characters (60,000 UTF-8 bytes) is loaded whole."""
    cjk = "\n".join("记" * 99 for _ in range(200))
    assert len(cjk) < INDEX_MAX_CHARS < len(cjk.encode())
    assert load_index(cjk) == cjk
    wide = "\n".join("记" * 999 for _ in range(30))
    cut = load_index(wide)
    kept = cut.split("\n> Note")[0]
    assert INDEX_MAX_CHARS - 1000 <= len(kept) <= INDEX_MAX_CHARS
    assert f"{len(wide)} characters (limit {INDEX_MAX_CHARS})" in cut


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


async def test_the_recall_budget_counts_characters(h: Harness, tmp_path: Path) -> None:
    """Claude Code's 61,440 budget of a conversation counts the recalled
    text's length in characters, not its UTF-8 bytes."""
    home = tmp_path / "home"
    directory = Path(memory_dir_for(str(h.work), home))
    directory.mkdir(parents=True)
    _memory(directory, "zh.md", "中文笔记", "用制表符缩进。")
    a = agent(home, recall_model="small")
    h.provider.add(reply(json.dumps({"selected_memories": ["zh.md"]})), reply("ok"))
    tid = await h.start(a, goal="怎么缩进 this file")
    (record,) = await h.records(tid, "memory", "recalled")
    shown = (directory / "zh.md").read_text().strip()
    assert record.data["chars"] == len(shown) < len(shown.encode())


def test_a_selector_reply_that_is_not_json_still_picks_listed_files() -> None:
    from noeta.plugins.memory.extension import selected_names

    names = {"tabs.md", "deploy.md"}
    assert selected_names('{"selected_memories": ["tabs.md"]}', names) == ["tabs.md"]
    assert selected_names("deploy.md\ntabs.md, nope.md", names) == ["deploy.md", "tabs.md"]
    assert selected_names("None of them.", names) == []


async def test_a_provider_refusing_disabled_thinking_is_asked_again_without_it(
        h: Harness, tmp_path: Path) -> None:
    from noeta.core.errors import Fatal

    home = tmp_path / "home"
    directory = Path(memory_dir_for(str(h.work), home))
    directory.mkdir(parents=True)
    _memory(directory, "tabs.md", "indent with tabs", "Use tabs.")
    _memory(directory, "deploy.md", "how deploys run", "Deploy on Fridays.", "project")
    a = agent(home, recall_model="refuses-disabled-thinking")
    h.provider.add(Fatal("thinking: disabled is not supported", status=400),
                   reply("tabs.md"), reply("ok"), reply("deploy.md"), reply("again"))
    tid = await h.start(a, goal="format this file")
    refused, retried, main = h.provider.requests[:3]
    assert refused.thinking is False and retried.thinking is None
    assert any(m.origin == "memory" and "Use tabs." in m.text for m in main.messages)
    await h.say(a, tid, "and how do deploys run")
    selector = h.provider.requests[3]
    assert selector.model == "refuses-disabled-thinking"
    assert selector.thinking is None  # remembered for the model


async def test_a_failing_selector_warns_once(h: Harness, tmp_path: Path,
                                             caplog: pytest.LogCaptureFixture) -> None:
    from noeta.core.errors import Fatal

    home = tmp_path / "home"
    directory = Path(memory_dir_for(str(h.work), home))
    directory.mkdir(parents=True)
    _memory(directory, "tabs.md", "indent with tabs", "Use tabs.")
    a = agent(home, recall_model="small")
    h.provider.add(Fatal("quota gone", status=403), reply("ok"),
                   Fatal("quota gone", status=403), reply("again"))
    tid = await h.start(a, goal="format this file")
    await h.say(a, tid, "and this other file")
    warned = [r for r in caplog.records if "recall selector failed" in r.message]
    assert len(warned) == 1 and "quota gone" in warned[0].message


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


async def test_a_blocked_prompts_recall_is_dropped_whatever_the_plugin_order(
        h: Harness, tmp_path: Path) -> None:
    # Memory listed before Hooks recalls for the prompt before the hook
    # blocks it: that recall never reaches the model, and its files may be
    # recalled again.
    home = tmp_path / "home"
    directory = Path(memory_dir_for(str(h.work), home))
    directory.mkdir(parents=True)
    _memory(directory, "vault.md", "the vault", "The vault code is 1234.")

    def answer(request: ModelRequest) -> ModelResponse:
        if request.model == "small":
            picked = ["vault.md"] if "vault please" in request.messages[0].text else []
            return reply(json.dumps({"selected_memories": picked}))
        return reply("ok")

    h.provider.add(*[answer] * 6)
    hooks = {"UserPromptSubmit": [{"matcher": "", "hooks": [{
        "type": "command", "command": "grep -q SECRET && { echo no >&2; exit 2; }; exit 0"}]}]}
    a = Agent(name="a", model="m", extensions=[
        Memory(home=str(home), recall_model="small"), Hooks(hooks=hooks)])
    tid = await h.start(a, goal="SECRET vault please", config={"cwd": str(h.work)})
    await h.say(a, tid, "hello")
    main = h.provider.requests[-1]
    assert main.model == "m"
    assert [m.text for m in main.messages if m.origin != "system"] == ["hello"]
    assert not any("1234" in m.text or "SECRET" in m.text for m in main.messages)
    await h.say(a, tid, "vault please")
    main = h.provider.requests[-1]
    assert [m for m in main.messages if m.origin == "memory" and "1234" in m.text]


def test_the_selector_is_sparing_with_broad_memories() -> None:
    from noeta.plugins.memory.extension import RECALL_PROMPT
    # Claude Code's selector rule: match the subject, not shared words, and
    # be most careful with the user-profile and project-overview memories.
    assert "[user]" in RECALL_PROMPT and "[project]" in RECALL_PROMPT
    assert "not by words it happens to share" in RECALL_PROMPT


def test_a_recalled_file_shows_its_start_and_says_where_the_rest_is() -> None:
    from noeta.plugins.memory.extension import (
        RECALL_FILE_MAX_BYTES,
        RECALL_FILE_MAX_LINES,
        recalled_text,
    )
    assert recalled_text("/m/a.md", "short") == "short"
    many = "\n".join(f"line {i}" for i in range(300))
    cut = recalled_text("/m/a.md", many)
    assert cut.split("\n")[RECALL_FILE_MAX_LINES - 1] == f"line {RECALL_FILE_MAX_LINES - 1}"
    assert cut.endswith(f"> Only the first {RECALL_FILE_MAX_LINES} lines of this memory are "
                        "shown; Read /m/a.md for the whole file.")
    wide = "\n".join("x" * 100 for _ in range(100))
    cut = recalled_text("/m/a.md", wide)
    body, note = cut.rsplit("\n", 1)
    assert len(body.encode()) <= RECALL_FILE_MAX_BYTES and body.endswith("x")
    assert f"first {RECALL_FILE_MAX_BYTES} bytes" in note


async def test_recall_attaches_a_long_memory_cut_to_its_start(h: Harness, tmp_path: Path) -> None:
    home = tmp_path / "home"
    directory = Path(memory_dir_for(str(h.work), home))
    directory.mkdir(parents=True)
    _memory(directory, "big.md", "everything", "\n".join("y" * 80 for _ in range(400)))
    a = agent(home, recall_model="small")
    h.provider.add(reply(json.dumps({"selected_memories": ["big.md"]})), reply("ok"))
    await h.start(a, goal="tell me everything")
    [recalled] = [m for m in h.provider.requests[1].messages if m.origin == "memory"]
    assert len(recalled.text.encode()) < 4096 + 800
    assert f"Read {directory / 'big.md'} for the whole file." in recalled.text


async def test_an_old_recalled_memory_says_how_old_it_is(h: Harness, tmp_path: Path) -> None:
    import os
    import time
    home = tmp_path / "home"
    directory = Path(memory_dir_for(str(h.work), home))
    directory.mkdir(parents=True)
    _memory(directory, "old.md", "old fact", "The parser lives in a.py:10.")
    _memory(directory, "new.md", "new fact", "Fresh.")
    _memory(directory, "day.md", "a day old", "Yesterday.")
    week = time.time() - 7 * 86_400 - 60
    os.utime(directory / "old.md", (week, week))
    one = time.time() - 1.5 * 86_400
    os.utime(directory / "day.md", (one, one))
    a = agent(home, recall_model="small")
    h.provider.add(reply(json.dumps({"selected_memories": ["old.md", "new.md", "day.md"]})),
                   reply("ok"))
    await h.start(a, goal="where is the parser")
    [recalled] = [m for m in h.provider.requests[1].messages if m.origin == "memory"]
    text = recalled.text
    assert "This memory was last changed 7 days ago. A memory records what was true when " \
        "it was written" in text
    assert "Check against the current code before stating it as fact.\n" \
        f"Memory {directory / 'old.md'}:" in text
    # A day old or less carries no note (Claude Code's threshold).
    assert text.count("last changed") == 1


async def test_a_one_word_message_asks_no_selector(h: Harness, tmp_path: Path) -> None:
    home = tmp_path / "home"
    directory = Path(memory_dir_for(str(h.work), home))
    directory.mkdir(parents=True)
    _memory(directory, "tabs.md", "indent with tabs", "Use tabs.")
    a = agent(home, recall_model="small")
    h.provider.add(reply("ok"))
    tid = await h.start(a, goal="  continue  ")
    assert [r.model for r in h.provider.requests] == ["m"]
    # CJK prose has no spaces; it is not one word.
    h.provider.add(reply(json.dumps({"selected_memories": ["tabs.md"]})), reply("ok"))
    await h.say(a, tid, "用制表符缩进这个文件")
    assert [r.model for r in h.provider.requests] == ["m", "small", "m"]


async def test_a_memory_the_task_read_is_not_recalled(h: Harness, tmp_path: Path) -> None:
    home = tmp_path / "home"
    directory = Path(memory_dir_for(str(h.work), home))
    directory.mkdir(parents=True)
    _memory(directory, "tabs.md", "indent with tabs", "Use tabs.")
    _memory(directory, "deploy.md", "how deploys run", "Deploy on Fridays.")
    a = Agent(name="a", model="m", extensions=[
        FileTools(), Memory(home=str(home), recall_model="small")])
    h.provider.add(reply(json.dumps({"selected_memories": []})),
                   reply("", ("Read", {"file_path": str(directory / "tabs.md")})), reply("ok"))
    tid = await h.start(a, goal="look at my tabs memory")
    h.provider.add(reply(json.dumps({"selected_memories": []})), reply("ok"))
    await h.say(a, tid, "and now the deploy one")
    listing = h.provider.requests[-2].messages[0].text
    assert "deploy.md" in listing and "tabs.md" not in listing


async def test_a_sub_agent_recalls_nothing(tmp_path: Path) -> None:
    from noeta.core.extension import TaskView
    from noeta.core.loop.state import TaskState
    from noeta.core.types import user

    async def complete(_r: ModelRequest) -> ModelResponse:
        raise AssertionError("a sub-agent asked the selector")

    state = TaskState(task_id="r.c1.1", agent="a", parent_id="r", root_id="r",
                      ext={"memory": {"dir": str(tmp_path), "surfaced": [], "bytes": 0}})
    state.messages.append(user("format this file"))
    state.message_seqs.append(1)
    view = TaskView(state=state, complete=complete)
    assert await Memory(recall_model="small").reminders(view) == ()


def test_a_recall_logged_with_its_former_bytes_key_still_counts() -> None:
    """Recalls logged before the count's key was ``chars`` (``bytes``) fold
    into the same count, and an erased one takes its share back off."""
    ext = Memory()
    state = ext.reduce(None, "recalled", {"paths": ["a.md"], "bytes": 10, "through": 1})
    state = ext.reduce(state, "recalled", {"paths": ["b.md"], "chars": 5, "through": 2})
    assert state["chars"] == 15
    assert ext.reduce({"dir": None, "surfaced": [], "bytes": 7}, "recalled",
                      {"paths": ["c.md"], "chars": 1, "through": 3})["chars"] == 8
    erased = ext.reduce(state, "core.erased", {"seqs": [1]})
    assert erased["chars"] == 5 and erased["surfaced"] == ["b.md"]
