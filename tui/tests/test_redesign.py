"""The 2026-11 pass over the terminal UI: the transcript stays at the top
while it is short, reasoning streams into its own line and counts toward
the spinner's tokens, a background agent and a workflow show their progress
(not the model's bookkeeping), the slash menu keeps one height, dropped
files attach as images, and the footer carries the effort."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from noeta.core.events import ChildEnded
from noeta.core.types import Delta, ToolUse
from noeta.testing import ScriptedProvider, reply
from noeta_tui import render
from noeta_tui.widgets import Completions, ThinkingBlock, ToolCall, dropped_paths

from support import MODEL, make_app, plain, ready, settle, type_line


class Thinks(ScriptedProvider):
    """Reasons a little before each reply."""

    async def complete(self, request: Any, on_delta: Any = None) -> Any:
        if on_delta is not None:
            on_delta(Delta(kind="thinking", text="weighing it up " * 20))
        return await super().complete(request, on_delta)


async def test_reasoning_streams_into_its_line_and_counts(tmp_path: Path) -> None:
    app = make_app(tmp_path, Thinks([reply("done")]))
    async with app.run_test(size=(120, 36)) as pilot:
        await settle(pilot, lambda: ready(app))
        await type_line(pilot, "go")
        await settle(pilot, lambda: not app.busy and bool(app.query(ThinkingBlock)))
        block = app.query_one(ThinkingBlock)
        assert plain(block).startswith("∴ Thought for ") and "ctrl+o to show" in plain(block)
        await pilot.press("ctrl+o")
        assert "weighing it up" in plain(block)


async def test_a_short_transcript_stays_at_the_top(tmp_path: Path) -> None:
    app = make_app(tmp_path, ScriptedProvider([reply("hello there")]))
    async with app.run_test(size=(120, 36)) as pilot:
        await settle(pilot, lambda: ready(app))
        await type_line(pilot, "hi")
        await settle(pilot, lambda: not app.busy and "hello there" in str(
            [m.source for m in app.query("Markdown")]))
        await pilot.pause(0.1)
        transcript = app.query_one("#transcript")
        assert transcript.scroll_y >= 0 and transcript.max_scroll_y == 0


def _agent_call() -> ToolCall:
    return ToolCall(ToolUse(id="c1", name="Agent",
                            input={"description": "Explore", "prompt": "look around"}))


async def test_a_background_agent_shows_its_progress_then_how_it_ended(tmp_path: Path) -> None:
    app = make_app(tmp_path, ScriptedProvider([]))
    async with app.run_test(size=(120, 36)) as pilot:
        await settle(pilot, lambda: ready(app))
        call = _agent_call()
        await app.query_one("#transcript").mount(call)
        call.set_result("The agent was launched … agentId: x (internal)", is_error=False,
                        meta={"child_id": "child-1"}, at=1000.0)
        app.agents["child-1"] = call
        body = plain(call.body)
        assert "agentId" not in body and "internal" not in body
        assert "Starting…" in body and "in the background" in body
        assert call.state == "background"
        app.update_status()
        assert "◆ 1 agent running" in plain(app.query_one("#hints"))
        app._agent_ended(ChildEnded(child_id="child-1", status="done", answer="all good",
                                    tool_calls=3, output_tokens=1200), 1012.0)
        assert plain(call.body).strip() == "⎿  Done · 3 tool uses · 1.2k tokens · 12s"
        assert call.state == "done" and not app.agents
        call.set_expanded(True)
        assert "all good" in plain(call.body) and "Prompt: look around" in plain(call.body)


async def test_a_failed_agent_says_why(tmp_path: Path) -> None:
    app = make_app(tmp_path, ScriptedProvider([]))
    async with app.run_test(size=(120, 36)) as pilot:
        await settle(pilot, lambda: ready(app))
        call = _agent_call()
        await app.query_one("#transcript").mount(call)
        call.set_result("launched", is_error=False, meta={"child_id": "k"}, at=0.0)
        call.end_agent("failed", error="boom", at=5.0)
        assert "Failed: boom" in plain(call.body) and call.state == "error"


async def test_a_workflow_folds_its_records_into_its_call(tmp_path: Path) -> None:
    app = make_app(tmp_path, ScriptedProvider([]))
    async with app.run_test(size=(120, 36)) as pilot:
        await settle(pilot, lambda: ready(app))
        call = ToolCall(ToolUse(id="w1", name="Workflow", input={"script": "…"}))
        app.calls["w1"] = call
        await app.query_one("#transcript").mount(call)
        app._workflow_record("run", {"run_id": "wf_1", "call_id": "w1",
                                     "description": "Review the diff"})
        for key, label in (("a", "review:bugs"), ("b", "review:perf")):
            app._workflow_record("agent", {"run_id": "wf_1", "key": key, "label": label,
                                           "phase": "Review"})
        call.set_result('Workflow "x" started …', is_error=False, meta={"run_id": "wf_1"})
        body = plain(call.body)
        assert "Review the diff" in body and "0/2 agents done" in body
        assert "Review · review:bugs" in body and "started" not in body
        assert app.background_agents() == 2
        app._workflow_record("result", {"run_id": "wf_1", "key": "a"})
        app._workflow_record("agent_failed", {"run_id": "wf_1", "key": "b", "error": "x"})
        app._workflow_record("ended", {"run_id": "wf_1", "status": "completed"})
        body = plain(call.body)
        assert "1/2 agents done, 1 failed" in body and "Completed" in body
        assert call.state == "done" and app.background_agents() == 0


async def test_the_slash_menu_keeps_one_height(tmp_path: Path) -> None:
    app = make_app(tmp_path, ScriptedProvider([]))
    async with app.run_test(size=(120, 36)) as pilot:
        await settle(pilot, lambda: bool(app.plugin_commands))
        menu = app.query_one(Completions)
        box = app.query_one("#prompt-box")
        await pilot.press("/")
        await pilot.pause(0.05)
        top, height = box.region.y, menu.region.height
        assert not app.query_one("#footer").display  # the menu takes its place
        await pilot.press("c", "o", "s")
        await pilot.pause(0.05)
        assert (box.region.y, menu.region.height) == (top, height) == (top, Completions.ROWS)
        await pilot.press("escape")
        await pilot.pause(0.05)
        assert app.query_one("#footer").display


def test_dropped_files_are_split_into_paths() -> None:
    assert dropped_paths("/tmp/a.png /tmp/my\\ shot.png") == ["/tmp/a.png", "/tmp/my shot.png"]
    assert dropped_paths("'/tmp/a b.png'\n/tmp/c.png") == ["/tmp/a b.png", "/tmp/c.png"]
    assert dropped_paths("file:///tmp/x%20y.png") == ["/tmp/x y.png"]
    assert dropped_paths("") == []


async def test_dropping_two_images_attaches_both(tmp_path: Path) -> None:
    from support import png

    one, two = tmp_path / "one.png", tmp_path / "two shot.png"
    one.write_bytes(png())
    two.write_bytes(png())
    app = make_app(tmp_path, ScriptedProvider([]))
    async with app.run_test(size=(120, 36)) as pilot:
        await settle(pilot, lambda: ready(app))
        from textual import events

        prompt = app.query_one("#prompt")
        prompt.post_message(events.Paste(f"{one} '{two}'"))
        await settle(pilot, lambda: len(app.images) == 2)
        shown = plain(app.query_one("#attachments"))
        assert "Image #1 one.png" in shown and "Image #2 two shot.png" in shown
        assert prompt.text == "[Image #1] [Image #2] "


def test_hanging_rows_wrap_under_their_text() -> None:
    from rich.console import Console

    console = Console(width=30, record=True)
    console.print(render.result_block([("word " * 10, "")]))
    lines = console.export_text().splitlines()
    assert lines[0].startswith("  ⎿  word") and all(line.startswith("     word")
                                                   for line in lines[1:])


def test_the_footer_bar_and_the_logo() -> None:
    assert render.context_bar(0.25).plain == "▰▰▱▱▱▱▱▱ 25%"
    assert len(render.logo().plain.splitlines()) == 4
    assert MODEL
