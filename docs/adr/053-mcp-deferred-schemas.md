# ADR-053: Deferred schemas

- Status: accepted
- Date: 2026-09-25

## Context

Refines [ADR-012](012-mcp-connectors.md) (Noeta is an MCP client over a request/response subset; credentials stay host-side and a resume rebuilds the tool set from the recording). Where the two disagree, this record wins.

## Decision

This reverses the rejection, in [ADR-012](012-mcp-connectors.md)'s alternative 4, of a lazy tool-search layer, in a shape
that leaves the composer's contract and the fixed tool set intact.

- **What changed.** `McpServerSpec` / `McpHttpServerSpec` take `deferred: bool =
  False`. A deferred server's tools are still built as ordinary `McpTool`s —
  registered, guarded, audited and approved under their real
  `mcp__{alias}__{tool}` names, `tool_subset` still applied — but carry
  `advertised = False`, an optional tool attribute the composer honours by
  leaving the schema out of `provider_tool_schemas`. Whenever at least one
  enabled spec is deferred, the build adds two small tools once for all of them:
  `ToolSearch` (a query matched on name and description words, or an exact
  name, returns up to five tools with their full input schema; an empty query
  lists every deferred tool with a one-line description) and `McpCall` (`tool`,
  `arguments`). The pair is added even when the deferred server was skipped
  this turn, so an outage does not move the stable prefix.
- **How a call reaches the real tool.** `McpCall` is a plain tool carrying an
  optional `route_call(arguments) -> (tool_name, arguments)` attribute. The
  react policy applies it to the `ToolCallsDecision` it is about to return,
  whichever translate built it, rewriting the call into a `ToolCall` on the real
  name with the same `call_id` — so the Guard, `can_use_tool`, the audit
  observer and the ToolRuntime see the real tool, and a permission rule or
  `require_approval_tools` entry naming it applies unchanged. The recorded
  assistant message keeps the `McpCall` tool_use the provider saw, and the
  result pairs back by `call_id`. An unknown or non-deferred name, or a missing
  `arguments` object, is answered as an error for that call alone; the rest of
  the batch runs. `McpCall.invoke` itself refuses to run anything: a policy that
  does not route would otherwise slip the real call past the Guard.
- **Why a routed plain tool, not a control tool.** A control tool is contributed
  through the control-tool mount loop, but the MCP tools arrive as a finished
  tool dict at the kernel's MCP band; mounting `McpCall` there would mean new
  builder plumbing and a translate that must merge its rewritten calls with
  every other control tool's outcome. Two optional attributes, read with
  `getattr`, keep the kernel ignorant of MCP and need no new mechanism.
- **The trade-off.** The first use of a deferred tool costs one extra round trip
  to fetch its schema; in exchange the stable prefix carries two schemas
  (≈1.2 KB) instead of N (≈26 KB for a 40-tool server). The tool set stays
  fixed for the turn, so the prompt cache is unaffected; a non-deferred server
  is byte-identical to before.
