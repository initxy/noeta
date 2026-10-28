"""Sub-agents: ``Agent``, ``SubagentHandback``, ``SendMessage``,
``ListAgents``, background children and their notices."""

from noeta.plugins.subtasks.extension import SubagentSpec, Subtasks
from noeta.plugins.subtasks.texts import AGENT_TOOL, HANDBACK_TOOL
from noeta.plugins.subtasks.tool import (
    AGENT_ID_KEY,
    DEPTH_KEY,
    TYPE_KEY,
    UNATTENDED_KEY,
    AgentTool,
    HandbackTool,
)

__all__ = [
    "AGENT_ID_KEY",
    "AGENT_TOOL",
    "DEPTH_KEY",
    "HANDBACK_TOOL",
    "TYPE_KEY",
    "UNATTENDED_KEY",
    "AgentTool",
    "HandbackTool",
    "SubagentSpec",
    "Subtasks",
]
