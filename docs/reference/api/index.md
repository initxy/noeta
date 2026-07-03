# API Reference

Auto-generated API documentation for `noeta.sdk` — the one public import
surface. Library users import everything from here:

```python
from noeta.sdk import query, Client, Options, tool
```

and never touch `noeta-runtime` internals or `noeta.client` directly.

## Client

The main entry point for running agents in-process.

::: noeta.sdk.Client
    options:
      members:
        - __init__
        - query
        - close

::: noeta.sdk.query

::: noeta.sdk.QueryResult

::: noeta.sdk.QueryFailedError

## Options & Agent Definition

Declarative agent configuration.

::: noeta.sdk.Options

::: noeta.sdk.AgentDefinition

::: noeta.sdk.SystemPromptPreset

::: noeta.sdk.compile_options

## Host Configuration

Host-level wiring: durable storage, preview gateway, MCP resolver.

::: noeta.sdk.HostConfig

## Messages & Wire

Message projection and serialization helpers.

::: noeta.sdk.as_messages

::: noeta.sdk.UserMessage

::: noeta.sdk.AssistantMessage

::: noeta.sdk.ToolUse

::: noeta.sdk.ToolResultView

::: noeta.sdk.Result

::: noeta.sdk.envelope_to_dict

## Authoring API

Decorators and helpers for defining tools and MCP servers.

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

::: noeta.sdk.Policy

::: noeta.sdk.View

::: noeta.sdk.Decision

::: noeta.sdk.StepContext

## Guards & Observers

Synchronous approval hooks and async event subscribers.

::: noeta.sdk.Guard

::: noeta.sdk.GuardContext

::: noeta.sdk.ProposedAction

::: noeta.sdk.VerdictResult

::: noeta.sdk.Observer

## Content Channel

Register new content kinds for the semi-stable context segment.

::: noeta.sdk.ContentKindSpec

## Errors

Typed, coded error surface for boundary code.

::: noeta.sdk.CodedError

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
