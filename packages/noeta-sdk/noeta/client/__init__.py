"""``noeta.client`` — the SDK public face.

This package exposes the *library user* entrypoints with a Claude-Agent-SDK
shape — lightweight sugar types that compile into the canonical
``noeta.agent.spec`` identity objects the runtime hosts register and resolve.

Slice 4a (this file) lands:

* :class:`Options` — the human-friendly recipe dataclass.
* :func:`compile_options` — pure function turning ``Options`` into
  ``(main_AgentSpec, tuple_of_descendant_AgentSpecs)``.
* :func:`builtin_tool_ref` — resolve a built-in tool name to its canonical
  :class:`~noeta.agent.spec.ToolRef` (part of the SDK "batteries" parts table;
  shared by :func:`compile_options` for string tool entries and by callers
  who want to build spec tool lists manually).

Future slices (4b/4c):

* ``Client`` — a runtime host that wires compiled specs to a provider +
  storage and exposes ``start``/``send_goal``/… methods.
* ``query`` — one-shot ``Iterator[EventEnvelope]`` driver.
* ``as_messages`` — event-envelope → human-readable message view.
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
