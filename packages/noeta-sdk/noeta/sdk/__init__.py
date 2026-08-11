"""``noeta.sdk`` — the one public import surface for the Noeta SDK.

Library users import everything from here (``from noeta.sdk import query, Client,
Options, tool``), run an agent in-process, and never touch engine internals. This
module is **re-export only**, no logic, so every public type has exactly one
identity no matter which internal module defines it.
"""

from __future__ import annotations

from noeta import presets
from noeta.agent.spec import BudgetSpec
from noeta.client.capabilities import (
    effort_modes,
    model_capabilities,
    permission_modes,
)
from noeta.client.client import (
    DEFAULT_MODEL_ALLOWLIST,
    Client,
    DeleteTaskResult,
    QueryFailedError,
    QueryResult,
    TaskStatus,
    query,
)
# The types the ``Client`` verbs return and raise. A host writes functions that
# take and return them, so without these it cannot annotate its own signatures
# without reaching into ``noeta.execution``.
from noeta.execution.driver import DriveOutcome, NotForkableError, SeededTurn
from noeta.client.consolidation import (
    build_consolidation_digest,
    consolidation_due,
    run_consolidation,
)
from noeta.client.host_config import HostConfig, SandboxExecEnvConfig
# The factory seam types behind ``HostConfig.sandbox_backend_factory`` /
# ``sandbox_browser_factory``, so a host can annotate its injected factories
# without reaching into ``noeta.client``.
from noeta.client.sandbox import BackendFactory, BoundPreamble, BrowserBackendFactory
from noeta.client.sandbox_provider import (
    MountSpec,
    SandboxAuth,
    SandboxHandle,
    SandboxProvider,
    SandboxSpec,
    StaticApiKeyAuth,
    decode_exec_env_ref,
    encode_exec_env_ref,
)
from noeta.client.otlp import OtlpTraceConfig
from noeta.client.messages import (
    AssistantMessage,
    InjectedMessage,
    Result,
    ToolResultView,
    ToolUse,
    UserMessage,
    ViewItem,
    as_messages,
)
from noeta.client.options import (
    DEFAULT_PLUGINS,
    AgentDefinition,
    Options,
    PluginActivation,
    SystemPromptPreset,
    compile_options,
    register_preset_prompt,
)
from noeta.client.plugins import (
    PluginError,
    PluginVersionWarning,
    UnnamedPluginFileWarning,
    UntrustedPluginDirWarning,
    grant_trust,
    is_trusted,
)
from noeta.client.plugin_manifest import (
    ManifestContribution,
    PluginBuilder,
    PluginManifest,
)
from noeta.client.plugin_set import PluginSet
from noeta.client.plugin_set import load_plugins
from noeta.client.surfaces import (
    SurfaceRegistry,
    SurfaceSpec,
    standard_registry,
)
from noeta.client.wire import envelope_to_dict

# The recorded reminder-provider seam. A plugin contributing to the
# ``reminder_provider`` surface returns ``Reminder`` s from a ``RecallView``,
# so both types plus the seam name belong on the public surface — without them
# a host could declare the contribution but not write its value.
from noeta.execution.reminders import (
    TURN_INTAKE,
    RecallView,
    Reminder,
    ReminderProvider,
)
from noeta.sdk.authoring import (
    DecoratedTool,
    SdkMcpServer,
    create_sdk_mcp_server,
    tool,
)

