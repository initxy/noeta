# Sub-agents and background work

This page is about work a task hands off and gets back later: sub-agents started with `Agent`,
scripted fan-outs with `Workflow`, scheduled prompts, and background shell commands. It says
how each one starts, how the task that started it waits and is woken, what the child inherits,
how to stop it, and what survives a crash.

The short version: every sub-agent is a **child task** in the same store, with its own log and
inbox. The parent never blocks on it. The `Agent` call returns at once, the parent's turn can
end, and the child's report and its end arrive later in the parent's inbox and wake it. While
it waits, the parent holds no worker.

## The tools

| Tool | Plugin | What it does |
|---|---|---|
| `Agent` | `subtasks` | Start one sub-agent of a listed type (or a `fork` of yourself). Returns at once with the agent id. |
| `SubagentHandback` | `subtasks` | Offered to a sub-agent in auto mode: delivers its final report to the caller and ends its run. |
| `SendMessage` | `subtasks` (deferred) | Message a running agent, resume a finished one, write to the caller (`to: "main"`), or to another open task. |
| `ListAgents` | `subtasks` | The agents this task started, and the store's other open top-level tasks. Main conversation only. |
| `Workflow` | `workflow` | Run a JavaScript script that starts many agents (`agent()`, `parallel()`, `pipeline()`, `phase()`). Returns a run id at once. |
| `TaskStop` | `shell` (deferred) | Stop a background shell job, a `Monitor`, an agent this task started, or a `Workflow` run. |
| `Bash` with `run_in_background`, `Monitor` (deferred) | `shell` | Background commands whose end (or each output line, for `Monitor`) comes back as a notice. |
| `ScheduleWakeup`, `CronCreate` / `CronList` / `CronDelete`, `/loop` | `schedule` | Prompts that fire later, once or on a cron schedule. |

Deferred tools are named to the model and loaded through `ToolSearch` when it needs them (see
[Built-in tools](../reference/tools.md)). The plugins are listed in
[Built-in plugins](../reference/plugins.md).

## Which agents a task can start

The built-in `main` agent carries `subtasks` with `agents: ["*"]`: every agent the worker knows
that has a `description`, except `main` itself, is a type the model may start. The built-ins
are `general-purpose`, `Explore` and `Plan` (`noeta/agents/files/`); your own agent files in
`~/.noeta/agents/*.md` or a trusted project's `.noeta/agents/*.md` join the list the same way
(the format is described in [Skills, agents and commands](./skills-and-agents.md)).

The model sees the list once, in the first request, one line per type with its tools. When an
agent file is added or removed (or plugins are reloaded) it gets a short note naming the types
that joined or left (`noeta/plugins/subtasks/roster.py`). A type name the model gets slightly
wrong (`general_purpose`, `explore`) is matched when it means exactly one type.

`Explore` and `Plan` carry `subtasks` with no types, so they get no `Agent` tool: they cannot
delegate. `general-purpose` can.

`subagent_type: "fork"` is special: the child is a copy of the caller. It starts from the
caller's whole conversation (`history_from` and `history_through` set to the parent's log),
runs the caller's own agent, and always on the caller's model. A fork cannot start another fork.

## Starting one

The model calls `Agent` with a short `description` and a `prompt`; `subagent_type`, `model`
(`sonnet`, `opus`, `haiku`, `fable`), `effort` (`low` … `max`) and `isolation` (`worktree`) are
optional. Before anything runs, the plugin checks the reply as a whole
(`noeta/plugins/subtasks/tool.py`):

- a bad call (empty prompt, unknown type, an `effort` that is not one of the five) is refused
  by itself; the reply's other calls still run;
- a task at depth `max_depth` (default 3; the main task is depth 0) cannot delegate;
- a task may have at most `max_background` agents running at once (default 20), counting the
  ones this reply would start;
- `isolation: "remote"` is refused: there is no remote isolation.

A good call stages the child task (its id is `<parent id>.<call id>.<n>`), records `launched`,
and returns "launched, agent id …". `Agent` is marked `run_last`, so the reply's other calls
run before any agent starts.

