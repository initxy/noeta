# Refused calls (every tool)

Claude Code 2.1.312 · rules in our words (see `../README.md`). How a call that never ran is
answered, whatever the tool. Sources: the 2.1.312 bundle's tool-call runner and the recording
`2.1.312-r2-attempts/m1-escape-during-tool-input` (requests `0067`, `0071`); the baseline
sessions never send a refused call.

| source | sha256 |
|---|---|
| bundle 2.1.312, the tool-call runner (unknown tool, schema check, input check, call failure) | — (not recorded) |

## Rules

| id | rule | noeta today |
|---|---|---|
| `tool.common.1` | A call to a tool the request does not offer is answered `<tool_use_error>Error: No such tool available: <name></tool_use_error>` (an error result); a deferred tool called before it was loaded gets that tool-loading error inside the same tags. | covered — `core/tool_input.py` `NO_SUCH_TOOL`, `tool_use_error`; the withheld tool's error wrapped in `core/runner/calls.py` |
| `tool.common.2` | Errors that refuse a call before the tool acts come back inside `<tool_use_error>…</tool_use_error>`, with no tool-name prefix: arguments that do not fit the schema, and the tool's own checks of its input and of the state it needs (a file not read first or changed since, a string not found or found several times, a missing file, a refused path, a task id that names nothing running, a long leading `sleep`). A failure while the tool works (a command's exit code, a read or write that fails, a timeout) is a plain error result. Permission denials are plain too. | covered — `toolkit/tools.py` `input_error` for the tools' input checks (`Read`, `Edit`, `Write`, `NotebookEdit`, `Bash`, `Monitor`, `TaskStop`, `EnterWorktree`, `ExitWorktree`, `ExitPlanMode`); `tool_error` (`<tool>: <message>`) for failures while working |
| `tool.common.3` | Arguments are checked against the tool's input schema before any permission check, except an MCP server's tools (passed on as sent). A misfit reads `InputValidationError: <Tool> failed due to the following issue(s):` then one line per problem: a missing required parameter, an unexpected parameter, a parameter of the wrong type (expected and provided type named), then other problems (an enum value outside the options, at most 20, then a count). Strings that spell a number or boolean, and JSON text for an array or object, are accepted. | covered — `core/tool_input.py` `input_problems`, run in `core/runner/calls.py` for every tool without `unchecked_input` (MCP tools, host functions, the answer tool); a `null` for an optional argument counts as leaving it out (`deviations.md`) |
