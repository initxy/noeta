"""Instruction files: Claude Code's layers over noeta's files at the start, imports, rules, the
memory index, and the files that apply to a file a tool touches."""

from __future__ import annotations

from pathlib import Path

from noeta.core.agent import Agent
from noeta.core.errors import ContextOverflow
from noeta.core.events import Compacted
from noeta.core.extension import Extension, TaskView
from noeta.core.types import ModelRequest, ToolResult
from noeta.plugins.fs import FileTools
from noeta.plugins.instructions import Instructions
from noeta.plugins.instructions.files import clean, matches
from noeta.plugins.memory import Memory, memory_dir_for
from noeta.plugins.permissions import Permissions
from noeta.plugins.shell import Shell
from noeta.plugins.skills import Skills
from noeta.testing import reply
from tests.plugins.context.conftest import Harness

PREAMBLE = ("Below are instructions from the codebase and the user. Follow them: they override "
            "your default behavior and must be followed exactly as written.")
PROJECT = "(project instructions, part of the repository)"


def agent(**kw: object) -> Agent:
    kw.setdefault("walk", False)
    return Agent(name="a", model="m", extensions=[FileTools(), Instructions(**kw)])  # type: ignore[arg-type]


async def test_noeta_files_are_recorded_once_before_the_first_message(h: Harness) -> None:
    (h.work / "NOETA.md").write_text("Use tabs.\n")
    (h.work / ".noeta").mkdir()
    (h.work / ".noeta" / "NOETA.md").write_text("Run tests.\n")
    (h.work / "AGENTS.md").write_text("not read: the directory has NOETA.md\n")
    (h.work / "NOETA.local.md").write_text("mine\n")
    a = agent()
    h.provider.add(reply("ok"), reply("again"))
    tid = await h.start(a)
    first = h.provider.requests[0].messages
    assert first[0].origin == "system"
    assert first[0].text == "\n\n".join([
        PREAMBLE,
        f"{h.work / 'NOETA.md'} {PROJECT}:\n\nUse tabs.",
        f"{h.work / '.noeta' / 'NOETA.md'} {PROJECT}:\n\nRun tests.",
        f"{h.work / 'NOETA.local.md'} (the user's private instructions for this project, "
        "not part of the repository):\n\nmine"])
    assert first[1].text == "go"
    await h.say(a, tid, "more")
    assert len(await h.records(tid, "instructions")) == 1
    assert [m.text for m in h.last_messages()].count(first[0].text) == 1


async def test_a_directory_without_noeta_md_reads_its_agents_md(h: Harness) -> None:
    (h.work / "AGENTS.md").write_text("agents\n")
    h.provider.add(reply("ok"))
    tid = await h.start(agent())
    [rec] = await h.records(tid, "instructions")
    assert [f["path"] for f in rec.data["files"]] == [str(h.work / "AGENTS.md")]


async def test_claude_codes_instruction_files_are_never_read(
        h: Harness, tmp_path: Path) -> None:
    user_claude = tmp_path / "user" / ".claude"
    (user_claude / "rules").mkdir(parents=True)
    (user_claude / "CLAUDE.md").write_text("claude user\n")
    (h.work / "CLAUDE.md").write_text("claude project\n")
    (h.work / "CLAUDE.local.md").write_text("claude local\n")
    (h.work / ".claude" / "rules").mkdir(parents=True)
    (h.work / ".claude" / "CLAUDE.md").write_text("claude dot\n")
    (h.work / ".claude" / "rules" / "r.md").write_text("claude rule\n")
    (h.work / "AGENTS.md").write_text("agents\n")
    h.provider.add(reply("ok"))
    tid = await h.start(agent(user_dir=str(tmp_path / "user" / ".noeta")))
    [rec] = await h.records(tid, "instructions")
    assert [f["path"] for f in rec.data["files"]] == [str(h.work / "AGENTS.md")]
    assert "claude " not in h.provider.requests[0].messages[0].text


