# DeferredToolPlaceholder

Claude Code 2.1.312 · baseline `m1` · rules in our words (see `../README.md`).

| source | sha256 |
|---|---|
| `m1/0001 tools[DeferredToolPlaceholder].description` | `97f0a3ed59c75538669383689dabea6e1def347a07e0d5d2f2819e13a0fa9c70` |
| `m1/0001 tools[DeferredToolPlaceholder].input_schema` | `efddc7bd8bbcef73a14eb1ace1ffdaec81e518ef1e13c1e9271d0b8acb694a49` |

## Purpose

A dummy tool with `defer_loading: true` that keeps the API's deferred-tool mechanism switched
on even before any real deferred tool has been loaded. Present in every main-agent and
sub-agent request of every session. noeta sends it whenever a task offers a deferred tool (`noeta/plugins/tool_search`).

## Parameters

Schema is `{"type": "object", "properties": {}}` — no `$schema`, no `additionalProperties`,
nothing required (unlike every other baseline tool).

## Rules

| id | rule | noeta today |
|---|---|---|
| `tool.DeferredToolPlaceholder.1` | The tool is always sent, with `defer_loading: true`. | covered |
| `tool.DeferredToolPlaceholder.2` | Its description says it is reserved, exists only to keep deferred loading active, and must never be called. | covered |
| `tool.DeferredToolPlaceholder.3` | Position: always second to last, inserted right before the last tool of the otherwise sorted list (built-ins by name, then MCP tools): between `Workflow` and `Write` while no MCP tool is sent, even with a deferred built-in loaded (m1/0001, m5 `monitor` and `taskstop`); right before the last MCP tool once MCP tools are sent (`Write, DeferredToolPlaceholder, mcp__echo__echo` in m1/0019 onward and m4/0001); just before `SubagentHandback` in sub-agents. | covered (`noeta/plugins/tool_search` `ToolSearch.tool_changes` puts it before the last tool of the kernel's order, `noeta/core/loop/request.py` `request_tools`; tools that declare `listed_last` form the trailing group; the replay compares each request's tool order) |
| `tool.DeferredToolPlaceholder.4` | Every tool fetched through `ToolSearch` is likewise sent with `defer_loading: true`; always-loaded tools carry no such field. | covered |

## Result shape

| id | shape (observed) | noeta today |
|---|---|---|
| `tool.DeferredToolPlaceholder.r.1` | Never called; no result. | covered (an entry, not a tool: a call to it is refused as an unknown tool) |

## Variants

Same hashes in every session.