<Diagram name="guide2-task-tree" caption="A task tree. Every box is a task in the store; a resumed agent is a new task that starts from the old one's conversation." />

### What the child inherits

The child's config is built in this order, later steps winning
(`Subtasks.child_config`, `noeta/core/extension.py` `child_task`):

1. From the parent's config, the keys in `inherit_config`. The default is `cwd`, `model`,
   `effort`; the built-in `main` agent also passes `permission_mode`.
2. What the agent's definition asks for: its own `model` (unless `model: inherit`), `effort`,
   `permissionMode`, `isolation: worktree`.
3. The call's own `model` and `effort`.
4. What every plugin of the parent passes on through `Extension.inherit`: the permissions
   plugin passes its remembered rules and, if the person switched mode during the task, the
   current mode; memory, hooks, instructions, environment, plan mode and `ask_user` pass their own
   pieces.

`model: inherit` means "the caller's model". The built-in `Explore` is capped at the Opus family:
under a Fable caller it runs on Opus, as in Claude Code (`BUILTIN_INHERIT_CAP`,
`noeta/agents/agent_files.py`). The cap applies only to first-party Claude model ids; another
provider's id is inherited as it is.

### Isolation in a worktree

With `isolation: "worktree"` (or `isolation: worktree` in the agent file) the child gets a fresh
git worktree under `<repository>/.noeta/worktrees/agent-<hash>` on a new branch, and works
there (`noeta/plugins/subtasks/worktree.py`). When the agent ends with nothing changed, the
worktree is removed. When it holds work, it is kept, and the end notice names its path and
branch. If the parent ends while the agent is still running, its worktree is closed the same way.

## How the parent waits and is woken

The parent does not park on its children. It goes on with its turn; when the turn ends, the
main task waits for input like any idle task and gives its claim back. The child runs on
whichever worker claims it.

<Diagram name="guide2-child-wait" caption="The parent's turn ends and it holds no worker. The hand-back, then the child's end, arrive in its inbox." />

Two things come back, both through the parent's inbox:

1. **The report.** In auto mode (the permission mode the child inherits; no permissions plugin
   counts as auto) a sub-agent that has the tool is offered `SubagentHandback`. Calling it posts
   the report to the parent as a framed `agent`-origin message (`Say`, `when="now"`), which
   wakes the parent, and ends the child's run. A report over 100,000 characters is saved whole to a
   file in the sub-agent's spill directory and the parent gets a preview naming it.
2. **The end.** Every task that ends posts `ChildEnded` (status, answer, token usage, tool-call
   count) to its parent (`noeta/core/runner/closing.py`). The `subtasks` plugin turns it into
   Claude Code's `<task-notification>`: status `completed`, `failed` or `killed`, a summary, a
   `<usage>` block and, when the agent kept a worktree, `<worktree>`.

After a hand-back, the notice does not wake the parent a second time: it is held until the model
has answered the report, then rides along with the next request. Without a hand-back (outside
auto mode, or an agent whose tools exclude it), the child's last reply is the notice's
`<result>`, and the notice itself wakes the parent. An agent that was offered a hand-back and
ended without one delivers no result; the notice says so and (for any type but `Explore` and
`Plan`) suggests asking it with `SendMessage`.

A sub-agent's task always runs **one turn**: it ends when its turn ends, whatever its agent file
says (`TaskRun.one_shot`). If it started background agents of its own, it waits for them
(`Waiting` on `children`) before it ends, because their hand-backs may open another turn.

While a sub-agent runs, every 30 seconds (once it has three messages, and only if its
conversation changed) the plugin asks the model, beside one of its calls, for a 3–5 word label
of what it is doing. Front ends get it as a notice; the model never sees it. Turn it off with
`progress_labels = false`.

Each sub-agent also keeps a transcript, one JSON line per message, at
`<spill dir>/<root task id>/tasks/<agent id>.output`; the launch result and the notice name it.
It is a projection of the log, rewritten whole when a compaction changes the conversation or
another worker took the task over.