async def test_no_file_records_nothing(h: Harness) -> None:
    h.provider.add(reply("ok"))
    tid = await h.start(agent())
    assert await h.records(tid, "instructions") == []


async def test_layers_in_claude_codes_order(h: Harness, tmp_path: Path) -> None:
    managed, user_dir = tmp_path / "managed", tmp_path / "noeta-home"
    (managed / ".noeta" / "rules").mkdir(parents=True)
    (managed / "NOETA.md").write_text("policy\n")
    (managed / ".noeta" / "rules" / "m.md").write_text("managed rule\n")
    (user_dir / "rules").mkdir(parents=True)
    (user_dir / "NOETA.md").write_text("user\n")
    (user_dir / "rules" / "u.md").write_text("user rule\n")
    (user_dir / "rules" / "py.md").write_text("---\npaths: ['**/*.py']\n---\nuser python\n")
    sub = h.work / "app"
    (sub / ".noeta" / "rules").mkdir(parents=True)
    (h.work / "NOETA.md").write_text("outer\n")
    (sub / "NOETA.md").write_text("inner\n")
    (sub / ".noeta" / "rules" / "a.md").write_text("always\n")
    (sub / ".noeta" / "rules" / "ts.md").write_text("---\npaths:\n  - 'src/**/*.ts'\n---\nts\n")
    (sub / "NOETA.local.md").write_text("local\n")
    a = agent(walk=True, user_dir=str(user_dir), managed_dir=str(managed))
    h.provider.add(reply("ok"))
    tid = await h.start(a, config={"cwd": str(sub)})
    [rec] = await h.records(tid, "instructions")
    got = [(Path(f["path"]).relative_to(tmp_path).as_posix(), f["kind"])
           for f in rec.data["files"]]
    assert got == [
        ("managed/NOETA.md", "managed"), ("managed/.noeta/rules/m.md", "managed"),
        ("noeta-home/NOETA.md", "user"), ("noeta-home/rules/u.md", "user"),
        ("work/NOETA.md", "project"), ("work/app/NOETA.md", "project"),
        ("work/app/.noeta/rules/a.md", "project"), ("work/app/NOETA.local.md", "local")]
    text = h.provider.requests[0].messages[0].text
    assert "(the user's own instructions, for every project)" in text
    assert "ts" not in text.split("always")[1].split("local")[0]


async def test_managed_only_reads_just_the_machines_files(h: Harness, tmp_path: Path) -> None:
    # An agent file's omitClaudeMd: Claude Code keeps only the managed
    # policy files.
    managed, user_dir = tmp_path / "managed", tmp_path / "noeta-home"
    managed.mkdir()
    (managed / "NOETA.md").write_text("policy\n")
    user_dir.mkdir()
    (user_dir / "NOETA.md").write_text("user\n")
    (h.work / "NOETA.md").write_text("project\n")
    a = agent(user_dir=str(user_dir), managed_dir=str(managed), managed_only=True)
    h.provider.add(reply("ok"))
    tid = await h.start(a)
    [rec] = await h.records(tid, "instructions")
    assert [f["kind"] for f in rec.data["files"]] == ["managed"]
    text = h.provider.requests[0].messages[0].text
    assert "policy" in text and "project\n" not in text and "user\n" not in text


async def test_imports_follow_the_file_naming_them(h: Harness, tmp_path: Path) -> None:
    (h.work / "docs").mkdir()
    (h.work / "NOETA.md").write_text(
        "See @AGENTS.md and @docs/style.md.\n```\n@docs/ignored.md\n```\n`@docs/code.md`\n"
        "<!-- @docs/comment.md -->\nmail me@example.com\n")
    (h.work / "AGENTS.md").write_text("agents @./docs/style.md\n")
    for i in range(1, 7):
        (h.work / "docs" / f"deep{i}.md").write_text(f"deep{i} @deep{i + 1}.md\n")
    for name in ("ignored.md", "code.md", "comment.md"):
        (h.work / "docs" / name).write_text("never\n")
    outside = tmp_path / "outside.md"
    outside.write_text("outside\n")
    (h.work / "docs" / "style.md").write_text(f"style @deep1.md @{outside}\n")
    h.provider.add(reply("ok"))
    tid = await h.start(agent())
    [rec] = await h.records(tid, "instructions")
    names = [Path(f["path"]).name for f in rec.data["files"]]
    # Depth 0 is NOETA.md (style.md is depth 2, through AGENTS.md); an
    # import at depth 5 is not read, nor one outside the repository.
    assert names == ["NOETA.md", "AGENTS.md", "style.md", "deep1.md", "deep2.md"]
    text = h.provider.requests[0].messages[0].text
    assert "<!--" not in text and "never" not in text and "outside\n" not in text