# --- Extension interfaces -----------------------------------------------------
# Users implement these and mount them through the matching ``Options`` field.
from noeta.context.content_channel import ContentKindSpec
# Only the ``ExecEnv`` / ``BrowserBackend`` seam Protocols are public — the types
# ``HostConfig.sandbox_backend_factory`` / ``sandbox_browser_factory`` are written
# against. The concrete adapters stay implementation detail in the ``sandbox``
# built-in, reached through the loader's dynamic-import doorway.
from noeta.runtime.workspace import path_within
from noeta.runtime.exec_env import ExecEnv
# ``BrowserBackend`` is re-exported LAZILY via the module ``__getattr__`` below:
# its Protocol lives in the ``browser`` built-in, and nothing statically imports
# ``noeta.builtins``.
# The read surface's own types: what ``subscribe`` hands a callback, what
# ``task_streams`` returns, and the suspend payload a host reads the
# ``waiting_human`` / ``interrupted`` / ``turn_failed: …`` tag off.
from noeta.protocols.events import (
    SUSPEND_REASON_INTERRUPTED,
    SUSPEND_REASON_TURN_FAILED,
    SUSPEND_REASON_WAITING_HUMAN,
    EventEnvelope,
    SuspendReason,
    TaskSuspendedPayload,
    parse_suspend_reason,
)
from noeta.protocols.event_log import TaskStreamSummary
from noeta.protocols.event_log import Subscriber as Observer
from noeta.protocols.hooks import (
    Guard,
    GuardContext,
    ProposedAction,
    # The union's members too: a Guard dispatches on them
    # (``isinstance(action, ProposedToolCall)``), so exporting only
    # ``ProposedAction`` would force every guard author to reach into
    # ``noeta.protocols.hooks``.
    ProposedFinish,
    ProposedSpawnSubtask,
    ProposedToolCall,
    VerdictResult,
)
from noeta.protocols.decisions import Decision
from noeta.protocols.messages import (
    ImageBlock,
    LLMProvider,
    LLMRequest,
    LLMResponse,
    Message,
    StreamDelta,
    StreamingProvider,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
)
from noeta.protocols.policy import Policy
from noeta.protocols.values import ContentRef
# ``MemoryStore`` is re-exported LAZILY via the module ``__getattr__`` below:
# it lives in the ``memory`` built-in, and nothing statically imports
# ``noeta.builtins``.
from noeta.protocols.wake import NEXT_GOAL_WAKE_HANDLE
from noeta.protocols.step_context import StepContext
from noeta.protocols.tool import (
    FileReadRegistry,
    Tool,
    ToolContext,
    ToolResult,
)
from noeta.protocols.view import View

# --- Host-level wiring --------------------------------------------------------
# ``HostConfig`` is deliberately separate from ``Options``: Options carries agent
# identity, HostConfig carries the deployment's injections (durable storage,
# preview gateway, live-MCP resolver), so a host opts into them without touching
# any compiled spec. ``AppMount`` / ``AppPreviewGateway`` are re-exported LAZILY
# via the module ``__getattr__`` below — those seam types live in the ``app``
# built-in.
from noeta.runtime.mcp import (
    HttpPostFn,
    McpAnyServerSpec,
    McpConfigError,
    McpError,
    McpHttpServerSpec,
    McpServerSpec,
)

# --- Public error surface (typed / coded) -------------------------------------
# ``CodedError`` is the stable base carrying the ``code`` token, and every
# client-facing engine error sets its own. Boundary code matches STRUCTURALLY —
# ``isinstance(exc, CodedError)`` + ``exc.code`` — so a host never has to pin
# class names or message substrings.
from noeta.execution import (
    ModelSelectorError,
    NotResumableError,
    ProviderSelectorError,
    TaskAlreadyTerminalError,
    UnknownTaskError,
    UnsupportedSubtaskSuspend,
)
from noeta.protocols.errors import CodedError


