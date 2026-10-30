"""InstructionsLoaded: the hooks plugin fires Claude Code's event for each
instruction file the instructions plugin shows, read from its records."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from noeta.plugins.compaction import Compaction
from noeta.plugins.compaction.extension import command_text
from noeta.plugins.instructions import Instructions
from noeta.testing import reply
from tests.plugins.test_hooks_events import Setup, hook


def _shown(payloads: list[dict[str, Any]]) -> list[tuple[str, str, str]]:
    return [(Path(p["file_path"]).name, p["memory_type"], p["load_reason"]) for p in payloads]


async def test_instructions_loaded_fires_for_every_file_shown(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / "NOETA.md").write_text("User rules.\n")
    s = Setup(tmp_path, {"InstructionsLoaded": hook("loaded")},
              [reply("", ("Read", {"file_path": "sub/x.py"})), reply("read"),
               reply("again"), reply("summary text"), reply("after")],
              extensions=(Compaction(auto=False),
                          Instructions(walk=False, user_dir=str(home))))
    (s.work / "NOETA.md").write_text("Project rules. See @extra.md\n")
    (s.work / "extra.md").write_text("Imported.\n")
    (s.work / "sub").mkdir()
    (s.work / "sub" / "NOETA.md").write_text("Sub rules.\n")
    (s.work / "sub" / "x.py").write_text("x = 1\n")
    rules = s.work / ".noeta" / "rules"
    rules.mkdir(parents=True)
    (rules / "py.md").write_text("---\npaths: ['**/*.py']\n---\nPython rules.\n")
    await s.start()
    loaded = s.runner.payloads("loaded")
    assert _shown(loaded) == [
        ("NOETA.md", "User", "session_start"), ("NOETA.md", "Project", "session_start"),
        ("extra.md", "Project", "include"),
        ("py.md", "Project", "path_glob_match"), ("NOETA.md", "Project", "nested_traversal")]
    assert loaded[0]["hook_event_name"] == "InstructionsLoaded"
    assert loaded[2]["parent_file_path"] == str(s.work / "NOETA.md")
    trigger = str((s.work / "sub" / "x.py").resolve())
    assert loaded[3]["globs"] == ["**/*.py"] and loaded[3]["trigger_file_path"] == trigger
    assert loaded[4]["trigger_file_path"] == trigger and "globs" not in loaded[4]
    assert "trigger_file_path" not in loaded[0]

    # A changed file is shown again with the next message: loaded again.
    (s.work / "sub" / "NOETA.md").write_text("Sub rules, changed.\n")
    await s.send("go on")
    assert _shown(s.runner.payloads("loaded")[5:]) == [("NOETA.md", "Project", "session_start")]

    # A compaction puts every file ahead of its summary.
    await s.send(command_text(""))
    compacted = s.runner.payloads("loaded")[6:]
    assert {p["load_reason"] for p in compacted} == {"compact"}
    assert len(compacted) == 5


async def test_instructions_loaded_matcher_is_the_load_reason(tmp_path: Path) -> None:
    s = Setup(tmp_path, {"InstructionsLoaded": hook("loaded", "nested_traversal")},
              [reply("", ("Read", {"file_path": "sub/x.txt"})), reply("read")],
              extensions=(Instructions(walk=False),))
    (s.work / "NOETA.md").write_text("Project rules.\n")
    (s.work / "sub").mkdir()
    (s.work / "sub" / "NOETA.md").write_text("Sub rules.\n")
    (s.work / "sub" / "x.txt").write_text("x\n")
    await s.start()
    assert _shown(s.runner.payloads("loaded")) == [("NOETA.md", "Project", "nested_traversal")]
