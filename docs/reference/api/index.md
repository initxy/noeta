# API Reference

Auto-generated API documentation for `noeta.sdk` — the one public import
surface. Library users import everything from here:

```python
from noeta.sdk import query, Client, Options, tool
```

and never touch `noeta-runtime` internals or `noeta.client` directly. Every
name below appears in `noeta.sdk.__all__`; `MemoryStore`, `BrowserBackend`,
`AppMount` and `AppPreviewGateway` resolve through the module's `__getattr__`
because their implementations live in built-in plugins.

For a hand-written tour of the same surface, see the
[SDK reference](../sdk.md).

## Client

The main entry point for running agents in-process.

::: noeta.sdk.Client

::: noeta.sdk.query

::: noeta.sdk.QueryResult

::: noeta.sdk.NEXT_GOAL_WAKE_HANDLE

## Options & Agent Definition

Declarative agent configuration.

::: noeta.sdk.Options

::: noeta.sdk.AgentDefinition

::: noeta.sdk.SystemPromptPreset

::: noeta.sdk.BudgetSpec

::: noeta.sdk.compile_options

::: noeta.sdk.register_preset_prompt

## Host Configuration

Host-level wiring: durable storage, preview gateway, MCP resolver, tracing.

::: noeta.sdk.HostConfig

::: noeta.sdk.OtlpTraceConfig

::: noeta.sdk.path_within

## Sandbox Execution Environment

Provisioning, attaching and addressing a per-session container.

::: noeta.sdk.SandboxProvider

::: noeta.sdk.SandboxSpec

::: noeta.sdk.SandboxHandle

::: noeta.sdk.SandboxAuth

::: noeta.sdk.StaticApiKeyAuth

::: noeta.sdk.MountSpec

::: noeta.sdk.SandboxExecEnvConfig

::: noeta.sdk.encode_exec_env_ref

::: noeta.sdk.decode_exec_env_ref

::: noeta.sdk.ExecEnv

::: noeta.sdk.BrowserBackend

::: noeta.sdk.BackendFactory

::: noeta.sdk.BrowserBackendFactory

::: noeta.sdk.BoundPreamble

## Messages & Wire

Message projection and serialization helpers.

::: noeta.sdk.as_messages

::: noeta.sdk.UserMessage

::: noeta.sdk.AssistantMessage

::: noeta.sdk.InjectedMessage

::: noeta.sdk.ToolUse

::: noeta.sdk.ToolResultView

::: noeta.sdk.Result

::: noeta.sdk.envelope_to_dict

## Authoring API

Decorators and helpers for defining tools and in-process MCP servers.

::: noeta.sdk.tool

::: noeta.sdk.DecoratedTool

::: noeta.sdk.create_sdk_mcp_server

::: noeta.sdk.SdkMcpServer

## Extension Interfaces

Implement these and mount them through the matching `Options` field.

::: noeta.sdk.Tool

::: noeta.sdk.ToolContext

::: noeta.sdk.ToolResult

::: noeta.sdk.LLMProvider

::: noeta.sdk.StreamingProvider

::: noeta.sdk.StreamDelta

::: noeta.sdk.Policy

::: noeta.sdk.View

::: noeta.sdk.Decision

::: noeta.sdk.StepContext

## Provider Message Types

The request/response material an `LLMProvider` implementation consumes and
produces.

::: noeta.sdk.LLMRequest

::: noeta.sdk.LLMResponse

::: noeta.sdk.Message

::: noeta.sdk.TextBlock

::: noeta.sdk.ToolUseBlock

::: noeta.sdk.ToolResultBlock

::: noeta.sdk.Usage

## Guards & Observers

Synchronous approval hooks and post-commit event subscribers.

::: noeta.sdk.Guard

::: noeta.sdk.GuardContext

::: noeta.sdk.ProposedAction

::: noeta.sdk.ProposedToolCall

::: noeta.sdk.ProposedSpawnSubtask

::: noeta.sdk.ProposedFinish

::: noeta.sdk.VerdictResult

::: noeta.sdk.Observer

## Content Channel

Register new content kinds for the semi-stable context segment.

::: noeta.sdk.ContentKindSpec

## Plugins

The manifest mechanism: surface registry, static manifests, the loader, and the
workspace-directory trust store.

::: noeta.sdk.SurfaceSpec

::: noeta.sdk.SurfaceRegistry

::: noeta.sdk.standard_registry

::: noeta.sdk.PluginManifest

::: noeta.sdk.ManifestContribution

::: noeta.sdk.PluginBuilder

::: noeta.sdk.load_plugins

::: noeta.sdk.PluginSet

::: noeta.sdk.PluginActivation

::: noeta.sdk.DEFAULT_PLUGINS

::: noeta.sdk.grant_trust

::: noeta.sdk.is_trusted

::: noeta.sdk.PluginError

::: noeta.sdk.UntrustedPluginDirWarning

## Memory

::: noeta.sdk.MemoryStore

::: noeta.sdk.run_consolidation

::: noeta.sdk.consolidation_due

::: noeta.sdk.build_consolidation_digest

## Errors

Typed, coded error surface for boundary code.

::: noeta.sdk.CodedError

::: noeta.sdk.QueryFailedError

::: noeta.sdk.ModelSelectorError

::: noeta.sdk.ProviderSelectorError

::: noeta.sdk.NotResumableError

::: noeta.sdk.UnsupportedSubtaskSuspend

::: noeta.sdk.TaskAlreadyTerminalError

## Capability Projections

::: noeta.sdk.permission_modes

::: noeta.sdk.effort_modes

::: noeta.sdk.model_capabilities

## Presets

::: noeta.sdk.presets

## MCP & App

::: noeta.sdk.AppMount

::: noeta.sdk.AppPreviewGateway

::: noeta.sdk.McpAnyServerSpec

::: noeta.sdk.McpServerSpec

::: noeta.sdk.McpHttpServerSpec

::: noeta.sdk.McpConfigError

::: noeta.sdk.McpError

::: noeta.sdk.HttpPostFn

## Content Types

::: noeta.sdk.ImageBlock

::: noeta.sdk.ContentRef
