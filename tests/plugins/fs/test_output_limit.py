"""The large-output rule (Claude Code's persisted / truncated block), the
tools that apply it, and the model-facing names, descriptions and schemas,
which must stay exactly what 0.6 sent."""

from __future__ import annotations

from importlib import resources
from pathlib import Path
from typing import Any

from noeta.core.tool_helpers import large_output_message, limit_output, spill_path
from noeta.plugins.fs import EditTool, GlobTool, GrepTool, ReadTool, WriteTool
from noeta.plugins.shell import BashTool
from tests.plugins.conftest import Harness, text_of

# ---------------------------------------------------------------------------
# large_output_message
# ---------------------------------------------------------------------------


def test_persisted_output_is_claude_codes_block() -> None:
    body = "\n".join(f"line {i:04d}" for i in range(6000))  # 59,999 chars
    out = large_output_message(body, 50_000, saved_to="/spill/t/c1.txt")
    head, preview_and_tail = out.split("Preview (first 2KB):\n")
    assert head == (
        "<persisted-output>\n"
        "Output too large (58.6KB). Full output saved to: /spill/t/c1.txt\n\n"
    )
    preview, tail = preview_and_tail.rsplit("\n...\n", 1)
    assert tail == "</persisted-output>"
    assert body.startswith(preview + "\n")
    assert 1_000 < len(preview) <= 2_000


def test_persisted_output_without_more_has_no_ellipsis() -> None:
    assert large_output_message("abc", 2, saved_to="/f.txt") == (
        "<persisted-output>\n"
        "Output too large (3 bytes). Full output saved to: /f.txt\n\n"
        "Preview (first 2KB):\nabc\n</persisted-output>"
    )


def test_unsaved_output_keeps_the_start_and_end_within_the_limit() -> None:
    body = "x" * 5_000 + "y" * 5_000
    out = large_output_message(body, 1_000)
    assert out.startswith(
        "<truncated-output>\nOutput too large (9.8KB). It could not be saved, "
        "so only the first "
    )
    assert (
        "the middle was dropped. If the tool can page or filter its results, "
        "call it again for the part you need." in out
    )
    assert out.endswith("y\n</truncated-output>")
    assert len(out) <= 1_000
    assert "x" * 300 in out and "y" * 300 in out
    assert " characters dropped] ...\n" in out


def test_unsaved_output_keeps_the_trailing_error() -> None:
    lines = [f"line {i} ok" for i in range(4000)] + ["FAILED test_x: AssertionError"]
    out = large_output_message("\n".join(lines), 30_000)
    assert len(out) <= 30_000
    assert "line 0 ok\n" in out
    assert out.endswith("\nFAILED test_x: AssertionError\n</truncated-output>")


def test_size_labels_follow_claude_code() -> None:
    assert "(1023 bytes)" in large_output_message("a" * 1023, 10, saved_to="p")
    assert "(1KB)" in large_output_message("a" * 1024, 10, saved_to="p")
    assert "(1.5MB)" in large_output_message("a" * (3 * 512 * 1024), 10, saved_to="p")


def test_spill_paths_are_a_function_of_task_and_call() -> None:
    assert spill_path("/spill", "task/1", "toolu_01:x") == "/spill/task_1/toolu_01_x.txt"
    assert spill_path("/spill", "", "") == "/spill/task/call.txt"


# ---------------------------------------------------------------------------
# limit_output
# ---------------------------------------------------------------------------


async def test_limit_output_passes_short_text_through(h: Harness, ws: Path) -> None:
    h.spill_dir = str(ws.parent / "spill")
    assert await limit_output("short", 10, h.ctx()) == "short"
    assert not (ws.parent / "spill").exists()


async def test_limit_output_spills_through_the_env(h: Harness, ws: Path) -> None:
    h.spill_dir = str(ws.parent / "spill")
    body = "z" * 100
    out = await limit_output(body, 10, h.ctx("call/9"))
    saved = ws.parent / "spill" / "task-1" / "call_9.txt"
    assert saved.read_text() == body
    assert out.startswith(f"<persisted-output>\nOutput too large (100 bytes). Full output saved to: {saved}\n")


async def test_limit_output_without_spill_dir_cuts(h: Harness) -> None:
    out = await limit_output("x" * 5_000 + "y" * 5_000, 1_000, h.ctx())
    assert out.startswith("<truncated-output>\n")
    assert len(out) <= 1_000


async def test_limit_output_cuts_when_the_spill_fails(h: Harness, ws: Path) -> None:
    blocker = ws.parent / "file"
    blocker.write_text("x")
    h.spill_dir = str(blocker)  # a file where a directory must go
    out = await limit_output("x" * 5_000, 1_000, h.ctx())
    assert out.startswith("<truncated-output>\n")


async def test_grep_spills_past_20000_chars(h: Harness, ws: Path) -> None:
    h.spill_dir = str(ws.parent / "spill")
    (ws / "many.txt").write_text(("needle " + "q" * 100 + "\n") * 240)
    out = await h.call(GrepTool(), pattern="needle", output_mode="content")
    text = text_of(out)
    assert not out.is_error
    assert text.startswith("<persisted-output>\nOutput too large (")
    [saved] = list((ws.parent / "spill").rglob("*.txt"))
    assert saved.read_text().startswith("many.txt:1:needle ")
    assert out.meta["matches"] == 240