### Approvals and questions inside a sub-agent

A child that needs an approval or asks a question parks like any task: it logs `Waiting` and
holds no worker. Front ends look across the whole tree (`noeta/host/pending.py`
`pending_tree`), so the person sees and answers a sub-agent's approval from the main
conversation. A task whose config has `unattended: true` (a host sets it for tasks nobody
follows) does not wait: it fails with `needs_human` (a sub-agent ends; a top-level task's turn
ends and the task waits for its next message).

## Talking to a running or finished agent

`SendMessage` takes a `to` and a `message`:

| `to` | What happens |
|---|---|
| an agent id this task started, still running | the message lands in its inbox (`when="now"`) |
| an agent id that has finished | the agent is **resumed**: a new child task starts from the old one's conversation (`history_from`), under the same agent id, with the same config and worktree (if the worktree is gone, it works in the caller's `cwd`) |
| `main` (from a sub-agent) | a note to the caller, framed as from that agent |
| another top-level task's id | the message arrives at that task's next turn |
| `*` | refused: there is no broadcast |

From the main conversation, `notify_when_idle: true` subscribes to another top-level task and
wakes this one when that task goes idle or ends (at most 32 subscriptions, each expiring after
12 hours; `noeta/plugins/subtasks/idle.py`).

## Workflow: many agents from a script

`Workflow` runs a JavaScript script written against Claude Code's workflow API. The script
starts with `export const meta = { name, description, phases }` and then calls `agent()`,
`parallel()`, `pipeline()`, `phase()` and `log()`; `args` is passed through. The call returns a
run id (`wf_…`) at once and the run goes on in the background.

How it survives restarts (`noeta/plugins/workflow/__init__.py`, `engine.py`):

- The script runs in an embedded V8 (the `mini-racer` package), in a fresh context with no
  module loader, process, network, file API, clock or randomness, a 30 s time limit and a
  256 MB heap limit per replay.
- A replay is a pure function of the script, its `args` and the results recorded so far. An
  `agent()` with a result returns it; one without blocks. When a replay blocks, the plugin
  starts the waiting agents (up to `max_running` at once, by default CPUs − 2 between 2 and 16)
  as child tasks; each child's end records its result and triggers the next replay.
- Only the results live in the log, so a run moves between workers like any task. At most
  1000 agents per run.
- The end comes as a `<task-notification>` with the script's return value, the failed agents
  and the usage. Every run saves its script; `scriptPath` reruns an edited copy and
  `resumeFromRunId` reuses the results of agents whose prompt and options did not change.

Workflow agents copy the parent's `cwd`, `model`, `effort` and `permission_mode`; an
`agent()`'s options may set `model`, `effort`, `agentType` and a `schema` (the agent then
answers through `StructuredOutput`). Saved workflows live in `.noeta/workflows/<name>.js` or
`~/.noeta/workflows/<name>.js` and are run with `name`.

The model is told to use `Workflow` only when the person opts in: the word `ultracode` in a
prompt (for that turn), or the task's `ultracode` config switch, which keeps it on and reminds
the model every 10 prompts (`noeta/plugins/workflow/opt_in.py`).

## Background shell commands

`Bash` with `run_in_background: true` starts the command as a job and returns its id and
output file at once. A foreground command whose timeout (120 s by default, at most 600 s) runs
out is moved to the background instead of being killed. `Monitor` runs a script (or opens a
WebSocket) and delivers each stdout line as an event, until it exits or its timeout passes. When
a job ends, the model gets a `<task-notification>` with its exit code and output file, which
opens a turn if the task is idle.

