# ListMcpResourcesTool

Claude Code version: 2.1.312 (baseline session `m1`).

| source | sha256 (normalized) |
|---|---|
| `m1/0060 tools[ListMcpResourcesTool].description` | `a51a237f35f704e3db20c7bf38df5bfc768071563f68ac5c0134a8de157ff631` |
| `m1/0060 tools[ListMcpResourcesTool].input_schema` | `970f025bfa7a4a000e0224291b1aa26003a8ad3a5c632f6d95fdb0ac6559d8a9` |

## Purpose and placement

Lists resources offered by the configured MCP servers. **Deferred**: its name is in the
environment message's deferred-tool list whenever an MCP server is configured; the model loads
it with `ToolSearch` (`select:mcp__echo__echo,ListMcpResourcesTool,ReadMcpResourceTool` in
`m1/0019`). Once loaded it is sent with `defer_loading: true`, alphabetically between
`ListAgents` and `Read`, and stays loaded across compaction and `--continue`. Not called in any
recording, so no result shape was observed.

noeta equivalent: none. noeta turns MCP resources into `@` mentions
(`noeta/plugins/mcp/prompts.py`) and offers no resource tool.

## Parameters

JSON Schema 2020-12, `additionalProperties: false`; no `required` list.

| id | name | type | required | enum / default / limits | noeta today |
|---|---|---|---|---|---|
| `tool.ListMcpResourcesTool.p.server` | `server` | string | no | name of one server to list; omitted means every server | missing |

## Rules

| id | rule | noeta today |
|---|---|---|
| `tool.ListMcpResourcesTool.1` | Lists the resources available from the configured MCP servers. | missing |
| `tool.ListMcpResourcesTool.2` | Each listed resource carries all standard MCP resource fields plus a `server` field naming its server. | missing |
| `tool.ListMcpResourcesTool.3` | Without `server`, resources from all servers are returned. | missing |
| `tool.ListMcpResourcesTool.4` | The description restates the parameter in a `Parameters:` list (marked optional) and starts and ends with a blank line. | missing |

## Result shape

Not observed (never called in the recordings).

## Variants

`s1` did not load this tool (it loaded only `ReadMcpResourceTool`); otherwise identical.
