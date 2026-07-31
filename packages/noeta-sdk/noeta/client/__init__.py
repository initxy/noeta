"""The SDK assembly layer: sugar types that compile into runtime identity.

Everything here is a library-user entrypoint. :class:`Options` and friends are
lightweight recipes; :func:`compile_options` turns them into the canonical
``noeta.agent.spec`` objects a host registers and resolves, and
:class:`Client` / :func:`query` drive a live engine over that identity.
"""

from noeta.client.client import Client, QueryFailedError, QueryResult, query
from noeta.client.host import SdkHost
from noeta.client.messages import (
    AssistantMessage,
    Result,
    ToolResultView,
    ToolUse,
    UserMessage,
    as_messages,
)
from noeta.client.options import (
    AgentDefinition,
    Options,
    SystemPromptPreset,
    compile_options,
    register_preset_prompt,
)
from noeta.client.parts import builtin_tool_ref


__all__ = [
    "AgentDefinition",
    "AssistantMessage",
    "Client",
    "Options",
    "QueryFailedError",
    "QueryResult",
    "Result",
    "SdkHost",
    "SystemPromptPreset",
    "ToolResultView",
    "ToolUse",
    "UserMessage",
    "as_messages",
    "builtin_tool_ref",
    "compile_options",
    "query",
    "register_preset_prompt",
]
