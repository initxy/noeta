# ReadMcpResourceTool

Claude Code version: 2.1.312 (baseline session `m1`).

| source | sha256 (normalized) |
|---|---|
| `m1/0060 tools[ReadMcpResourceTool].description` | `96f6ab93df09239e4daa284ac8d73ff99d3c72215e5a44b51a1d223d99e72b4c` |
| `m1/0060 tools[ReadMcpResourceTool].input_schema` | `4c12bb8a2a9210a86d00ce10d5b9210e4f382ee28ebab13ec37f7db6ab892f7a` |

## Purpose and placement

Reads one resource from an MCP server by server name and URI. **Deferred** like
`ListMcpResourcesTool`: named in the deferred-tool list, loaded through `ToolSearch` `select:`,
then sent with `defer_loading: true`, alphabetically between `Read` and `ReportFindings`, and kept
for the rest of the conversation.

noeta equivalent: `ReadMcpResourceTool` in `noeta/plugins/mcp/tools.py`.

## Parameters

JSON Schema 2020-12, `additionalProperties: false`.

| id | name | type | required | enum / default / limits | noeta today |
|---|---|---|---|---|---|
| `tool.ReadMcpResourceTool.p.server` | `server` | string | yes | MCP server name (as configured) | covered |
| `tool.ReadMcpResourceTool.p.uri` | `uri` | string | yes | resource URI | covered |

## Rules

| id | rule | noeta today |
|---|---|---|
| `tool.ReadMcpResourceTool.1` | Reads one resource from one MCP server, identified by server name and URI. | covered |
| `tool.ReadMcpResourceTool.2` | The description restates both parameters as required in a `Parameters:` list and starts and ends with a blank line. | covered |
| `tool.ReadMcpResourceTool.3` | Observed: the model may call it in the same assistant message as an MCP server tool (parallel calls, `m1/0021`). | covered |

## Result shape

| id | case | shape | noeta today |
|---|---|---|---|
| `tool.ReadMcpResourceTool.r.1` | text resource | the server's `resources/read` result as one compact JSON string: `{"contents":[{"uri":...,"text":...}]}` (no spaces, fields as the server sent them) | covered |

## Variants

None: `s1` sends the same text and schema.