A job is different from a sub-agent: it is a process on **one machine**. So
(`noeta/plugins/shell/jobs.py`, [ADR-029](https://github.com/initxy/noeta/blob/main/docs/adr/029-a-task-keeps-its-own-claim-while-it-owns-process-bound-work.md)):

- While a job runs, its task **keeps its claim** (`ToolContext.keep`). The task row says
  `waiting`, but the worker goes on renewing the lease and watching the inbox, so no other
  worker runs the task and its next turn can still stop the job.
- If that worker dies, the lease runs out, another worker claims the task, finds no such job,
  and the model is told the job is lost (`JOBS_LOST`). A guard beside the command kills the job
  once its worker stops showing signs of life, so a frozen worker's job does not run on after
  another worker has called it lost.
- A job dies with its task. A sub-agent's jobs die when it hands back; a sub-agent that needs
  a job's result waits for it first. With `background_ends_with_turn = true` jobs also stop
  when the task's turn ends.
- A task may have 8 jobs running at once (`max_jobs`). The output file keeps the first 64 MB.

## Scheduled prompts

The `schedule` plugin's wake-ups and cron jobs are kernel timers recorded in the task's log
(`view.set_timer`), so they survive restarts and fire on whichever worker holds the task
(`noeta/plugins/schedule/__init__.py`):

- `/loop <prompt>` repeats a prompt. Self-paced, the model calls `ScheduleWakeup` each
  iteration with a delay clamped to 60–3600 s; if it forgets, one fallback wake-up comes after
  20 minutes.
- `CronCreate` takes a five-field cron expression in local time: the task's `timezone` (which
  noeta's front ends send), else the zone of the machine that runs it. A recurring job fires until
  deleted or for 7 days; `recurring: false` fires once. Each job gets a small offset so jobs
  for the same minute do not all fire at once. At most 50 jobs per task.

The prompt arrives as the next message when the timer fires while the task is idle.

## Stopping things

| You want to stop… | How |
|---|---|
| a sub-agent, from the model | `TaskStop` with its agent id: its task is cancelled; the notice says "stopped by you" |
| a sub-agent, as a person | cancel its task from a front end or `Client.cancel(task_id)`; the notice says "stopped by the user" |
| everything a task started | end the task: its end posts `Cancel` to every live child |
| the parent's current turn only | `Client.cancel(task_id, turn_only=True)` (Esc in the terminal UI): open calls are closed and children a call was parked on are cancelled; background agents keep running |
| a `Workflow` run | `TaskStop` with the run id: the run ends `killed` and its running agents are cancelled |
| a background job | `TaskStop` with the job id; a parent can stop a running child's job the same way (posted to the child's inbox) |
| a loop or cron job | `ScheduleWakeup` with `stop: true`; `CronDelete` |

## Settings

Everything here is a plugin argument, set in `~/.noeta/config.toml` for every agent (an agent
file's own arguments win over these):

```toml
[plugins.subtasks]
max_depth = 2              # a task at this depth may not delegate (root = 0); default 3
max_background = 8         # agents one task may have running; default 20
progress_labels = false    # no 30-second progress labels
queue = "gpu"              # create children on this queue (default: the caller's)

[plugins.workflow]
max_running = 4            # agents one run keeps running at once

[plugins.shell]
max_jobs = 4                       # background jobs per task; default 8
background_ends_with_turn = true   # stop jobs when the turn ends
```

A child created on another queue is run only by workers serving it (`noeta worker --queue gpu`);
see [Run it as a server](./server.md).

## When things go wrong

- **A worker dies while a sub-agent runs.** Nothing special: the child is a task; its lease
  runs out and another worker continues it from its log. The parent is not involved.
- **A crash cuts an `Agent` call short.** The launch is written together with the call's
  result, so a call cut short started nothing. On recovery its outcome is "unknown" to the
  model, and a worktree it made is removed unless it holds work.
- **A crash cuts a hand-back short.** It is made again on recovery; the parent drops the
  repeated report by its dedupe key.
- **A crash between a child's end and its notice.** On the parent's next start, ends that were
  taken but never turned into a notice are turned into one (`Subtasks.on_start`).
- **A sub-agent fails.** Its notice has status `failed` and the error. A sub-agent that hits its
  step limit is `completed` with a note that the result is partial.
- **A background job's worker dies.** The model is told the job is lost; nothing re-runs it.

Related: [Waiting and waking](../how-it-works/waiting.md),
[One writer and crash recovery](../how-it-works/recovery.md),
[Many machines](../how-it-works/many-machines.md).
