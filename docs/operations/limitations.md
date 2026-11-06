# Known limitations

What noeta does not do, or does less well than you might expect, as of the current code. Each
item says where it comes from: a decision (won't change without a new one), or an open follow-up
recorded in
[`docs/specs/2026-10-29-open-source-follow-ups.md`](https://github.com/initxy/noeta/blob/main/docs/specs/2026-10-29-open-source-follow-ups.md)
(may change). Every model-visible difference from Claude Code is listed, with its reason, in
[`parity/deviations.md`](https://github.com/initxy/noeta/blob/main/parity/deviations.md); this
page picks the ones you are most likely to notice.

## Status and platforms

- **Linux and macOS only.** The package declares POSIX Linux and macOS; nothing is built or
  tested for Windows.
- **Python 3.11 or newer.**
- **The macOS sandbox is looser than the Linux one.** The Seatbelt profile still allows
  `mach-lookup` and Apple Events (open follow-up: it needs a macOS machine to test). The Linux
  system-call filter knows only x86_64 and aarch64.

## One party per store

A store and a server serve one party. Whoever holds the server token drives every task, and
tools run as the worker's OS user. There are no tenants, users or per-user quotas
([ADR-028](https://github.com/initxy/noeta/blob/main/docs/adr/028-no-tenants-in-noeta.md)). To
keep parties apart, give each its own server and store schema, or build the separation in host
code ([run it as a server](../guides/server.md)).

## Several machines

Most of a task lives in its log and moves freely between workers. These parts do not:

- **The workspace.** Commands and file tools run on the task's `cwd`, which must look the same
  from every worker: a shared filesystem at the same path, or a remote exec env. noeta does not
  copy files between machines ([where commands run](../guides/sandbox.md)).
- **Background `Bash` jobs** live in one worker process. The task keeps its claim on that
  worker while the job runs; if the worker dies, the model is told the job was lost. A job
  cannot be reattached.
- **Pasted images** are written per worker; a worker that takes a task over writes them again.
  A task that continues another's conversation (`history_from`) does not copy the source's
  pasted images: their paths stop working once the source's image directory is cleaned (open
  follow-up).
- **Live text** (model deltas) crosses machines through Postgres notifications, best effort.
  Deltas published before a front end's `LISTEN` is up are not replayed; the recorded reply
  still arrives (open follow-up). On sqlite, live text stays in one process.
- **Upgrades are stop-the-world.** Workers of different versions must not share a store; stop
  every worker, upgrade, start them again. A process older than the store's schema refuses to
  open it.
- **Plugins must match on every worker.** A worker missing a plugin a task was started with
  never runs the task; after three such claims on the same worker the task fails with
  `plugin_missing` ([troubleshooting](./troubleshooting.md#a-worker-without-a-plugin-the-task-needs)).
- **Snapshots hold the whole conversation.** A 20,000-event log's snapshot is about 12 MiB, so
  on Postgres a claim of a long task is bound by transfer (open follow-up).
- **Dead workers' job log directories** (`w<pid>`) in an exec env that is not the local machine
  are not swept (open follow-up).

## Commands and the sandbox

- **The sandbox is optional and off by default.** Without it, commands run as the worker's user
  with the permission rules as the only fence. Without `failIfUnavailable`, a sandbox that
  cannot start (no `bwrap`) leaves commands unconfined, with a notice at start.
- **A link swapped in between the sandbox plan's resolution and `bwrap`'s mounts is not
  caught** (open follow-up).
- **Network access outside `sandbox.network.allowedDomains` is refused**, not asked about
  mid-command; Claude Code prompts.
- **`credentials` entries in `mask` mode act as `deny`** (the file hidden, the variable unset).
- **`EnterWorktree`'s worktree is read-only in the sandbox** (open follow-up).
- **No `/sandbox` command.** Configure the `sandbox` section of the settings.
- **`Bash` sources no shell startup file** (no `~/.bashrc`, no login profile) and runs the
  `bash` on `PATH`. Claude Code snapshots your login shell.
- **`Bash` keeps the last 256 KB a command printed**, saving output past 30,000 characters to a
  file; there is no `BASH_MAX_OUTPUT_LENGTH` override.
- **`Write` refuses content over 8 MB**: a call's input is logged and resent with every later
  request.

## Differences from Claude Code you may notice

- **Config places.** noeta never reads `~/.claude`, `.claude/`, `CLAUDE.md`, `.mcp.json` or
  `CLAUDE_*` variables. The formats are Claude Code's; the places are noeta's own
  (`~/.noeta`, `.noeta/`, `NOETA.md`, `AGENTS.md`).
- **Only the terminal UI's tasks get the `! <command>` rule** (the model suggests you run an
  interactive command with `!`), as only Claude Code's interactive terminal does; not with
  `--server`, where the terminal UI refuses `!` commands, nor `-p`, ACP, HTTP or the web app.
- **No `LSP` tool**, and a Claude Code plugin's LSP servers, monitors, `bin/`, themes and output
  styles are not loaded ([comparison](./comparison.md)).
- **No agent teams**, and `Agent` has no `name` parameter.
- **`WebSearch` needs a Tavily key** (`NOETA_WEB_SEARCH_API_KEY`); it returns each hit's
  snippet rather than a search model's summary.
- **No bundled skills.**
- **A `context: fork` skill always runs its agent in the background**, also under `-p`.
- **Skills:** a skill's `hooks:` frontmatter is not run; `skillOverrides` is not read.
- **Hooks:** a hook's input has an empty `transcript_path` (the transcript is the task log, read
  through a client). A `Stop` / `SubagentStop` hook that blocks the end of the turn 8 times in a
  row is let through. `TaskCreated` and `TaskCompleted` hooks, and Claude Code's other events
  noeta lacks (`Setup`, `ConfigChange`, `FileChanged`, …), are skipped with a warning.
- **MCP:** an MCP prompt's or an @-mentioned resource's text is cut at 64 KiB. Tool schemas go to
  the provider as the server gives them (Claude Code drops ones the API would reject). A tool
  whose name passes 128 characters is not offered. ACP over `--server` cannot pass the editor's
  MCP servers (they hold the editor's credentials); a Claude Code plugin's MCP servers are
  replaced by an ACP editor's (open follow-up).
- **`AskUserQuestion`** answers carry no per-question notes, and there is no free-form answer or
  timeout. Without an interactive front end or a permission-prompt tool the tool is not offered.
- **Unknown models** get a 128,000-token window and a 16,384-token output cap (Claude Code
  assumes 200,000 and 32,000). Give a gateway model's real limits in a models file.
- **`WebFetch` asks per domain** also for Claude Code's preapproved documentation hosts (open
  owner question).
- **Task tools:** a sub-agent gets no task list of its own; a finished list stays until its
  tasks are deleted.

## Known rough edges

Recorded follow-ups that a careful operator may run into:

- **Subprocess start at shutdown.** MCP stdio servers (`noeta/plugins/mcp/stdio.py`,
  `pool.py`) and the hooks runner (`noeta/plugins/hooks/runners.py`) start processes with
  asyncio subprocesses; before CPython 3.14 a loop shut down in the middle of a start can wait
  forever.
- **Channels.** A chat message redelivered after its task ended is handled again in the
  continuation task (a one-shot agent answers twice). Continuation ids use a `.N` suffix, which
  can collide with a thread id that itself ends in `.<digits>`.
- **Background job logs.** Empty job logs are never pruned by the byte cap, and a log writer that
  outlives its wait can recreate a log after cleanup (`noeta/plugins/shell/jobs.py`).
- **Memory recall.** The recall selector fails on gateways that refuse `thinking: disabled` or
  ignore an output schema (retried, then warned); recall stays off by default as in Claude Code.
  With memory listed before hooks, the selector still sees a prompt a hook blocked (the main
  model does not).
- **Trust in worktrees.** An isolated sub-agent's worktree is not a trusted project, so "always"
  answers given there are not saved to the project.
- **Runner bookkeeping.** `Runtime._running` is a set, not a count; two unlikely events together
  could let a task be adopted again at the same epoch.
- **Pasted-image save.** A file cut short by a crash mid-write is never written again.

## Decided against

Left out on purpose, with the reason; changing any of these takes a new decision.

- **No telemetry, no log upload, no update check**
  ([ADR-010](https://github.com/initxy/noeta/blob/main/docs/adr/010-nothing-uploaded-by-default.md)).
  Only the model provider you configure is contacted.
- **No sandbox for plugin code.** A plugin is Python in the worker's process; CPython cannot
  confine it. Isolation is offered where it is real: the exec env, MCP servers, command hooks.
- **No provider from a plugin file**: the plugin pin does not cover providers, and one on a single
  worker would break "any worker resumes any task".
- **No hosted plugin gallery or auto-update.** Installs are pinned to a commit and run nothing.
- **No rewriting of the raw provider payload** from a plugin: a request must be reproducible from
  the log.
- **MCP tools are never retried after a crash**: a server's `idempotentHint` is not trusted.
- **Short PDFs stay page images**, not a document block, so a `/model` switch to another provider
  keeps working.
- **No subscription login** for model providers: a server holds operator keys.
