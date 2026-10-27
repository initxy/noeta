"""Workspace instructions: the root file once at the start, nested files
after a Read, each once."""

from __future__ import annotations

from pathlib import Path

from noeta.core.agent import Agent
from noeta.core.errors import ContextOverflow
from noeta.core.events import Compacted
from noeta.core.extension import Extension, TaskView
from noeta.core.types import ToolResult
from noeta.plugins.fs import FileTools
from noeta.plugins.instructions import Instructions
from noeta.plugins.memory import Memory
from noeta.plugins.skills import Skills
from noeta.testing import reply
from tests.plugins.context.conftest import Harness


def agent(**kw: object) -> Agent:
    return Agent(name="a", model="m", extensions=[FileTools(), Instructions(**kw)])  # type: ignore[arg-type]


async def test_root_file_is_recorded_once_before_the_first_message(h: Harness) -> None:
    (h.work / "AGENTS.md").write_text("Use tabs.\n")
    (h.work / "CLAUDE.md").write_text("ignored\n")
    a = agent()
    h.provider.add(reply("ok"), reply("again"))
    tid = await h.start(a)
    first = h.provider.requests[0].messages
    assert first[0].origin == "system"
    assert first[0].text == '<workspace-instructions source="AGENTS.md">\nUse tabs.\n\n' \
                            "</workspace-instructions>"
    assert first[1].text == "go"
    await h.say(a, tid, "more")
    assert len(await h.records(tid, "instructions")) == 1
    assert [m.text for m in h.last_messages()].count(first[0].text) == 1


async def test_noeta_md_wins_and_a_blank_file_is_skipped(h: Harness) -> None:
    (h.work / "NOETA.md").write_text("  \n")
    (h.work / "AGENTS.md").write_text("agents\n")
    h.provider.add(reply("ok"))
    tid = await h.start(agent())
    [rec] = await h.records(tid, "instructions")
    assert rec.data == {"name": "AGENTS.md"}


async def test_no_file_records_nothing(h: Harness) -> None:
    h.provider.add(reply("ok"))
    tid = await h.start(agent())
    assert await h.records(tid, "instructions") == []


async def test_a_read_brings_nested_files_after_its_result(h: Harness) -> None:
    pkg = h.work / "src" / "pkg"
    pkg.mkdir(parents=True)
    (h.work / "src" / "AGENTS.md").write_text("src rules\n")
    (pkg / "NOETA.md").write_text("pkg rules\n")
    (pkg / "AGENTS.md").write_text("not this one\n")
    (pkg / "a.py").write_text("x = 1\n")
    (pkg / "b.py").write_text("y = 2\n")
    h.provider.add(
        reply("", ("Read", {"file_path": "src/pkg/a.py"})),
        reply("", ("Read", {"file_path": str(pkg / "b.py")})),
        reply("done"),
    )
    tid = await h.start(agent())
    recs = await h.records(tid, "instructions")
    assert [r.data["name"] for r in recs] == ["src/AGENTS.md", "src/pkg/NOETA.md"]
    msgs = h.provider.requests[1].messages
    # user goal, assistant Read, result, then the two files in order
    assert isinstance(msgs[2].content[0], ToolResult)
    assert msgs[3].text.startswith('<workspace-instructions source="src/AGENTS.md">')
    assert msgs[4].text.startswith('<workspace-instructions source="src/pkg/NOETA.md">')
    assert "pkg rules" in msgs[4].text


async def test_reads_outside_the_workspace_or_failing_discover_nothing(h: Harness) -> None:
    outside = h.work.parent / "elsewhere"
    outside.mkdir()
    (outside / "AGENTS.md").write_text("outside\n")
    (outside / "f.txt").write_text("f\n")
    (h.work / "sub").mkdir()
    (h.work / "sub" / "AGENTS.md").write_text("sub\n")
    h.provider.add(
        reply("", ("Read", {"file_path": str(outside / "f.txt")})),
        reply("", ("Read", {"file_path": "sub/missing.txt"})),
        reply("done"),
    )
    tid = await h.start(agent())
    assert await h.records(tid, "instructions") == []


async def test_discovery_off_and_explicit_file(h: Harness) -> None:
    (h.work / "docs").mkdir()
    (h.work / "docs" / "RULES.md").write_text("custom\n")
    (h.work / "docs" / "AGENTS.md").write_text("nested\n")
    (h.work / "docs" / "x.txt").write_text("x\n")
    h.provider.add(reply("", ("Read", {"file_path": "docs/x.txt"})), reply("done"))
    tid = await h.start(agent(file="docs/RULES.md", discovery=False))
    [rec] = await h.records(tid, "instructions")
    assert rec.kind == "root" and rec.data == {"name": "RULES.md"}


class MakeRoom(Extension):
    name = "room"

    async def on_context_overflow(self, view: TaskView) -> bool:
        view.compacted(Compacted(summary="(summary)", through=view.state.last_seq,
                                 kept=await view.residents()))
        return True


async def test_residents_rehang_after_a_compaction(h: Harness, tmp_path: Path) -> None:
    (h.work / "AGENTS.md").write_text("Use tabs.\n")
    skill = h.work / ".noeta" / "skills" / "deploy"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: deploy\ndescription: Ship.\n---\nSteps.\n")
    mem = tmp_path / "mem"
    mem.mkdir()
    (mem / "note.md").write_text("A note.\n")
    a = Agent(name="a", model="m", extensions=[
        Skills(), Memory(root=mem), Instructions(), FileTools(), MakeRoom()])
    h.provider.add(reply("", ("skill", {"skill": "deploy"})), reply("done"),
                   ContextOverflow("too long"), reply("fits"))
    tid = await h.start(a)
    await h.say(a, tid, "next")
    texts = [m.text for m in h.last_messages()]
    assert texts[0] == "(summary)"
    assert texts[1].startswith("The following skills are available")
    assert texts[2].startswith("Activated skill: deploy")
    assert texts[3].startswith("Long-term memory index.")
    assert texts[4].startswith('<workspace-instructions source="AGENTS.md">')
    assert len(texts) == 5
