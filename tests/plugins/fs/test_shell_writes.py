"""A ``Bash`` command that rewrote files the model had read ends its result
with a note naming them, as in Claude Code; the changed-file reminder still
follows."""

from __future__ import annotations

from pathlib import Path

from noeta.plugins.fs.shell_writes import WRITE_MARKERS
from tests.plugins.fs.test_shell_reads import Formatter, Scenario, Step, _bash, _edit, _notes


def _read(path: Path) -> Step:
    return ("Read", {"file_path": str(path)})


def test_the_markers_are_claude_codes() -> None:
    for command in ("sed -i 's/a/b/' f", "gsed -Ei s/a/b/ f", "prettier --write .",
                    "eslint --fix src", "ruff format x.py", "black .", "cargo fmt",
                    "npm run format", "go fmt ./...", "perl -pi --in-place f"):
        assert WRITE_MARKERS.search(command), command
    for command in ("sed -n 1,5p f", "cat f", "ruff check .", "echo x > f", "git commit"):
        assert not WRITE_MARKERS.search(command), command


async def test_sed_in_place_names_the_read_file_it_changed(tmp_path: Path) -> None:
    run = Scenario(tmp_path)
    a, b = run.work / "a.txt", run.work / "b.txt"
    a.write_text("one\ntwo\n")
    b.write_text("keep\n")
    run.steps = (_read(a), _read(b), _bash("sed -i.bak 's/two/2/' a.txt"), _edit(a, "2", "two"))
    results, provider, _ = await run.go()
    # sed printed nothing: the note is the whole result.
    assert results[2].text == (
        "[Running this command changed 1 file you had read: a.txt. "
        "Read the new contents with Read before you edit any of them.]")
    # The read entry is left alone: the changed-file reminder still shows
    # what changed before the next model call (which, as Claude Code's
    # re-read, lets the edit through).
    [note] = _notes(provider.requests[3])
    assert note.startswith(f"Note: {a.resolve()} changed on disk") and "2\t2" in note


async def test_output_comes_first_and_past_five_files_the_rest_are_counted(
        tmp_path: Path) -> None:
    run = Scenario(tmp_path)
    files = [run.work / f"f{n}.txt" for n in range(7)]
    for f in files:
        f.write_text("x\n")
    run.steps = (*(_read(f) for f in files),
                 _bash("sed -i.bak 's/x/y/' f*.txt && echo done --fix"))
    results, _, _ = await run.go()
    out = results[-1].text
    assert out.startswith("done --fix\n[Running this command changed 7 files you had read: "
                          "f0.txt, f1.txt, f2.txt, f3.txt, f4.txt and 2 more.")


async def test_no_note_without_a_marker_or_for_a_change_already_told(tmp_path: Path) -> None:
    run = Scenario(tmp_path)
    a, b = run.work / "a.txt", run.work / "b.txt"
    a.write_text("one\n")
    b.write_text("two\n")
    run.tools = [Formatter(b, "TWO\n")]
    run.steps = (_read(a), _read(b), _bash("echo changed > a.txt"),
                 ("Format", {}),  # b changes; the changed-file check tells the model
                 _bash("sed -i.bak 's/zzz/y/' a.txt"))
    results, _, _ = await run.go()
    assert "Running this command" not in results[2].text
    # a was rewritten by the redirect, b by the formatter: both were seen by
    # the changed-file check before this command, and sed matched nothing
    # but still rewrote a.
    assert results[4].text.startswith("[Running this command changed 1 file you had read: a.txt.")