def test_only_comment_blocks_are_dropped() -> None:
    """Claude Code's markdown lexer drops a comment that stands as a block;
    one inside a paragraph, inline code, a code block or a list item stays."""
    text = ("<!-- maintainers only -->\nUse tabs.\n<!-- a\nlong one -->\n\n"
            "Keep <!-- inline --> this and `<!-- code -->`.\n"
            "```\n<!-- fenced -->\n```\n- item\n  <!-- in the item -->\n"
            "<!-- note --> after it\n<!-- never closed")
    assert clean(text)[0] == (
        "Use tabs.\nKeep <!-- inline --> this and `<!-- code -->`.\n"
        "```\n<!-- fenced -->\n```\n- item\n  <!-- in the item -->\n"
        " after it\n<!-- never closed")


async def test_an_import_through_a_link_out_of_the_repository_is_not_read(
        h: Harness, tmp_path: Path) -> None:
    secret = tmp_path / "secret.md"
    secret.write_text("SECRET\n")
    (h.work / "docs").mkdir()
    (h.work / "docs" / "link.md").symlink_to(secret)
    (h.work / "docs" / "inside.md").write_text("inside\n")
    (h.work / "docs" / "near.md").symlink_to(h.work / "docs" / "inside.md")
    (h.work / "NOETA.md").write_text("See @docs/link.md and @docs/near.md\n")
    h.provider.add(reply("ok"))
    tid = await h.start(agent())
    [rec] = await h.records(tid, "instructions")
    assert [Path(f["path"]).name for f in rec.data["files"]] == ["NOETA.md", "near.md"]
    assert "SECRET" not in h.provider.requests[0].messages[0].text


async def test_the_memory_index_ends_the_instructions_message(h: Harness, tmp_path: Path) -> None:
    (h.work / "NOETA.md").write_text("Use tabs.\n")
    home = tmp_path / "home"
    mem = Path(memory_dir_for(str(h.work), home))
    mem.mkdir(parents=True)
    (mem / "MEMORY.md").write_text("- [Note](note.md) — a note\n")
    a = Agent(name="a", model="m", extensions=[
        FileTools(), Memory(home=str(home)), Instructions(walk=False)])
    h.provider.add(reply("ok"))
    await h.start(a)
    messages = h.provider.requests[0].messages
    assert [m.text for m in messages][1:] == ["go"]
    assert messages[0].text.endswith(
        f"{mem / 'MEMORY.md'} (your auto memory index for this project, kept across "
        "conversations):\n\n- [Note](note.md) — a note")