__all__ = [
    "Options",
    "AgentDefinition",
    "SystemPromptPreset",
    "compile_options",
    "register_preset_prompt",
    "BudgetSpec",
    "Client",
    "query",
    "QueryResult",
    # what the Client verbs hand back: every command returns a DriveOutcome,
    # every seed_* a SeededTurn, delete_task a DeleteTaskResult
    "DriveOutcome",
    "SeededTurn",
    "DeleteTaskResult",
    # the read-only twin of a DriveOutcome: where a task rests, without driving
    "TaskStatus",
    # the deployment allowlist applied when HostConfig sets no allowed_models
    "DEFAULT_MODEL_ALLOWLIST",
    # memory consolidation: the host-callable entry, plus the guard / digest
    # halves for hosts that orchestrate their own runs
    "run_consolidation",
    "consolidation_due",
    "build_consolidation_digest",
    # the typed chat turn-boundary marker — a host's session-stop seam
    # recognizes the trailing next-goal suspend by this wake handle
    "NEXT_GOAL_WAKE_HANDLE",
    "HostConfig",
    "SandboxExecEnvConfig",
    "SandboxProvider",
    "SandboxSpec",
    "SandboxHandle",
    "SandboxAuth",
    "StaticApiKeyAuth",
    "MountSpec",
    "encode_exec_env_ref",
    "decode_exec_env_ref",
    # sandbox execution extension surface: seam protocols + factory types only,
    # the concrete adapters stay internal
    "ExecEnv",
    "BrowserBackend",
    "BackendFactory",
    "BrowserBackendFactory",
    "BoundPreamble",
    "OtlpTraceConfig",
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
    "UnknownTaskError",
    "NotForkableError",
    "permission_modes",
    "effort_modes",
    "model_capabilities",
    "as_messages",
    "envelope_to_dict",
    # the read surface's own types: the envelope a subscriber / replay reads,
    # the task_streams row, and the suspend payload carrying the reason tag
    "EventEnvelope",
    "TaskStreamSummary",
    "TaskSuspendedPayload",
    # the parsed (kind, detail) view of a TaskSuspended.reason tag —
    # suspend_reason returns it, parse_suspend_reason produces it from the raw
    # tag a subscriber reads off the payload
    "SuspendReason",
    "parse_suspend_reason",
    # the three reason kinds a host renders differently, compared with ==
    # against SuspendReason.kind
    "SUSPEND_REASON_WAITING_HUMAN",
    "SUSPEND_REASON_INTERRUPTED",
    "SUSPEND_REASON_TURN_FAILED",
    "AssistantMessage",
    "UserMessage",
    # a host-injected user-channel turn (origin "system" / "memory") — kept
    # out of UserMessage so an isinstance filter never mistakes it for the human
    "InjectedMessage",
    "ToolUse",
    "ToolResultView",
    "Result",
    # the union as_messages yields — a host annotating its own projection
    "ViewItem",
    "ImageBlock",
    "ContentRef",
    "tool",
    "DecoratedTool",
    "create_sdk_mcp_server",
    "SdkMcpServer",
    "Tool",
    "FileReadRegistry",
    "ToolContext",
    "ToolResult",
    # The containment predicate the fs write fence uses, published so a host
    # deciding what to put in HostConfig.write_roots asks the question exactly
    # the way the fence answers it (component-wise, never string-prefix).
    "path_within",
    "LLMProvider",
    "StreamingProvider",
    "StreamDelta",
    # provider implementation material (the request/response/message types an
    # LLMProvider implementation consumes and produces)
    "LLMRequest",
    "LLMResponse",
    "Message",
    "TextBlock",
    "ToolUseBlock",
    "ToolResultBlock",
    "Usage",
    "MemoryStore",
    "Policy",
    "View",
    "Decision",
    "StepContext",
    "Guard",
    "GuardContext",
    "ProposedAction",
    "ProposedToolCall",
    "ProposedSpawnSubtask",
    "ProposedFinish",
    "VerdictResult",
    "Observer",
    "ContentKindSpec",
    "PluginError",
    "PluginVersionWarning",
    "UnnamedPluginFileWarning",
    "UntrustedPluginDirWarning",
    "grant_trust",
    "is_trusted",
    "SurfaceSpec",
    "SurfaceRegistry",
    "standard_registry",
    "PluginManifest",
    "ManifestContribution",
    "PluginBuilder",
    "PluginSet",
    "load_plugins",
    "Reminder",
    "RecallView",
    "ReminderProvider",
    "TURN_INTAKE",
    "PluginActivation",
    "DEFAULT_PLUGINS",
    "presets",
]


def __getattr__(name: str) -> object:
    # Public names whose implementations live in built-in plugins, imported on
    # first access so the "nothing statically imports noeta.builtins" rule holds
    # here too. ``MemoryStore`` is the file-per-memory store behind the memory
    # tools: a host managing memory pools opens the very store the agent writes,
    # so both sides agree on slugs and frontmatter.
    if name == "MemoryStore":
        import importlib

        return importlib.import_module("noeta.builtins.memory.impl").MemoryStore
    # ``BrowserBackend`` (implemented by the sandbox built-in) and ``AppMount``
    # / ``AppPreviewGateway`` (satisfied structurally by a host's own gateway)
    # are exported for implementers' typing convenience only — the kernel and
    # SDK core treat both as opaque objects in the builder's ``backends`` bag.
    if name == "BrowserBackend":
        import importlib

        return importlib.import_module(
            "noeta.builtins.browser.impl"
        ).BrowserBackend
    if name in ("AppMount", "AppPreviewGateway"):
        import importlib

        return getattr(importlib.import_module("noeta.builtins.app.impl"), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
