"""``noeta.sdk`` — the one public import surface for the Noeta SDK.

Library users import everything from here::

    from noeta.sdk import query, Client, Options, tool

and never touch noeta-runtime internals or ``noeta.client`` directly. Like
claude-agent-sdk / LangChain: import the SDK, run an agent in-process; the
engine (noeta-runtime) is a transitive dependency the user never imports.

This module is **re-export only** — no logic. The real implementations live in
``noeta.client.*`` (the thin client, this wheel) and ``noeta.*`` (the runtime
engine). ``noeta.client`` stays importable for now to limit churn, but it is no
longer the advertised public path.

Surface landed in T2: the client verbs (``query`` / ``Client``), the recipe
(``Options`` / ``AgentDefinition`` / ``SystemPromptPreset``), the message
projection (``as_messages`` + message/content types), the authoring API
(``tool`` / ``create_sdk_mcp_server``), and the official ``presets``. The
pluggable **extension interfaces** (``Tool`` / ``LLMProvider`` / ``Policy`` /
``Guard`` / ``Observer`` / ``ContentKindSpec``) are wired and re-exported in T3.
"""

from __future__ import annotations

from noeta import presets
from noeta.client.capabilities import (
    effort_modes,
    model_capabilities,
    permission_modes,
)
from noeta.client.client import Client, QueryFailedError, QueryResult, query
from noeta.client.host_config import HostConfig
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
from noeta.client.wire import envelope_to_dict
from noeta.sdk.authoring import (
    DecoratedTool,
    SdkMcpServer,
    create_sdk_mcp_server,
    tool,
)

# --- Extension interfaces (T3) ------------------------------------------------
# Users implement these and mount them through the matching ``Options`` field
# (compile_options + the Client wire them into the runtime). Re-exported from
# the runtime protocol modules so there is one canonical type per extension.
from noeta.context.content_channel import ContentKindSpec
from noeta.protocols.event_log import Subscriber as Observer
from noeta.protocols.hooks import (
    Guard,
    GuardContext,
    ProposedAction,
    VerdictResult,
)
from noeta.protocols.decisions import Decision
from noeta.protocols.messages import ImageBlock, LLMProvider
from noeta.protocols.policy import Policy
from noeta.protocols.values import ContentRef
from noeta.protocols.step_context import StepContext
from noeta.protocols.tool import Tool, ToolContext, ToolResult
from noeta.protocols.view import View

# --- Host-level wiring (D3) ---------------------------------------------------
# The host-config surface: durable storage + host runtime injections (preview
# gateway, live-MCP resolver). Separate from Options (which carries agent
# identity); a product backend passes a populated HostConfig to opt into durable
# storage / preview / MCP while still driving the engine only through noeta.sdk.
from noeta.tools.app import AppMount, AppPreviewGateway
from noeta.tools.mcp import (
    HttpPostFn,
    McpAnyServerSpec,
    McpConfigError,
    McpError,
    McpHttpServerSpec,
    McpServerSpec,
)

# --- Public error surface (typed / coded) -------------------------------------
# Boundary code (the product's HTTP backend, which reaches the engine only
# through noeta.sdk) matches these STRUCTURALLY — ``isinstance(exc, CodedError)``
# + ``exc.code`` — instead of the class-name / message-substring matching it
# used before. ``CodedError`` is the stable base carrying the ``code`` token;
# each concrete client-facing engine error sets its own ``code``.
from noeta.execution import (
    ModelSelectorError,
    NotResumableError,
    ProviderSelectorError,
    TaskAlreadyTerminalError,
    UnsupportedSubtaskSuspend,
)
from noeta.protocols.errors import CodedError


__all__ = [
    # recipe
    "Options",
    "AgentDefinition",
    "SystemPromptPreset",
    "compile_options",
    "register_preset_prompt",
    # client verbs
    "Client",
    "query",
    "QueryResult",
    # host-level wiring (D3)
    "HostConfig",
    "AppPreviewGateway",
    "AppMount",
    "McpAnyServerSpec",
    "McpServerSpec",
    "McpHttpServerSpec",
    "McpConfigError",
    "McpError",
    "HttpPostFn",
    # public error surface (typed / coded)
    "CodedError",
    "QueryFailedError",
    "ModelSelectorError",
    "ProviderSelectorError",
    "NotResumableError",
    "UnsupportedSubtaskSuspend",
    "TaskAlreadyTerminalError",
    # capability projections (composer enums + per-model vision gate)
    "permission_modes",
    "effort_modes",
    "model_capabilities",
    # message projection + wire
    "as_messages",
    "envelope_to_dict",
    "AssistantMessage",
    "UserMessage",
    "ToolUse",
    "ToolResultView",
    "Result",
    # content blocks + ref (image-input write side: put_content → ImageBlock)
    "ImageBlock",
    "ContentRef",
    # authoring
    "tool",
    "DecoratedTool",
    "create_sdk_mcp_server",
    "SdkMcpServer",
    # extension interfaces (implement → mount via Options)
    "Tool",
    "ToolContext",
    "ToolResult",
    "LLMProvider",
    "Policy",
    "View",
    "Decision",
    "StepContext",
    "Guard",
    "GuardContext",
    "ProposedAction",
    "VerdictResult",
    "Observer",
    "ContentKindSpec",
    # official factory content
    "presets",
]
