# ToolSearch

Claude Code 2.1.312 · baseline `m1` · rules in our words (see `../README.md`).

| source | sha256 |
|---|---|
| `m1/0001 tools[ToolSearch].description` | `88033eceb421f1c23c30384179560269736bf801904499b601baecfb9ded5ced` |
| `m1/0001 tools[ToolSearch].input_schema` | `7b6d3804b3cc61f63489216c9ce3f50bdb84b2bc463e8bacd5caa46c20c8ed08` |

## Purpose

Loads the schemas of deferred tools so they become callable. Always loaded; position 10 of 13
in the baseline list; also present for sub-agents. noeta's counterpart is `ToolSearchTool` in
`noeta/core/deferred.py`, added by the runner whenever a task offers a deferred tool.

## Parameters

`additionalProperties: false`; required: `query`, `max_results`.

| id | name | type | required | enum / default / limits | noeta today |
|---|---|---|---|---|---|
| `tool.ToolSearch.p.query` | `query` | string | yes | `select:<name>[,<name>...]` for exact selection, otherwise keywords | covered |
| `tool.ToolSearch.p.max_results` | `max_results` | number | yes (in `required`, yet documented with default 5) | default 5 | covered |

## Rules

| id | rule | noeta today |
|---|---|---|
| `tool.ToolSearch.1` | Its job is to fetch full schemas of deferred tools so they can be called. | covered |
| `tool.ToolSearch.2` | Deferred tools are announced by name only, in a message in the conversation (the `role: "system"` message lists them; see `main-prompt.md`). | covered (`TaskView.deferred_tool_names()` gives the list the environment message names) |
| `tool.ToolSearch.3` | Until fetched a deferred tool has no schema and cannot be invoked. | covered |
| `tool.ToolSearch.4` | The query is matched against the deferred-tool list and the matched tools' complete schemas are returned. | covered |
| `tool.ToolSearch.5` | Once fetched, a tool is callable directly by its own name, exactly like the tools defined up front. | covered |
| `tool.ToolSearch.6` | Documented result format: each match as one `<function>{"description","name","parameters"}</function>` line inside a `<functions>` block — the same encoding as the up-front tool list. (Wire result differs; see r.1.) | covered |
| `tool.ToolSearch.7` | Query form `select:A,B,C` fetches exactly those tools by name. | covered |
| `tool.ToolSearch.8` | Query form of plain keywords does a keyword search returning up to `max_results` best matches. | covered |
| `tool.ToolSearch.9` | Query form `+word rest` requires `word` in the tool name and ranks by the remaining terms. | covered |
| `tool.ToolSearch.10` | The deferred set is not only MCP tools: built-in tools such as `WebFetch`, `WebSearch`, `NotebookEdit`, `Monitor`, `TaskStop`, `SendMessage`, the plan-mode, worktree and cron tools, and the MCP resource tools are deferred in the baseline (see `main-prompt.md` for the list). | covered (`DesignSync` left out, `parity/deviations.md`) |

## Result shape

| id | shape (observed) |
|---|---|
| `tool.ToolSearch.r.1` | The `tool_result` content is a list of `{"type": "tool_reference", "tool_name": "<name>"}` blocks, one per match, in query order (m1/0019, m1/0023, m1/0060, s1/0018). |
| `tool.ToolSearch.r.2` | The same user message carries, after the tool_result, a separate `text` block `Tool loaded.`. |
| `tool.ToolSearch.r.3` | From the next request on, each loaded tool appears in `tools` with its full schema and `defer_loading: true`, sorted into the list by name (MCP tools last); it stays there for the rest of the conversation, including after compaction. |
| `tool.ToolSearch.r.4` | The model was observed passing `max_results` equal to the number of names in a `select:` query (1–3) as well as the default 5. |
| `tool.ToolSearch.r.5` | (bundle, not recorded) `select:` reads at most 64 names; a name that is no tool's full name also matches MCP tools by their own name (without `mcp__<server>__`). A keyword query reads at most 32 words; a query equal to a tool's name (any case) loads that tool alone; an `mcp__…` prefix loads the tools whose names start with it; otherwise each word scores against the name's words and parts (MCP server and tool names weigh a little more) and, as a whole word, the description; `+word` filters on the same fields; ties keep the tool order; `max_results` is not raised to 1. No match: a plain-text result saying nothing matched. noeta today: covered (the no-match text in our words; no MCP connection notes). |

## Variants

Same hashes in every session.
