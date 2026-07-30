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
from noeta.agent.spec import BudgetSpec
from noeta.client.capabilities import (
    effort_modes,
    model_capabilities,
    permission_modes,
)
from noeta.client.client import Client, QueryFailedError, QueryResult, query
from noeta.client.consolidation import (
    build_consolidation_digest,
    consolidation_due,
    run_consolidation,
)
from noeta.client.host_config import HostConfig, SandboxExecEnvConfig
# The factory seam types behind ``HostConfig.sandbox_backend_factory`` /
# ``sandbox_browser_factory`` — exported so a product can annotate its injected
# factories without importing ``noeta.client`` internals.
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
    Result,
    ToolResultView,
    ToolUse,
    UserMessage,
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
# Plugin trust store + shared error surface (the primitives the manifest
# mechanism stands on; the retired 0.4.0 contribution-bundle path is gone).
from noeta.client.plugins import (
    PluginError,
    UntrustedPluginDirWarning,
    grant_trust,
    is_trusted,
)
# Manifest-plugin mechanism (SDK-extensibility redesign): the surface registry,
# the static manifest reader, the five-source loader / PluginSet, and the
# activation contributions type. ``load_plugin_set`` is the manifest-based
# loader a host binds to.
from noeta.client.plugin_manifest import (
    ManifestContribution,
    PluginBuilder,
    PluginManifest,
)
from noeta.client.plugin_set import PluginSet
from noeta.client.plugin_set import load_plugins as load_plugin_set
from noeta.client.surfaces import (
    SurfaceRegistry,
    SurfaceSpec,
    standard_registry,
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
# Sandbox execution extension surface — ONLY the ``ExecEnv`` / ``BrowserBackend``
# seam protocols (the types ``HostConfig.sandbox_backend_factory`` /
# ``sandbox_browser_factory`` are written against). The concrete AIO adapters
# (``AioSandboxExecEnv`` / ``AioBrowserBackend``) are deliberately NOT public:
# they are implementation detail slated for retirement — since microkernel M2
# they live in the ``sandbox`` built-in plugin
# (``noeta.builtins.sandbox.impl``) and the SDK's sandbox manager resolves
# them through the loader's dynamic-import doorway (see the
# execution-environment-seam ADR, "SDK-adapter export surface").
from noeta.runtime.workspace import path_within
from noeta.runtime.exec_env import ExecEnv
# ``BrowserBackend`` is re-exported LAZILY via the module ``__getattr__``
# below: since microkernel phase 3 the Protocol lives in the ``browser``
# built-in plugin, and nothing statically imports ``noeta.builtins``.
from noeta.protocols.event_log import Subscriber as Observer
from noeta.protocols.hooks import (
    Guard,
    GuardContext,
    ProposedAction,
    # The ``ProposedAction`` members: a Guard dispatches on them
    # (``isinstance(action, ProposedToolCall)``), so exporting only the union
    # would leave every guard author — plugins included — reaching into
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
# The file-per-memory store behind the memory tools (``MemoryStore``) is
# re-exported LAZILY via the module ``__getattr__`` below: since microkernel
# M3 it lives in the ``memory`` built-in plugin, and nothing statically
# imports ``noeta.builtins`` — the loader's dynamic doorway is the only path.
from noeta.protocols.wake import NEXT_GOAL_WAKE_HANDLE
from noeta.protocols.step_context import StepContext
from noeta.protocols.tool import Tool, ToolContext, ToolResult
from noeta.protocols.view import View

# --- Host-level wiring (D3) ---------------------------------------------------
# The host-config surface: durable storage + host runtime injections (preview
# gateway, live-MCP resolver). Separate from Options (which carries agent
# identity); a product backend passes a populated HostConfig to opt into durable
# storage / preview / MCP while still driving the engine only through noeta.sdk.
# ``AppMount`` / ``AppPreviewGateway`` are re-exported LAZILY via the module
# ``__getattr__`` below: since microkernel phase 3 the seam types live in the
# ``app`` built-in plugin.
from noeta.runtime.mcp import (
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
    # recipe advanced field (Options.budget). The compiled AgentSpec identity
    # carries activation as the ``plugins`` tuple + ``spawnable`` (D6); the
    # authoring path is ``Options.plugins``.
    "BudgetSpec",
    # client verbs
    "Client",
    "query",
    "QueryResult",
    # memory consolidation (memory v2 phase 3 — the host-callable entry +
    # the guard/digest halves for hosts that orchestrate their own runs)
    "run_consolidation",
    "consolidation_due",
    "build_consolidation_digest",
    # the typed chat turn-boundary marker (a product's session-stop seam
    # recognizes the trailing next-goal suspend by this wake handle)
    "NEXT_GOAL_WAKE_HANDLE",
    # host-level wiring (D3)
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
    # sandbox execution extension surface (seam protocols + factory types only;
    # the concrete AIO adapters are runtime-internal — see the seam ADR)
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
    # The containment predicate the fs write fence uses, published so a host
    # deciding what to put in HostConfig.write_roots asks the question exactly
    # the way the fence will answer it (component-wise, never string-prefix).
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
    # memory store (host-side memory pool management)
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
    # plugin trust store + error surface (shared by the manifest mechanism)
    "PluginError",
    "UntrustedPluginDirWarning",
    "grant_trust",
    "is_trusted",
    # manifest-plugin mechanism (SDK-extensibility redesign, M1 + M2)
    "SurfaceSpec",
    "SurfaceRegistry",
    "standard_registry",
    "PluginManifest",
    "ManifestContribution",
    "PluginBuilder",
    "PluginSet",
    "load_plugin_set",
    "PluginActivation",
    "DEFAULT_PLUGINS",
    # official factory content
    "presets",
]


def __getattr__(name: str) -> object:
    # Lazy public re-exports whose implementations live in built-in plugins
    # (microkernel M3): resolved through the loader's dynamic-import doorway
    # on first access, keeping the universal "nothing statically imports
    # noeta.builtins" rule intact. ``MemoryStore`` — the file-per-memory store
    # behind the memory tools; a host that manages memory pools (listing,
    # editing, per-space scoping) opens the same store the agent's memory
    # tools write, so both sides agree on slugs and frontmatter.
    if name == "MemoryStore":
        import importlib

        return importlib.import_module("noeta.builtins.memory.impl").MemoryStore
    # The capability-seam types (microkernel phase 3): ``BrowserBackend``
    # lives in the browser plugin (the sandbox plugin implements it), and
    # ``AppMount`` / ``AppPreviewGateway`` live in the app plugin (a product
    # host's concrete gateway satisfies the Protocol structurally). Exported
    # here for implementers' typing convenience only — the kernel and SDK
    # core treat both as opaque objects in the builder's ``backends`` bag.
    if name == "BrowserBackend":
        import importlib

        return importlib.import_module(
            "noeta.builtins.browser.impl"
        ).BrowserBackend
    if name in ("AppMount", "AppPreviewGateway"):
        import importlib

        return getattr(importlib.import_module("noeta.builtins.app.impl"), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
