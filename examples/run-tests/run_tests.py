"""run_tests: a RunTests tool that runs the project's test command where the
task's commands run (this machine or a container), keeps the last result in
the task log, and reminds the model once when files changed since the last
run. Copy this file to ~/.noeta/plugins/; it joins the main agent by itself."""

from __future__ import annotations

import shlex
from collections.abc import Sequence
from typing import Annotated, Any

from noeta import (
    Extension,
    Reminder,
    TaskView,
    Tool,
    ToolContext,
    ToolOutput,
    ToolUse,
    function_tool,
)
from noeta import text_output as output


class RunTests(Extension):
    """``command``: the test command; ``timeout``: seconds."""

    name = "run_tests"
    add_to = ("main",)

    def __init__(self, command: str = "pytest -q", timeout: float = 900) -> None:
        self.command, self.timeout = command, timeout

        @function_tool(name="RunTests")
        async def run(ctx: ToolContext,
                      target: Annotated[str, "a test file or node id"] = "") -> ToolOutput:
            """Run the project's tests (all, or one target) and return the exit
            code and the end of the output."""
            argv = [*shlex.split(self.command), *([target] if target else [])]
            res = await ctx.env.run(argv, timeout=self.timeout, output_cap=20_000)
            ctx.record(self.name, "ran", {"exit": res.exit_code})
            text = (res.stdout + res.stderr).decode(errors="replace")[-8_000:]
            status = "timed out" if res.timed_out else f"exit {res.exit_code}"
            return output(f"{shlex.join(argv)}: {status}\n{text}", is_error=res.exit_code != 0)

        self._tool = run

    def initial_state(self) -> Any:
        return {"last_exit": None, "edited": False, "nudged": False}

    def reduce(self, state: Any, kind: str, data: Any) -> Any:
        if kind == "ran":
            return {"last_exit": data["exit"], "edited": False, "nudged": False}
        return {**state, kind: True}  # "edited" or "nudged"

    def tools(self, view: TaskView) -> Sequence[Tool]:
        return [self._tool]

    async def after_tool(self, view: TaskView, call: ToolUse, out: ToolOutput) -> ToolOutput:
        if call.name in ("Edit", "Write") and not out.is_error:
            view.record(self.name, "edited")
        return out

    async def reminders(self, view: TaskView) -> Sequence[Reminder]:
        state = view.ext_state(self.name) or self.initial_state()
        if state["last_exit"] is None or not state["edited"] or state["nudged"]:
            return ()
        view.record(self.name, "nudged")
        text = view.prompt("run_tests/untested",
                           "Files changed since the tests last ran; run them before finishing.")
        return [Reminder(text=text, kind="untested")]
