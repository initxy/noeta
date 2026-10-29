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
| `tool.mcp.2` | Every MCP server tool is **deferred by default**: its name appears in the environment message's deferred-tool list and it is not in `tools` until loaded with `ToolSearch` (`select:` form). | covered |
| `tool.mcp.3` | Once loaded, the tool is sent with `defer_loading: true` and placed after all built-in tools, after `DeferredToolPlaceholder` (MCP tools come last). | covered |
| `tool.mcp.4` | A loaded tool stays in `tools` for the rest of the conversation, including after compaction and after `--continue`. | covered |
| `tool.mcp.5` | After loading, the model calls the tool directly by its `mcp__...` name, with no wrapper tool. | covered |
| `tool.mcp.6` | The description is the server's own text, unchanged, with no prefix (`Echo text back.`). | covered (capped at 1024 bytes) |
| `tool.mcp.7` | The input schema is the server's `inputSchema` unchanged: no `$schema`, no added `additionalProperties`, no added descriptions. | covered (passed through; noeta substitutes an open object only when the server sends none) |
| `tool.mcp.8` | MCP resources are reached through the three deferred tools `ListMcpResourcesTool`, `ReadMcpResourceTool` and `ReadMcpResourceDirTool`, offered whenever a server is configured. | covered (to the main agent; a sub-agent gets the servers' tools only, as in Claude Code) |
| `tool.mcp.9` | A remote (`http` / `sse`) server that needs the user to sign in offers, in place of its own tools, `mcp__<server>__authenticate` (no parameters): it starts an OAuth authorization-code sign-in and returns an authorization URL for the model to give the user; a sign-in started in the last 3 minutes is handed out again. Once the user has authorized, the server's tools replace the two. (bundle, not recorded) | covered |
| `tool.mcp.10` | `mcp__<server>__complete_authentication` takes `callback_url` (string, required): the full redirect URL from the browser's address bar, for when the `http://localhost:<port>/callback` page cannot load; a URL without `code`/`error`, or with another sign-in's `state`, is refused. (bundle, not recorded) | covered |
| `tool.mcp.11` | `ReadMcpResourceDirTool` is offered: it is in the recorded deferred-tool lists (`m1` to `m5`; `main.env.7`). | covered |

## Result shape

| id | case | shape | noeta today |
|---|---|---|---|
| `tool.mcp.r.2` | over the output budget (25,000 tokens, `MAX_MCP_OUTPUT_TOKENS`) | a result without images is saved to a file and replaced by one text: its size in characters (and lines), the file's path, its format (plain text, or JSON for structured content), how to read it in parts or query it, and to read all of it before summarizing; with images, or when it cannot be saved, the text is cut to the budget, images that do not fit are left out with a note, and a closing note says the output was truncated and to page or filter on the server (2.1.312 bundle) | covered (the budget is estimated at four characters a token; Claude Code asks the API for an exact count between half and four times the budget) |
| `tool.mcp.r.1` | text result | the server's `content` array passed through as the `tool_result` content blocks: `[{"type":"text","text":"echo: ping"}]`; no wrapper text, no server label | covered |

## Variants

`s1` sends the same name, description and schema.