async def test_a_sub_agent_without_the_memory_plugin_is_shown_its_callers_index(
        h: Harness, tmp_path: Path) -> None:
    from noeta.plugins.subtasks import SubagentSpec, Subtasks

    home = tmp_path / "home"
    mem = Path(memory_dir_for(str(h.work), home))
    mem.mkdir(parents=True)
    (mem / "MEMORY.md").write_text("- [Note](note.md) — a note\n")
    roster = Subtasks([SubagentSpec(name="helper", description="Helps.", agent="w")])
    main = Agent(name="a", model="m", extensions=[
        FileTools(), Memory(home=str(home)), Instructions(walk=False), roster])
    # As Claude Code's general-purpose agent (Explore and Plan, without
    # instruction files, are shown none).
    helper = Agent(name="w", model="m", extensions=[FileTools(), Instructions(walk=False)])
    h.provider.add(reply("", ("Agent", {"description": "d", "prompt": "help",
                                        "subagent_type": "helper"})),
                   reply("started"), reply("helped"), reply("thanks"))
    await h.client.create("a", "go")
    await h.runtime(main, helper).run_until_idle()
    child = next(r for r in h.provider.requests if r.messages[-1].text == "help")
    index = f"{mem / 'MEMORY.md'} (your auto memory index for this project, kept across " \
        "conversations):\n\n- [Note](note.md) — a note"
    assert child.messages[0].text.endswith(index)
    # No memory section: the sub-agent's prompt does not name the directory.
    assert str(mem) not in "".join(p.text for p in child.system)


async def test_a_touched_file_brings_what_applies_to_it(h: Harness) -> None:
    pkg = h.work / "src" / "pkg"
    pkg.mkdir(parents=True)
    rules = h.work / ".noeta" / "rules"
    rules.mkdir(parents=True)
    (rules / "py.md").write_text("---\npaths: src/**/*.py\n---\npython rule\n")
    (rules / "md.md").write_text("---\npaths: ['*.md']\n---\nmarkdown rule\n")
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
    recs = await h.records(tid, "instructions", "nested")
    assert [Path(r.data["files"][0]["path"]).relative_to(h.work).as_posix() for r in recs] == [
        ".noeta/rules/py.md", "src/AGENTS.md", "src/pkg/NOETA.md"]
    msgs = h.provider.requests[1].messages
    assert isinstance(msgs[2].content[0], ToolResult)
    assert msgs[3].text == f"{rules / 'py.md'} {PROJECT}:\n\npython rule"
    assert msgs[5].text == f"{pkg / 'NOETA.md'} {PROJECT}:\n\npkg rules"
    assert len(h.provider.requests[2].messages) == len(msgs) + 2


async def test_a_file_a_shell_command_showed_brings_what_applies_to_it(h: Harness) -> None:
    # As Claude Code: ``cat`` of a file is a read of it for the nested files.
    pkg = h.work / "src" / "pkg"
    pkg.mkdir(parents=True)
    rules = h.work / ".noeta" / "rules"
    rules.mkdir(parents=True)
    (rules / "py.md").write_text("---\npaths: src/**/*.py\n---\npython rule\n")
    (pkg / "NOETA.md").write_text("pkg rules\n")
    (pkg / "a.py").write_text("x = 1\n")
    (h.work / "other").mkdir()
    (h.work / "other" / "NOETA.md").write_text("other rules\n")
    (h.work / "other" / "b.py").write_text("y = 2\n")
    h.provider.add(
        reply("", ("Bash", {"command": "cat src/pkg/a.py"})),
        # Not a shell read (a pipe), and a grep that found nothing: no read.
        reply("", ("Bash", {"command": "cat other/b.py | wc -l"})),
        reply("", ("Bash", {"command": "grep zzz other/b.py"})),
        reply("done"),
    )
    shell_agent = Agent(name="a", model="m",
                        extensions=[FileTools(), Shell(), Instructions(walk=False)])
    tid = await h.start(shell_agent)
    recs = await h.records(tid, "instructions", "nested")
    assert [Path(r.data["files"][0]["path"]).relative_to(h.work).as_posix() for r in recs] == [
        ".noeta/rules/py.md", "src/pkg/NOETA.md"]
    assert {r.data["trigger"] for r in recs} == {str(pkg / "a.py")}
    msgs = h.provider.requests[1].messages
    assert isinstance(msgs[2].content[0], ToolResult)
    assert msgs[3].text == f"{rules / 'py.md'} {PROJECT}:\n\npython rule"


