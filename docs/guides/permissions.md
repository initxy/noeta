# Permissions and hooks

What the model may do without asking, how to change that with modes, rules and hooks, how plan
mode keeps a task read-only until you approve a plan, and how to put files back with rewind. The
formats are Claude Code's; the files are noeta's own (`~/.noeta/settings.json`,
`.noeta/settings.json`, `.noeta/settings.local.json`).

## Permission modes

Every task runs in one permission mode, recorded in its log. The default is `auto`, as in Claude
Code.

| Mode | Shown as | What asks |
|---|---|---|
| `default` | Default (ask before changes) | edits, commands that are not read-only, fetches, most other tools |
| `acceptEdits` | Accept edits | as `default`, but file edits inside the working directories run without asking |
| `plan` | Plan (explore, change nothing) | anything that would change something ([plan mode](#plan-mode)) |
| `auto` | Auto (a classifier screens what would ask) | a model classifier answers what would ask; firm asks still reach you |
| `dontAsk` | Don't ask (refuse what would ask) | nothing: what would ask is refused |
| `bypassPermissions` | Bypass permissions (ask for nothing) | only firm asks and writes to protected paths |

Set the starting mode with `--permission-mode MODE` (`manual` is taken as `default`), a settings
file's `permissions.defaultMode`, an agent file's `permissionMode`, or a task's config. Switch it
while a task runs with `Shift+Tab` in the terminal UI, the mode picker in the web app or an editor,
the SDK's `set_permission_mode`, a card's "Yes, and switch to …" choice, or the `permission_mode`
signal over the client API. A sub-agent starts in its caller's mode.

`bypassPermissions` can be switched off: `"disableBypassPermissionsMode": "disable"` in a
settings file's `permissions`. `noeta serve` and `noeta worker` refuse it unless started with
`--allow-bypass-permissions`, whatever the request says.

## Rules

Rules are Claude Code's `Tool` or `Tool(content)` strings in three lists, in any settings file:

```json
{
  "permissions": {
    "allow": ["Bash(npm run test *)", "Bash(git status)", "Edit(src/**)", "WebFetch(domain:docs.python.org)"],
    "ask":   ["Bash(git push *)"],
    "deny":  ["Read(./.env)", "Read(~/.ssh/**)", "Bash(rm -rf *)", "mcp__github__delete_repo"],
    "additionalDirectories": ["../shared-lib"],
    "defaultMode": "acceptEdits"
  }
}
```

The files, lowest precedence first: `~/.noeta/settings.json`; a trusted project's
`.noeta/settings.json` and `.noeta/settings.local.json`; the machine's managed
`managed-settings.json` (`/etc/noeta`, `/Library/Application Support/Noeta` on macOS). Rule lists
and `additionalDirectories` add up across files; `defaultMode` is the last file's. Headless runs
add `--allowedTools` and `--disallowedTools` to the lists ([the -p flags](./terminal.md#p-one-prompt-no-ui)).
Claude Code's own `~/.claude/settings.json` is never read.

The syntax (`noeta/toolkit/permission_rules.py`):

| Rule | Matches |
|---|---|
| `Bash` | every call of the tool (a bare name works for any tool) |
| `Bash(npm run test)` | exactly that command |
| `Bash(npm run *)`, `Bash(npm run:*)` | by prefix; a `*` anywhere matches any text, and a trailing ` *` also matches the bare prefix |
| `Read(path)`, `Edit(path)` | gitignore-style patterns: `//abs` an absolute path, `~/p` under your home, `/p` and `p` / `./p` relative to the project root (a pattern with no `/` matches at any depth); `*` stays in one directory, `**` crosses them |
| `WebFetch(domain:example.com)` | that host; `*.example.com` its subdomains |
| `mcp__server`, `mcp__server__*` | every tool of an MCP server; `mcp__server__tool` one tool |
| `Agent(Explore)` | starting that sub-agent type |
| `Skill(name)` | that skill |

`Edit(...)` rules cover every tool that writes files (`Edit`, `Write`, `NotebookEdit`), and
`Read(...)` rules every tool that reads them. A compound command (`a && b | c`) is judged part by
part: a deny or ask rule matching any part applies, while an allow rule must cover every part and
never covers a command substitution or a redirection to a file (`npm test $(id)`). Wrappers that
only run the rest (`timeout`, `time`, `nice`, `nohup`, `stdbuf`) are looked through.

A deny rule wins over everything, then an ask rule (no mode skips it, `bypassPermissions`
included), then an allow rule. Deny rules on paths also cover the shell: a `Bash` command that
writes a path an `Edit` deny rule covers, reads one a `Read` deny rule covers, or removes a
directory holding one, is refused in every mode.

When you answer a card with "Yes, and don't ask again for …", the rule the card named is
remembered for the task and the sub-agents it starts, and, in a trusted project, added to
`.noeta/settings.local.json` (which is kept out of git through `.git/info/exclude`).

## How a call is decided

Before any tool call runs, every plugin of the agent is asked about it (`before_tool`), and the
kernel combines the answers (`noeta/core/runner/calls.py`). The permissions plugin is one of those
plugins; hooks, skills' `allowed-tools` and plan mode are others.

<Diagram name="guide-gates" caption="Every plugin's verdict on one call, combined: a deny wins, a rewrite is gated again, an ask parks unless something answers it first" />

1. **A deny from anyone wins.** A plugin that raises while checking counts as a deny
   (`permission check failed: …`), so a broken check never lets a call through.
2. **A rewrite** (a hook's `updatedInput`, a client's edited approval) replaces the call, which is
   gated again from the top, at most three times.
3. **Asks.** With none, the call runs. An approving allow (a PreToolUse hook's `allow`) satisfies
   asks that are not firm; a firm ask (an ask rule, a hook's `ask`) always needs an answer.
4. **Answering an ask before it reaches you** (`resolve_ask`): a PermissionRequest hook, the
   auto-mode classifier, a headless client's `can_use_tool`, or an MCP permission-prompt tool.
5. **Otherwise the call parks.** The task records `Waiting`, gives up its worker and shows you
   the card. Your answer arrives in its inbox and the call goes on, on whichever worker picks it up.

A reply's calls are gated in order and gating stops at the first ask: the calls after it wait,
ungated, until it is answered.

Inside the permissions plugin (`noeta/plugins/permissions/rule_check.py`), Claude Code's order:

<Diagram name="guide-permission-decision" caption="What the permissions plugin checks, in order, and what the mode makes of an ask" />

- Tools taken away (`--tools`, `--disallowedTools` names, an agent file's `disallowedTools`)
  are denied, and are not even offered to the model.
- Deny rules, then the shell's path checks, then ask rules.
- What the call touches: a read inside the working directories (or of the plan file, the memory
  directory, a saved tool output) is allowed; an edit inside them asks in `default`; an edit
  outside them asks in every mode but `bypassPermissions`; a write to a protected path asks even
  in `acceptEdits` and `bypassPermissions` (in `auto` the classifier judges it). A shell command whose every part is read-only (`ls`, `git log`, `grep`, …) and
  reads only inside the working directories is allowed; any other asks unless an allow rule
  covers every part. `WebFetch` asks per host. Tools that only read or talk to you
  (`AskUserQuestion`, `Skill`, `ToolSearch`, the todo tools …) are allowed, and so are the cron
  tools and `EnterWorktree` / `ExitWorktree` ([tools](../reference/tools.md#at-a-glance)).
- Then the mode: `dontAsk` turns the ask into a deny, `bypassPermissions` into an allow (not a
  firm ask, not a protected path), `auto` hands it to the classifier, `plan` keeps it.

Protected paths (`noeta/toolkit/protected_paths.py`) are the directories `.git`, `.vscode`,
`.idea`, `.claude`, `.husky`, `.cargo`, `.devcontainer`, `.yarn`, `.mvn`, `.noeta` (except its
`skills`, `commands` and `worktrees`) and `.config/git`, and files that configure a shell, git, a
package manager or a hook runner (`.bashrc`, `.zshrc`, `.gitconfig`, `.npmrc`, `.envrc`,
`.mcp.json`, `.pre-commit-config.yaml`, …).

### Auto mode

In `auto` mode a second model, the classifier, reads each call that would ask, together with your
instruction files (in a trusted project) and the conversation, and allows or denies it. A deny
tells the model why and the turn goes on; a firm ask still comes to you. The classifier's model
is `classifier_model` ([side models](./models.md#side-models)). Its guidance is Claude Code's
`autoMode` object, as the permissions plugin's `auto_mode` argument:

```toml
[plugins.permissions.auto_mode]
allow = ["$defaults", "Running the project's test suite"]
soft_deny = ["$defaults", "Pushing to any remote"]
```

Each list replaces the built-in text of its section; `"$defaults"` puts the built-in text at its
place. Allow rules that would let a whole class of calls past the classifier (a bare `Bash`, say)
are set aside while it judges.

## Hooks

Hooks are Claude Code's: commands, HTTP endpoints, model checks or MCP tools you configure to run
at fixed points of a task. They can add context for the model, change or stop what is about to
happen, or answer a permission prompt for you. They go in the same settings files, under
`hooks`, or in a Claude Code plugin's `hooks/hooks.json`:

```json
{
  "hooks": {
    "PreToolUse": [
      {"matcher": "Bash", "hooks": [{"type": "command", "command": "~/.noeta/hooks/check-bash.sh", "timeout": 30}]},
      {"matcher": "Edit|Write", "hooks": [{"type": "command", "command": "ruff format --quiet \"$NOETA_PROJECT_DIR\"", "if": "Edit(**/*.py)"}]}
    ],
    "SessionStart": [
      {"matcher": "startup", "hooks": [{"type": "command", "command": "git log --oneline -5"}]}
    ]
  }
}
```

`/hooks` in the terminal UI lists what is configured.

### Events

| Event | Matched on | What a hook can do |
|---|---|---|
| `PreToolUse` | tool name | `permissionDecision`: `deny`, `ask` or `allow`; `updatedInput` rewrites the call (gated again) |
| `PermissionRequest` | tool name | when a call would wait for you: allow (optionally rewriting) or deny in your place |
| `PermissionDenied` | tool name | after the classifier denied a call; `retry` tells the model it may try again |
| `PostToolUse`, `PostToolUseFailure` | tool name | add context after the result, block, or replace it (`updatedToolOutput`) |
| `PostToolBatch` | — | once every call of a reply has its result: add context, or end the turn |
| `UserPromptSubmit` | — | add context, or block: the turn fails and your messages are erased |
| `Stop`, `SubagentStop` | — / agent type | block to make the model go on, with the reason as its next message |
| `SessionStart` | `startup`, `resume`, `compact` | add context for the model |
| `SubagentStart` | agent type | add context for the sub-agent |
| `PreCompact`, `PostCompact` | `manual`, `auto` | add summary instructions, or skip the compaction; shown to you after |
| `PostModelSwitch` | the new model | add context after a front end switched the model |
| `Notification` | notification type | side effects only (waiting on an approval or a question) |
| `SessionEnd`, `StopFailure`, `CwdChanged`, `InstructionsLoaded` | reason, error, —, load reason | side effects only |

Claude Code's other events (`Setup`, `PreModelSwitch`, `FileChanged`, `Elicitation`, …) are
skipped with a warning; each belongs to a Claude Code feature noeta does not have.

### Matchers and `if`

A matcher is tested against the event's subject (the table's middle column). A plain list
(`Edit|Write`, `Edit, Write`) is compared name by name; anything else is a regular expression
searched in the subject; empty or `*` matches everything. A hook's `if` is a permission rule
(`Bash(git *)`, `Edit(**/*.py)`) the tool call must also match; on an event without a tool call
such a hook never runs. Identical hooks run once per event (`noeta/plugins/hooks/config.py`).

### Hook types

| `type` | Runs | Fields |
|---|---|---|
| `command` | `sh -c` on the worker, in the task's directory, with the event as JSON on stdin | `command` (or `args`, no shell), `timeout` (default 600 s), `async`, `asyncRewake`, `onFailure`, `once` |
| `http` | POSTs the event as JSON; the response body is read like stdout | `url`, `headers` (`$VAR` filled only for names in `allowedEnvVars`) |
| `prompt` | the task's model (or `model`) judges the condition; `$ARGUMENTS` is the event JSON | `prompt`, `model`, default timeout 30 s |
| `agent` | like `prompt`, but may first look with read-only tools (up to 50 calls) | `prompt`, `model`, default timeout 60 s |
| `mcp_tool` | calls a tool of an MCP server the task has | `server`, `tool`, `input` (`${tool_input.file_path}` filled from the event) |

A command hook's environment is noeta's (minus its own credentials) plus `NOETA_PROJECT_DIR`
(and `CLAUDE_PROJECT_DIR`, for scripts written for Claude Code). An `async` hook runs in the
background and what it adds reaches the model with its next call; an `asyncRewake` hook's exit 2
wakes the model with its output. Both come back through the task's inbox, so they work on any
worker.

### What a hook returns

- **Exit 0**: success. stdout is plain text (context for SessionStart and UserPromptSubmit) or a
  JSON object: `continue` / `stopReason`, `decision` / `reason`, `systemMessage`,
  `hookSpecificOutput` (`permissionDecision`, `permissionDecisionReason`, `updatedInput`,
  `additionalContext`, …).
- **Exit 2**: block, with stderr as the reason.
- **Anything else**: reported and ignored, unless `"onFailure": "block"`.

Text over 10,000 characters is saved to the task's spill directory and a preview sent. Every
event's input carries Claude Code's fields (`session_id`, the root task's id; `cwd`;
`permission_mode`; `hook_event_name`; `transcript_path`, empty) plus noeta's `task_id`, and
`agent_id` / `agent_type` inside a sub-agent.

### Deny wins

Among PreToolUse hooks, the first `deny` (or block) decides; otherwise an `ask` from any hook
beats an `allow`. Across plugins the kernel's rule applies: a hook's deny wins over the
permissions plugin's allow, and a hook's allow skips asking only where nothing firm asks, so a
deny or ask rule still wins over it. A worker that cannot reach the client that registered a
`callback` hook refuses the calls that hook would judge.

## Plan mode

In plan mode the model explores, writes a plan to a file, and changes nothing else until you
approve the plan (`noeta/plugins/plan_mode/`).

- Enter it with `/plan [request]`, by switching to `plan` with `Shift+Tab`, or when the model calls
  `EnterPlanMode` (which asks you first, except in `auto` mode).
- While planning, only reads, read-only commands and writes to the plan file run without asking
  (plan files live in `~/.noeta/plans/`). A plan begun in `auto` mode keeps the classifier for
  shell commands (Claude Code's `useAutoModeDuringPlan`, on by default; `false` in a settings
  file turns it off); edits always come to you.
- The model calls `ExitPlanMode` when the plan is written. You see the plan and choose: "Yes,
  and use auto mode" (or "Yes, auto-accept edits"), "Yes, manually approve edits", or "No, keep
  planning" with what to change.
- Sub-agents of a planning task plan too; they get no `ExitPlanMode` and answer in their final
  message.

Without the permissions plugin, plan mode still keeps the task read-only by itself, asking about
any call of a tool that is not read-only.

## Checkpoints and rewind

Right before `Edit`, `Write` or `NotebookEdit` changes a file, the `checkpoints` plugin records
the file's content, once per file and prompt (`noeta/plugins/checkpoints.py`). Contents are stored
once per task, by hash. Changes a `Bash` command makes are not tracked, as in Claude Code.

`/rewind` (or `Esc` `Esc` on an empty prompt) lists your prompts, each with the files changed
since, and puts back what you choose:

| Choice | What happens |
|---|---|
| Restore code and conversation | both of the below |
| Restore conversation | a new task continues the log up to just before that prompt; the prompt is put back in the box to edit |
| Restore code | the files go back to their content before that prompt; a file that did not exist is removed |

The conversation is never cut in place: the old task stays in the store and the new one starts
from its history (`history_from`), so a rewind cannot lose anything. Files are written back
through the task's own environment, wherever it runs, and only content the log recorded for that
path is written; anything else is reported as not restored (`noeta/host/rewind.py`). The same
helpers serve every front end: `/rewind` in the terminal UI and over ACP, the rewind dialog in the
web app, `rewind_files` in the headless protocol, and `GET` / `POST /tasks/{id}/rewind` over HTTP.
