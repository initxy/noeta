"""Auto memory: the per-project directory, the prompt section that names it,
``MEMORY.md`` loaded within its limits, and recall through a selector."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from noeta.core.agent import Agent
from noeta.plugins.memory import INDEX_FILE, Memory, load_index, memory_dir_for
from noeta.plugins.memory.extension import (
    INDEX_MAX_BYTES,
    INDEX_MAX_LINES,
    RECALL_HEAD,
    RECALL_MAX_FILES,
)
from noeta.testing import reply
from noeta.toolkit.context import project_slug
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