async def test_a_shell_read_of_a_file_a_read_deny_rule_covers_brings_nothing(
        h: Harness) -> None:
    # As Claude Code: the file still counts as read, but its directory's
    # instruction files and ``paths:`` rules are not brought in.
    secrets = h.work / "secrets"
    secrets.mkdir()
    (secrets / "NOETA.md").write_text("secret rules\n")
    (secrets / "x.txt").write_text("token\n")
    open_dir = h.work / "open"
    open_dir.mkdir()
    (open_dir / "NOETA.md").write_text("open rules\n")
    (open_dir / "y.txt").write_text("fine\n")
    h.provider.add(
        reply("", ("Bash", {"command": "cat secrets/x.txt"})),
        reply("", ("Bash", {"command": "cat open/y.txt"})),
        reply("done"),
    )
    guarded = Agent(name="a", model="m", extensions=[
        Permissions(mode="default", trusted_workspace=True, deny=["Read(./secrets/**)"]),
        FileTools(), Shell(), Instructions(walk=False)])
    tid = await h.start(guarded)
    reads = [r.data for r in await h.records(tid, "fs", "read")]
    assert [(Path(d["path"]).relative_to(h.work).as_posix(), d.get("read_denied")) for d in reads
            ] == [("secrets/x.txt", True), ("open/y.txt", None)]
    recs = await h.records(tid, "instructions", "nested")
    assert [Path(r.data["files"][0]["path"]).relative_to(h.work).as_posix() for r in recs] == [
        "open/NOETA.md"]


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
    assert rec.kind == "start"
    assert [f["path"] for f in rec.data["files"]] == [str(h.work / "docs" / "RULES.md")]


def test_globs_match_as_gitignore_does() -> None:
    assert matches(["*.md"], "docs/a.md") and matches(["*.md"], "a.md")
    assert matches(["src/**/*.ts"], "src/a/b/c.ts") and matches(["src/**/*.ts"], "src/c.ts")
    assert not matches(["src/**/*.ts"], "lib/src/c.ts")
    assert matches(["src/*.{ts,tsx}"], "src/a.tsx") and not matches(["src/*.ts"], "src/a/b.ts")
    assert matches(["docs"], "docs/deep/x.txt") and matches(["/src/"], "src/x")
    assert not matches(["*.md"], "../a.md") and not matches(["# comment"], "x")


class MakeRoom(Extension):
    name = "room"

    async def on_context_overflow(self, view: TaskView, request: ModelRequest) -> bool:
        view.compacted(Compacted(summary="(summary)", through=view.state.last_seq,
                                 lead=await view.residents(lead=True, keeps_tail=False),
                                 after=await view.residents(keeps_tail=False)))
        return True


async def test_residents_rehang_after_a_compaction(h: Harness, tmp_path: Path) -> None:
    (h.work / "AGENTS.md").write_text("Use tabs.\n")
    skill = h.work / ".noeta" / "skills" / "deploy"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: deploy\ndescription: Ship.\n---\nSteps.\n")
    home = tmp_path / "home"
    mem = Path(memory_dir_for(str(h.work), home))
    mem.mkdir(parents=True)
    (mem / "MEMORY.md").write_text("- [Note](note.md) — a note\n")
    a = Agent(name="a", model="m", extensions=[
        Skills(), Memory(home=str(home)), Instructions(walk=False), FileTools(), MakeRoom()])
    h.provider.add(reply("", ("Skill", {"skill": "deploy"})), reply("done"),
                   ContextOverflow("too long"), reply("fits"))
    tid = await h.start(a)
    await h.say(a, tid, "next")
    texts = [m.text for m in h.last_messages()]
    # Ahead of the summary: the instructions with the memory index; after
    # it: the invoked skill, then the listing, given again in a system turn.
    assert texts[0].startswith(PREAMBLE)
    assert texts[0].endswith("- [Note](note.md) — a note")
    assert texts[1] == "(summary)"
    assert texts[2].startswith("These skills were invoked earlier")
    # Each skill says where it comes from (Claude Code's ``<source>:<name>``).
    assert "### Skill: deploy\nPath: workspace:deploy\n\n" in texts[2]
    assert "\n\nBase directory for this skill: " in texts[2] and "Activated" not in texts[2]
    assert texts[3].startswith("The following skills are available")
    assert len(texts) == 4


