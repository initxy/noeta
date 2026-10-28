# MCP server tools (generic shape)

Claude Code version: 2.1.312 (baseline session `m1`). Example: the `echo` test server's `echo`
tool (`~/cc-recordings/2.1.312/tools/mcp_echo.py`), offered as `mcp__echo__echo`.

| source | sha256 (normalized) |
|---|---|
| `m1/0060 tools[mcp__echo__echo].description` | `3dff49e6ed2ce7108f7488ace96e90673e862b401008bf20c2392620c6d0fb3d` |
| `m1/0060 tools[mcp__echo__echo].input_schema` | `e3dce8d1afb8d03604af3bc6729219a5cd7df05558807b6e44e391d56fdf75d9` |

## Purpose and placement

How Claude Code presents a tool that an MCP server advertises. noeta equivalent:
`noeta/plugins/mcp/` (`extension.py`, `config.py`, `tools.py`, `results.py`).

## Rules

| id | rule | noeta today |
|---|---|---|
| `tool.mcp.1` | The tool name is `mcp__<server>__<tool>`, `<server>` being the configured server name. | covered |
| `tool.mcp.2` | Every MCP server tool is **deferred by default**: its name appears in the environment message's deferred-tool list and it is not in `tools` until loaded with `ToolSearch` (`select:` form). | contradicts: noeta servers are advertised in full unless configured `deferred` (default false) |
| `tool.mcp.3` | Once loaded, the tool is sent with `defer_loading: true` and placed after all built-in tools, after `DeferredToolPlaceholder` (MCP tools come last). | contradicts: noeta has no `defer_loading` flag; deferred tools are reached through `McpCall` instead of being added to `tools` |
| `tool.mcp.4` | A loaded tool stays in `tools` for the rest of the conversation, including after compaction and after `--continue`. | missing (no loading step) |
| `tool.mcp.5` | After loading, the model calls the tool directly by its `mcp__...` name, with no wrapper tool. | contradicts: a deferred noeta tool is called through `McpCall` |
| `tool.mcp.6` | The description is the server's own text, unchanged, with no prefix (`Echo text back.`). | contradicts: noeta prefixes `[MCP server '<alias>']` and caps it at 1024 bytes |
| `tool.mcp.7` | The input schema is the server's `inputSchema` unchanged: no `$schema`, no added `additionalProperties`, no added descriptions. | covered (passed through; noeta substitutes an open object only when the server sends none) |
| `tool.mcp.8` | MCP resources are reached through the two deferred tools `ListMcpResourcesTool` and `ReadMcpResourceTool`, offered whenever a server is configured. | missing (see those files) |

## Result shape

| id | case | shape | noeta today |
|---|---|---|---|
| `tool.mcp.r.1` | text result | the server's `content` array passed through as the `tool_result` content blocks: `[{"type":"text","text":"echo: ping"}]`; no wrapper text, no server label | contradicts: noeta joins the text blocks and returns one JSON object string `{"text": "..."}` (plus structured-content fields) |

## Variants

`s1` sends the same name, description and schema.