async def test_grep_under_20000_stays_inline(h: Harness, ws: Path) -> None:
    (ws / "many.txt").write_text(("needle " + "q" * 50 + "\n") * 100)
    text = text_of(await h.call(GrepTool(), pattern="needle", output_mode="content"))
    assert text.startswith("many.txt:1:needle ")


async def test_glob_cuts_past_50000_chars_without_spill_dir(h: Harness, ws: Path) -> None:
    deep = ws / ("d" * 200)
    deep.mkdir()
    for i in range(200):
        (deep / f"{'f' * 200}{i:03}.py").write_text("")
    out = await h.call(GlobTool(), pattern="**/*.py")
    text = text_of(out)
    assert text.startswith("<truncated-output>\n")
    assert len(text) <= 50_000


# ---------------------------------------------------------------------------
# What the model sees: names, descriptions, schemas (0.6's bytes)
# ---------------------------------------------------------------------------

_EXPECTED_SCHEMAS: dict[str, dict[str, Any]] = {
    "Bash": {
        "additionalProperties": False,
        "properties": {
            "command": {"type": "string"},
            "description": {"type": "string"},
            "run_in_background": {"type": "boolean"},
            "timeout": {"type": "number"},
        },
        "required": ["command"],
        "type": "object",
    },
    "Edit": {
        "additionalProperties": False,
        "properties": {
            "file_path": {"type": "string"},
            "new_string": {"type": "string"},
            "old_string": {"type": "string"},
            "replace_all": {"type": "boolean"},
        },
        "required": ["file_path", "old_string", "new_string"],
        "type": "object",
    },
    "Glob": {
        "additionalProperties": False,
        "properties": {"path": {"type": "string"}, "pattern": {"type": "string"}},
        "required": ["pattern"],
        "type": "object",
    },
    "Grep": {
        "additionalProperties": False,
        "properties": {
            "-A": {"minimum": 0, "type": "integer"},
            "-B": {"minimum": 0, "type": "integer"},
            "-C": {"minimum": 0, "type": "integer"},
            "-i": {"type": "boolean"},
            "-n": {"type": "boolean"},
            "-o": {"type": "boolean"},
            "-u": {"type": "boolean"},
            "context": {"minimum": 0, "type": "integer"},
            "glob": {"type": "string"},
            "head_limit": {"minimum": 0, "type": "integer"},
            "multiline": {"type": "boolean"},
            "offset": {"minimum": 0, "type": "integer"},
            "output_mode": {
                "enum": ["files_with_matches", "content", "count"],
                "type": "string",
            },
            "path": {"type": "string"},
            "pattern": {"type": "string"},
            "type": {"type": "string"},
        },
        "required": ["pattern"],
        "type": "object",
    },
    "Read": {
        "additionalProperties": False,
        "properties": {
            "file_path": {"type": "string"},
            "limit": {"minimum": 1, "type": "integer"},
            "offset": {"minimum": 1, "type": "integer"},
            "pages": {"type": "string"},
        },
        "required": ["file_path"],
        "type": "object",
    },
    "Write": {
        "additionalProperties": False,
        "properties": {"content": {"type": "string"}, "file_path": {"type": "string"}},
        "required": ["file_path", "content"],
        "type": "object",
    },
}

_TOOLS: dict[str, tuple[Any, str]] = {
    "Bash": (BashTool(), "shell_run"),
    "Edit": (EditTool(), "edit"),
    "Glob": (GlobTool(), "glob"),
    "Grep": (GrepTool(), "grep"),
    "Read": (ReadTool(), "read"),
    "Write": (WriteTool(), "write"),
}


def test_tool_names_and_schemas_match_0_6() -> None:
    for name, (tool, _) in _TOOLS.items():
        assert tool.name == name
        assert tool.input_schema == _EXPECTED_SCHEMAS[name], name


def test_descriptions_are_the_md_files_minus_trailing_newlines() -> None:
    for name, (tool, md) in _TOOLS.items():
        package = "noeta.plugins.shell" if name == "Bash" else "noeta.plugins.fs"
        raw = (
            resources.files(package)
            .joinpath("descriptions", f"{md}.md")
            .read_bytes()
        )
        assert raw.endswith(b"\n")
        assert tool.description.encode("utf-8") == raw.rstrip(b"\n"), name


def test_description_fingerprints() -> None:
    # Spot checks so an accidental edit of a .md file is caught here too.
    assert ReadTool().description.startswith("Reads a file from the filesystem.\n\n")
    assert GrepTool().description.endswith("To match by filename use `Glob`.")
    assert GlobTool().description.endswith("to read a known file use `Read`.")


def test_read_only_and_clearable_flags_match_0_6() -> None:
    flags = {name: (t.read_only, t.clearable) for name, (t, _) in _TOOLS.items()}
    assert flags == {
        "Bash": (False, True),
        "Edit": (False, True),
        "Glob": (True, True),
        "Grep": (True, True),
        "Read": (True, True),
        "Write": (False, True),
    }


def test_schemas_are_per_instance() -> None:
    a, b = GrepTool(), GrepTool()
    a.input_schema["properties"]["x"] = {}
    assert "x" not in b.input_schema["properties"]
