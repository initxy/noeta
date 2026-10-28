# DeferredToolPlaceholder

Claude Code 2.1.312 · baseline `m1` · rules in our words (see `../README.md`).

| source | sha256 |
|---|---|
| `m1/0001 tools[DeferredToolPlaceholder].description` | `97f0a3ed59c75538669383689dabea6e1def347a07e0d5d2f2819e13a0fa9c70` |
| `m1/0001 tools[DeferredToolPlaceholder].input_schema` | `efddc7bd8bbcef73a14eb1ace1ffdaec81e518ef1e13c1e9271d0b8acb694a49` |

## Purpose

A dummy tool with `defer_loading: true` that keeps the API's deferred-tool mechanism switched
on even before any real deferred tool has been loaded. Present in every main-agent and
sub-agent request of every session. noeta has nothing like it (noeta does not use
`defer_loading`; deferred MCP tools are reached through `ToolSearch` + `McpCall`).

## Parameters

Schema is `{"type": "object", "properties": {}}` — no `$schema`, no `additionalProperties`,
nothing required (unlike every other baseline tool).

## Rules

| id | rule | noeta today |
|---|---|---|
| `tool.DeferredToolPlaceholder.1` | The tool is always sent, with `defer_loading: true`. | missing |
| `tool.DeferredToolPlaceholder.2` | Its description says it is reserved, exists only to keep deferred loading active, and must never be called. | missing |
| `tool.DeferredToolPlaceholder.3` | Position: in the first request of a conversation it sat between `Workflow` and `Write` (m1/0001, m2/0002); once any deferred tool was loaded it moved after all built-ins and before the MCP tools (m1/0019 onward, m4/0001). In sub-agents it sat just before `SubagentHandback`. The list is otherwise sorted by name. | missing |
| `tool.DeferredToolPlaceholder.4` | Every tool fetched through `ToolSearch` is likewise sent with `defer_loading: true`; always-loaded tools carry no such field. | missing |

## Result shape

| id | shape (observed) |
|---|---|
| `tool.DeferredToolPlaceholder.r.1` | Never called; no result. |

## Variants

Same hashes in every session.
