"""The drawing functions, in Claude Code's shapes."""

from __future__ import annotations

from noeta.core.types import ToolUse
from noeta_tui import render

DIFF = """--- a/calc.py
+++ b/calc.py
@@ -1,2 +1,2 @@
 def add(a, b):
-    return a - b
+    return a + b
"""


def call(name: str, **input: object) -> ToolUse:
    return ToolUse(id="c1", name=name, input=dict(input))


def test_tool_headers_use_claude_codes_names() -> None:
    assert render.tool_header(call("Bash", command="ls -la\necho hi")) == "Bash(ls -la…)"
    assert render.tool_header(call("Edit", file_path="calc.py")) == "Update(calc.py)"
    assert render.tool_header(call("Grep", pattern="def add", path="src")) == \
        'Search(pattern: "def add", path: "src")'
    assert render.tool_header(call("mcp__github__get_issue")) == "github - get_issue (MCP)"
    assert render.tool_header(call("Read", file_path="x" * 200)).endswith("…)")


def test_a_diff_is_numbered_and_coloured() -> None:
    rows = render.diff_rows(DIFF)
    assert rows == [("   1  def add(a, b):", ""), ("   2 -    return a - b", "red"),
                    ("   2 +    return a + b", "green")]
    meta = {"diff": DIFF, "added": 1, "removed": 1, "path": "calc.py"}
    first, *_ = render.result_rows(call("Edit", file_path="calc.py"), "ok", meta, is_error=False)
    assert first == ("Added 1 line, removed 1 line", "")
    assert render.change_summary(call("Write", file_path="n.py"), {"added": 3, "removed": 0,
                                                                   "path": "n.py"}) \
        == "Wrote 3 lines to n.py"
    assert render.diff_rows("@@ -1 +1 @@\n-a\n+b\n@@ -9 +9 @@\n-c\n+d")[2] == ("   ...", "dim")


def test_long_output_folds_with_the_expand_hint() -> None:
    rows = render.result_rows(call("Bash", command="seq 10"),
                              "\n".join(str(i) for i in range(1, 11)), {}, is_error=False)
    shown = render.folded(rows, diff=False, expanded=False)
    assert [r[0] for r in shown] == ["1", "2", "3", "… +7 lines (ctrl+o to expand)"]
    assert render.folded(rows, diff=False, expanded=True) == rows
    block = render.result_block(shown).plain.splitlines()
    assert block[0] == "  ⎿  1" and block[1] == "     2"
    read = render.result_rows(call("Read", file_path="a"), "1\ta\n2\tb", {}, is_error=False)
    assert render.folded(read, diff=False, expanded=False, read=True) == [
        ("Read 2 lines (ctrl+o to expand)", "")]
    assert render.result_rows(call("Bash"), "", {}, is_error=False) == [("(No content)", "dim")]
    assert render.result_rows(call("Bash"), "boom", {}, is_error=True) == [("boom", "red")]


def test_the_task_list_folds_the_todos_records() -> None:
    items: dict[str, dict[str, object]] = {}
    render.fold_todos(items, "created", {"id": "1", "subject": "Write tests",
                                         "active_form": "Writing tests"})
    render.fold_todos(items, "created", {"id": "2", "subject": "Ship"})
    render.fold_todos(items, "updated", {"id": "1", "fields": {"status": "in_progress"}})
    assert render.active_form(items) == "Writing tests"
    assert render.todo_block(items).plain == "  ⎿  ◼ Write tests\n     ◻ Ship"
    render.fold_todos(items, "updated", {"id": "1", "fields": {"status": "completed"}})
    render.fold_todos(items, "deleted", {"id": "2"})
    render.fold_todos(items, "updated", {"no": "id"})
    assert render.todo_block(items).plain == "  ⎿  ✔ Write tests" and not render.active_form(items)


def test_spinner_footer_and_numbers() -> None:
    line = render.spinner_line(3, "Thinking", 65, 1234).plain
    assert line == "✶ Thinking… (1m 5s · ↓ 1.2k tokens · esc to interrupt)"
    assert render.spinner_line(0, "Working", 2, 0).plain == "· Working… (2s · esc to interrupt)"
    assert render.mode_hint("acceptEdits").plain == "⏵⏵ accept edits on (shift+tab to cycle)"
    assert render.mode_hint("plan").plain == "⏸ plan mode on (shift+tab to cycle)"
    assert render.mode_hint("default").plain == "? for shortcuts"
    assert [render.tokens_text(n) for n in (950, 46_800, 1_000_000)] == ["950", "46.8k", "1m"]
    assert render.elapsed_text(3700) == "1h 1m"


def test_context_grid() -> None:
    grid = render.context_grid({
        "model": "claude-opus-5-5", "window": 200_000, "total": 30_000, "deferred_tools": 2,
        "categories": {"System prompt": 4_000, "System tools": 16_000, "MCP tools": 0,
                       "Messages": 10_000, "Free space": 170_000}}).plain
    lines = grid.splitlines()
    assert lines[0] == "Context Usage"
    assert lines[1].startswith("⛁ ⛁ ") and "claude-opus-5-5" in lines[1]
    assert "30k/200k tokens (15%)" in lines[2]
    assert grid.count("⛁") - 4 == 15 and grid.count("⛶") - 1 == 85  # 15 cells used
    assert "⛁ System tools: 16k tokens (8.0%)" in grid and "Free space: 170k (85.0%)" in grid
    assert "2 deferred tools load on demand" in grid
