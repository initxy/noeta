"""A file the model read and someone else then changed: the model is told
once, in a system turn before its next model call, with the changed lines
when its last whole view of the file is still in the conversation (then it
may edit at once); its own edits and untouched files say nothing."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from noeta.backends.execenvs.local import LocalExecEnv
from noeta.backends.stores.sqlite import SqliteStore
from noeta.core.agent import Agent
from noeta.core.client import Client
from noeta.core.events import ToolFinished
from noeta.core.runner import Runtime
from noeta.core.tool import ToolContext, ToolOutput, text_output
from noeta.core.types import ModelRequest
from noeta.plugins.fs import FileTools
from noeta.testing import ScriptedProvider, reply


async def _no_sleep(_: float) -> None:
    return None


def _notes(request: ModelRequest) -> list[str]:
    return [m.text for m in request.messages
            if m.origin == "system" and m.text.startswith("Note: ")]


def _touch(path: Path, text: str) -> None:
    path.write_text(text)
    st = path.stat()
    os.utime(path, (st.st_atime, st.st_mtime + 5))  # a clearly newer mtime


async def test_a_cut_reads_paging_note_follows_its_result_once(tmp_path: Path) -> None:
    work = tmp_path / "w"
    work.mkdir()
    big = work / "big.txt"
    big.write_text("".join("y" * 150 + "\n" for _ in range(1700)))
    store = SqliteStore(str(tmp_path / "t.db"))
    provider = ScriptedProvider([
        reply("", ("Read", {"file_path": str(big)})),
        reply("", ("Read", {"file_path": str(big), "offset": 2, "limit": 1})),
        reply("done"),
    ])
    rt = Runtime(store=store, agents={"a": Agent(name="a", model="m", extensions=[FileTools()])},
                 provider_for=lambda _m: provider, env_for=lambda _s: LocalExecEnv(str(work)),
                 sleep=_no_sleep)
    client = Client(store)
    await client.create("a", "read it")
    await rt.run_until_idle()

    def paging(request: ModelRequest) -> list[str]:
        return [m.text for m in request.messages
                if m.origin == "system" and m.text.startswith("[Partial view")]

    assert paging(provider.requests[0]) == []
    assert len(paging(provider.requests[1])) == 1
    assert len(paging(provider.requests[2])) == 1  # the same note, not a second one
    await store.close()


async def test_told_once_per_change(tmp_path: Path) -> None:
    work = tmp_path / "w"
    work.mkdir()
    a, b = work / "a.txt", work / "b.txt"
    a.write_text("one\n")
    b.write_text("bee\n")
    store = SqliteStore(str(tmp_path / "t.db"))
    provider = ScriptedProvider([
        reply("", ("Read", {"file_path": str(a)}), ("Read", {"file_path": str(b)})),
        reply("read"),
        reply("", ("Edit", {"file_path": str(b), "old_string": "bee", "new_string": "be"})),
        reply("edited"),
        reply("ok"),
        reply("ok"),
        reply("ok"),
    ])
    rt = Runtime(store=store, agents={"a": Agent(name="a", model="m", extensions=[FileTools()])},
                 provider_for=lambda _m: provider, env_for=lambda _s: LocalExecEnv(str(work)),
                 sleep=_no_sleep)
    client = Client(store)
    tid = await client.create("a", "read both")
    await rt.run_until_idle()

    _touch(a, "two\n")
    await client.send(tid, "edit b")
    await rt.run_until_idle()
    turn2 = provider.requests[2]
    assert _notes(turn2) == [
        f"Note: {a} changed on disk since you last read it. A change like this is usually "
        "deliberate, so take the file as it is now rather than reverting it; if the change "
        "looks wrong, say so instead of undoing it yourself, and otherwise there is no need "
        "to mention it. These are the changed lines, numbered as in the file now:\n1\ttwo\n2\t"]
    note = next(m for m in turn2.messages if m.text.startswith("Note: "))
    assert note.role == "system"
    assert len(_notes(provider.requests[3])) == 1  # told once; b was the model's own edit

    await client.send(tid, "again")
    await rt.run_until_idle()
    assert len(_notes(provider.requests[4])) == 1  # the earlier note, not a new one

    _touch(a, "three\n")
    a.unlink()
    await client.send(tid, "and again")  # a deleted file says nothing
    await rt.run_until_idle()
    assert len(_notes(provider.requests[5])) == 1

    a.write_text("four\n")
    _touch(a, "four\n")
    await client.send(tid, "last")
    await rt.run_until_idle()
    assert len(_notes(provider.requests[6])) == 2
    await store.close()


class Formatter:
    """A tool that rewrites a file behind the model's back, as a formatter
    run through Bash would."""

    name = "Format"
    description = "format"
    input_schema: dict[str, Any] = {"type": "object"}
    read_only = False
    clearable = False

    def __init__(self, path: Path, text: str) -> None:
        self.path, self.text = path, text

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        _touch(self.path, self.text)
        return text_output("formatted")


async def test_a_change_made_by_a_command_is_told_after_its_step_and_can_be_edited(
        tmp_path: Path) -> None:
    work = tmp_path / "w"
    work.mkdir()
    src = work / "calc.py"
    lines = [f"x{n} = {n}" for n in range(1, 31)]
    src.write_text("\n".join(lines) + "\n")
    changed = list(lines)
    changed[9] = "x10 = 100"
    store = SqliteStore(str(tmp_path / "t.db"))
    provider = ScriptedProvider([
        reply("", ("Read", {"file_path": str(src)})),
        reply("", ("Format", {})),
        reply("", ("Edit", {"file_path": str(src), "old_string": "x10 = 100",
                            "new_string": "x10 = 10"})),
        reply("done"),
    ])
    agent = Agent(name="a", model="m", tools=[Formatter(src, "\n".join(changed) + "\n")],
                  extensions=[FileTools()])
    rt = Runtime(store=store, agents={"a": agent}, provider_for=lambda _m: provider,
                 env_for=lambda _s: LocalExecEnv(str(work)), sleep=_no_sleep)
    client = Client(store)
    tid = await client.create("a", "go")
    await rt.run_until_idle()
    # Told in the very next request, with the changed line and 8 lines either side.
    [note] = _notes(provider.requests[2])
    shown = [f"{n}\tx{n} = {100 if n == 10 else n}" for n in range(2, 19)]
    assert note.endswith("numbered as in the file now:\n" + "\n".join(shown))
    # With the change shown, the edit needs no new Read.
    results = [r.event.result for r in await client.events(tid)
               if isinstance(r.event, ToolFinished)]
    assert [r.is_error for r in results] == [False, False, False], results[-1].text
    assert "x10 = 10\n" in src.read_text()
    await store.close()


async def test_a_file_read_in_part_gets_no_note(tmp_path: Path) -> None:
    work = tmp_path / "w"
    work.mkdir()
    src = work / "notes.txt"
    src.write_text("a\nb\nc\n")
    store = SqliteStore(str(tmp_path / "t.db"))
    provider = ScriptedProvider([
        reply("", ("Read", {"file_path": str(src), "offset": 2, "limit": 1})),
        reply("", ("Format", {})),
        reply("done"),
    ])
    agent = Agent(name="a", model="m", tools=[Formatter(src, "a\nB\nc\n")],
                  extensions=[FileTools()])
    rt = Runtime(store=store, agents={"a": agent}, provider_for=lambda _m: provider,
                 env_for=lambda _s: LocalExecEnv(str(work)), sleep=_no_sleep)
    await Client(store).create("a", "go")
    await rt.run_until_idle()
    assert _notes(provider.requests[2]) == []  # Claude Code sends none for a windowed read
    await store.close()


async def test_without_a_whole_view_in_the_conversation_the_changes_are_not_shown(
        tmp_path: Path) -> None:
    work = tmp_path / "w"
    work.mkdir()
    src = work / "notes.txt"
    src.write_text("a\nb\nc\n")
    store = SqliteStore(str(tmp_path / "t.db"))
    provider = ScriptedProvider([
        reply("", ("Read", {"file_path": str(src)})),
        reply("", ("Edit", {"file_path": str(src), "old_string": "c", "new_string": "C"})),
        reply("", ("Format", {})),
        reply("", ("Edit", {"file_path": str(src), "old_string": "B", "new_string": "b"})),
        reply("done"),
    ])
    agent = Agent(name="a", model="m", tools=[Formatter(src, "a\nB\nC\n")],
                  extensions=[FileTools()])
    rt = Runtime(store=store, agents={"a": agent}, provider_for=lambda _m: provider,
                 env_for=lambda _s: LocalExecEnv(str(work)), sleep=_no_sleep)
    client = Client(store)
    tid = await client.create("a", "go")
    await rt.run_until_idle()
    [note] = _notes(provider.requests[3])  # the last whole view is older than the edit
    assert note.endswith("The changes are not shown here; read the file if you need what "
                         "it holds now.")
    results = [r.event.result for r in await client.events(tid)
               if isinstance(r.event, ToolFinished)]
    assert results[-1].is_error  # the edit still wants a fresh read
    await store.close()


def test_changed_lines_and_the_view_they_are_worked_out_against() -> None:
    from noeta.core.types import Message, Text, ToolResult, ToolUse
    from noeta.plugins.fs.changes import changed_lines, seen_text

    assert changed_lines("a\nb", "a\nB") == "1\ta\n2\tB"
    assert changed_lines("a\nb\nc", "a\nc") == "1\ta\n2\tc"  # a removal shows where
    assert changed_lines("a", "a") is None
    # Changes far apart are separate blocks, one after the other (Claude Code's join).
    far = [str(n) for n in range(40)]
    moved = [*far[:2], "B", *far[3:30], "Z", *far[31:]]
    assert changed_lines("\n".join(far), "\n".join(moved)) == "\n".join(
        [f"{n + 1}\t{moved[n]}" for n in range(0, 11)]
        + [f"{n + 1}\t{moved[n]}" for n in range(22, 39)])
    # Past 8,192 characters the snippet is cut at a line end with a count.
    long = changed_lines("x", "\n".join("y" * 99 for _ in range(200)))
    assert long is not None and len(long) < 8300
    kept, marker = long.rsplit("\n", 1)
    assert len(kept) <= 8192 and kept.endswith("y" * 99)
    assert marker == f"... [{200 - kept.count(chr(10)) - 1} lines truncated] ..."

    def call(i: int, name: str, **input: Any) -> list[Message]:
        return [Message(role="assistant", content=[ToolUse(id=f"c{i}", name=name, input=input)]),
                Message(role="user", content=[ToolResult(call_id=f"c{i}", content=[
                    Text(text="1\tone\n2\t")])])]

    read = call(1, "Read", file_path="f.txt")
    assert seen_text(read, "/w/f.txt", "/w") == "one\n"
    assert seen_text([*read, *call(2, "Edit", file_path="/w/f.txt")], "/w/f.txt", "/w") is None
    assert seen_text(call(3, "Read", file_path="f.txt", offset=5), "/w/f.txt", "/w") is None


class CountingEnv(LocalExecEnv):
    """The local env, counting whole-file reads and how stats are asked for."""

    def __init__(self, cwd: str) -> None:
        super().__init__(cwd)
        self.reads: list[str] = []
        self.batches: list[int] = []

    async def read_bytes(self, path: str) -> bytes:
        self.reads.append(path)
        return await super().read_bytes(path)

    async def stat_many(self, paths: Any) -> Any:
        self.batches.append(len(paths))
        return await super().stat_many(paths)


async def test_every_remembered_file_is_watched_in_one_batch(tmp_path: Path) -> None:
    # Claude Code checks every file in its read-file state, not only the
    # newest few; here the changed one is the oldest of 30.
    work = tmp_path / "w"
    work.mkdir()
    files = [work / f"f{n}.txt" for n in range(30)]
    for f in files:
        f.write_text("same\n")
    env = CountingEnv(str(work))
    store = SqliteStore(str(tmp_path / "t.db"))
    provider = ScriptedProvider([
        reply("", *[("Read", {"file_path": str(f)}) for f in files]),
        reply("read"),
        reply("ok"),
        reply("ok"),
    ])
    rt = Runtime(store=store, agents={"a": Agent(name="a", model="m", extensions=[FileTools()])},
                 provider_for=lambda _m: provider, env_for=lambda _s: env, sleep=_no_sleep)
    client = Client(store)
    tid = await client.create("a", "read them")
    await rt.run_until_idle()

    _touch(files[0], "changed\n")
    _touch(files[1], "same\n")  # a new time, the same bytes: nothing to tell
    env.reads.clear()
    env.batches.clear()
    await client.send(tid, "go on")
    await rt.run_until_idle()
    [note] = _notes(provider.requests[2])
    assert note.startswith(f"Note: {files[0]} changed on disk")
    assert env.batches == [30]  # one stat_many for all of them
    assert sorted(env.reads) == sorted([str(files[0]), str(files[1])])

    env.reads.clear()
    await client.send(tid, "again")
    await rt.run_until_idle()
    assert env.reads == []  # neither is read again until its time moves
    assert len(_notes(provider.requests[3])) == 1  # the earlier note only
    await store.close()


async def test_a_utf16_file_s_changes_are_shown_decoded(tmp_path: Path) -> None:
    work = tmp_path / "w"
    work.mkdir()
    src = work / "wide.txt"
    src.write_bytes(b"\xff\xfe" + "one\ntwo\n".encode("utf-16-le"))
    store = SqliteStore(str(tmp_path / "t.db"))
    provider = ScriptedProvider([
        reply("", ("Read", {"file_path": str(src)})),
        reply("", ("Format", {})),
        reply("done"),
    ])

    class Wide(Formatter):
        async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
            src.write_bytes(b"\xff\xfe" + "one\nTWO\n".encode("utf-16-le"))
            st = src.stat()
            os.utime(src, (st.st_atime, st.st_mtime + 5))
            return text_output("formatted")

    agent = Agent(name="a", model="m", tools=[Wide(src, "")], extensions=[FileTools()])
    rt = Runtime(store=store, agents={"a": agent}, provider_for=lambda _m: provider,
                 env_for=lambda _s: LocalExecEnv(str(work)), sleep=_no_sleep)
    await Client(store).create("a", "go")
    await rt.run_until_idle()
    [note] = _notes(provider.requests[2])
    assert note.endswith("numbered as in the file now:\n1\tone\n2\tTWO\n3\t")
    await store.close()


async def test_one_check_shows_at_most_16k_of_changed_lines_and_skips_large_files(
        tmp_path: Path) -> None:
    # As Claude Code: the changed lines of all notes of one check share
    # 16,384 characters (later notes name the file only, and the file then
    # needs a Read before an edit); a file over Read's whole-file limit is
    # neither read back nor told about.
    from noeta.plugins.fs import CHANGED_FILE_MAX_BYTES

    work = tmp_path / "w"
    work.mkdir()
    files = [work / f"f{n}.txt" for n in range(4)]
    for f in files:
        f.write_text("".join(f"line {i:04d} old old old old old\n" for i in range(200)))
    big = work / "big.txt"
    big.write_text("x\n" * (CHANGED_FILE_MAX_BYTES // 2 + 10))
    env = CountingEnv(str(work))
    store = SqliteStore(str(tmp_path / "t.db"))
    provider = ScriptedProvider([
        reply("", *[("Read", {"file_path": str(f)}) for f in files],
              ("Read", {"file_path": str(big), "allow_large": True})),
        reply("read"),
        reply("ok"),
    ])
    rt = Runtime(store=store, agents={"a": Agent(name="a", model="m", extensions=[FileTools()])},
                 provider_for=lambda _m: provider, env_for=lambda _s: env, sleep=_no_sleep)
    client = Client(store)
    tid = await client.create("a", "read them")
    await rt.run_until_idle()

    for f in files:
        _touch(f, "".join(f"line {i:04d} new new new new new\n" for i in range(200)))
    _touch(big, "y\n" * (CHANGED_FILE_MAX_BYTES // 2 + 10))
    env.reads.clear()
    await client.send(tid, "go on")
    await rt.run_until_idle()
    notes = ["Note: " + n for m in _notes(provider.requests[2]) for n in m.split("Note: ")
             if n.strip()]
    assert sorted(n.split(" changed on disk")[0] for n in notes) == [
        f"Note: {f}" for f in files]
    shown = ["numbered as in the file now:" in n for n in notes]
    assert shown.count(True) == 3 and not shown[-1]
    assert str(big) not in env.reads
    await store.close()