async def test_a_plugin_skill_invoked_under_its_former_bare_name_is_given_again(
        h: Harness) -> None:
    """A log written when a plugin's skills had their own names (``ship``)
    re-attaches the skill now named ``<plugin>:ship``."""
    plugin = h.work.parent / "tools-plugin" / "skills"
    (plugin / "ship").mkdir(parents=True)
    (plugin / "ship" / "SKILL.md").write_text("---\nname: ship\ndescription: Ship.\n---\nGo.\n")
    before = Agent(name="a", model="m", extensions=[
        Skills(dirs=[str(plugin)]), FileTools(), MakeRoom()])
    h.provider.add(reply("", ("Skill", {"skill": "ship"})), reply("done"))
    tid = await h.start(before)
    [act] = await h.records(tid, "skills", "activated")
    assert act.data["name"] == "ship"
    now = Agent(name="a", model="m", extensions=[
        Skills(plugin_dirs={str(plugin): "tools"}), FileTools(), MakeRoom()])
    h.provider.add(ContextOverflow("too long"), reply("fits"))
    await h.say(now, tid, "next")
    [again] = [m.text for m in h.last_messages() if m.text.startswith("These skills were")]
    assert "### Skill: tools:ship\nPath: plugin:tools:ship\n\n" in again


async def test_skills_given_again_are_capped_newest_first(h: Harness) -> None:
    """Claude Code's caps: each skill body cut to 5,000 tokens, newest
    invoked first, 25,000 in all; the oldest that does not fit is left out."""
    for i in range(6):
        skill = h.work / ".noeta" / "skills" / f"s{i}"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(
            f"---\nname: s{i}\ndescription: Skill {i}.\n---\nBODY{i} " + "word " * 6_000)
    a = Agent(name="a", model="m", extensions=[Skills(), FileTools(), MakeRoom()])
    h.provider.add(*(reply("", ("Skill", {"skill": f"s{i}"})) for i in range(6)),
                   reply("done"), ContextOverflow("too long"), reply("fits"))
    tid = await h.start(a)
    await h.say(a, tid, "next")
    [again] = [m.text for m in h.last_messages() if m.text.startswith("These skills were")]
    names = [line.removeprefix("### Skill: ") for line in again.splitlines()
             if line.startswith("### Skill: ")]
    assert names == ["s5", "s4", "s3", "s2", "s1"]
    assert again.count("[... the rest of this skill was cut at the summary") == 5
    assert "BODY0" not in again


