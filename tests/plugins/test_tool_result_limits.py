"""The tools Claude Code caps below the 50,000-character default declare
the same limit (2.1.312 bundle, each tool's ``maxResultSizeChars``)."""

from __future__ import annotations

from noeta.plugins.findings import ReportFindingsTool
from noeta.plugins.fs.read import ReadTool
from noeta.plugins.schedule import WakeupTool
from noeta.plugins.shell.bash import BashTool
from noeta.plugins.shell.tools import MonitorTool
from noeta.plugins.subtasks.tool import ListAgentsTool


def test_tools_capped_lower_in_claude_code_declare_their_limit() -> None:
    tools = (BashTool, MonitorTool, ListAgentsTool, WakeupTool, ReportFindingsTool, ReadTool)
    assert {t.name: t.max_result_chars for t in tools} == {
        "Bash": 30_000, "Monitor": 10_000, "ListAgents": 10_000, "ScheduleWakeup": 1_000,
        "ReportFindings": 256, "Read": None}
