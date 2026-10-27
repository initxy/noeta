"""Sub-agents: the ``Task`` tool, background children and their notices,
and the official sub-agent definitions (``agents``)."""

from noeta.plugins.subtasks.extension import SubagentSpec, Subtasks
from noeta.plugins.subtasks.texts import TASK_TOOL
from noeta.plugins.subtasks.tool import DEPTH_KEY, TYPE_KEY, UNATTENDED_KEY, TaskTool

__all__ = [
    "DEPTH_KEY",
    "TASK_TOOL",
    "TYPE_KEY",
    "UNATTENDED_KEY",
    "SubagentSpec",
    "Subtasks",
    "TaskTool",
]