async def test_an_import_of_a_non_text_file_is_skipped(h: Harness) -> None:
    (h.work / "NOETA.md").write_text("Logo @logo.png data @data.bin notes @notes.txt "
                                     "make @Makefile R @stats.R\n")
    (h.work / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\nfake")
    (h.work / "data.bin").write_text("binary-ish\n")
    (h.work / "notes.txt").write_text("notes\n")
    (h.work / "Makefile").write_text("all:\n")
    (h.work / "stats.R").write_text("x <- 1\n")
    h.provider.add(reply("ok"))
    tid = await h.start(agent())
    [rec] = await h.records(tid, "instructions")
    # Claude Code's text extensions only; a name without one is read.
    assert [Path(f["path"]).name for f in rec.data["files"]] == [
        "NOETA.md", "notes.txt", "Makefile", "stats.R"]


def test_the_nested_limit_follows_the_window() -> None:
    from noeta.plugins.instructions.extension import cut_nested, nested_max_chars
    from noeta.toolkit.tokens import chars_per_token
    assert chars_per_token("claude-sonnet-4-5-20250929") == 4
    assert chars_per_token("claude-opus-5-5") == 3 and chars_per_token("deepseek-v4") == 3
    assert chars_per_token("") == 4
    # 1M window, 3 characters a token: 5 % is 150,000 characters.
    assert nested_max_chars("claude-opus-5-5") == 150_000
    assert nested_max_chars("") == 40_000
    assert cut_nested("/p", "short", 10) == "short"
    cut = cut_nested("/p/NOETA.md", "aaaa\nbbbb\ncccc\n", 12)
    assert cut == ("aaaa\nbbbb\n> Only the start of this file is shown here; Read /p/NOETA.md "
                   "from offset=3 for the rest.")


async def test_a_long_nested_file_shows_its_start(h: Harness) -> None:
    pkg = h.work / "gen"
    pkg.mkdir()
    (pkg / "NOETA.md").write_text("".join(f"line {i:06d}\n" for i in range(10_000)))
    (pkg / "a.py").write_text("x = 1\n")
    h.provider.add(reply("", ("Read", {"file_path": "gen/a.py"})), reply("done"))
    await h.start(agent())
    [nested] = [m.text for m in h.provider.requests[1].messages
                if m.text.startswith(str(pkg / "NOETA.md"))]
    assert len(nested) < 40_000 + 600
    assert nested.endswith(f"Read {pkg / 'NOETA.md'} from offset=3334 for the rest.")


async def test_a_changed_instruction_file_is_shown_again_with_the_next_message(
        h: Harness) -> None:
    from noeta.plugins.instructions.extension import REREAD_HEAD
    (h.work / "NOETA.md").write_text("Use tabs.\n")
    (h.work / "NOETA.local.md").write_text("Mine.\n")
    a = agent()
    h.provider.add(reply("ok"), reply("ok"), reply("ok"), reply("ok"))
    tid = await h.start(a)
    await h.say(a, tid, "same files")
    assert not [m for m in h.last_messages() if m.text.startswith(REREAD_HEAD)]
    (h.work / "NOETA.md").write_text("Use spaces.\n")
    # Each message is taken by a new runtime (another worker, say): what
    # was shown lives in the log.
    await h.say(a, tid, "files changed")
    reread = [m.text for m in h.last_messages() if m.text.startswith(REREAD_HEAD)]
    assert reread == [f"{REREAD_HEAD}\n\n{h.work / 'NOETA.md'} {PROJECT}:\n\nUse spaces."]
    await h.say(a, tid, "unchanged since")
    assert len([m for m in h.last_messages() if m.text.startswith(REREAD_HEAD)]) == 1


async def test_the_memory_index_is_read_again_with_the_next_message(
        h: Harness, tmp_path: Path) -> None:
    """Claude Code's re-read covers the auto-memory index too, and a file
    that was not there before: a memory saved this turn is in the index the
    next message brings."""
    from noeta.plugins.instructions.extension import REREAD_HEAD
    (h.work / "NOETA.md").write_text("Use tabs.\n")
    home = tmp_path / "home"
    mem = Path(memory_dir_for(str(h.work), home))
    a = Agent(name="a", model="m", extensions=[
        FileTools(), Memory(home=str(home)), Instructions(walk=False)])
    h.provider.add(reply("ok"), reply("ok"), reply("ok"), reply("ok"))
    tid = await h.start(a)
    mem.mkdir(parents=True, exist_ok=True)
    (mem / "MEMORY.md").write_text("- [Tabs](tabs.md) — the user wants tabs\n")
    await h.say(a, tid, "next")

    def reread() -> list[str]:
        return [m.text for m in h.last_messages() if m.text.startswith(REREAD_HEAD)]

    [note] = reread()
    assert note.endswith(f"{mem / 'MEMORY.md'} (your auto memory index for this project, "
                         "kept across conversations):\n\n- [Tabs](tabs.md) — the user wants tabs")
    await h.say(a, tid, "unchanged")
    assert len(reread()) == 1
    (mem / "MEMORY.md").write_text("- [Spaces](spaces.md) — the user wants spaces\n")
    await h.say(a, tid, "changed")
    assert len(reread()) == 2 and reread()[-1].endswith("the user wants spaces")
