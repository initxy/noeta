# Accepted deviations from Claude Code

A model-visible difference from the baseline is allowed only when noeta's kernel or product
forces it (`docs/specs/2026-10-28-claude-code-parity.md`, Decisions). Each row names the reason;
a backticked id is the `make parity` mismatch it accepts (the comparator does not count it).
A row without an id is a difference the replay cannot see.

## Kernel

| id | difference | reason |
|---|---|---|
| | Tools run after the assistant message is logged, not while it streams. | Log-first crash safety. |
| | A tool started before a crash answers "result unknown" instead of re-running. | A call may have had effects; running it twice is worse than asking. |
| | Waits (approval, sub-agent, timer) survive restarts and move between machines. | Durable tasks. |
| `6c773c2c` | The results of one reply's calls go back in call order; Claude Code answered a `ToolSearch` ahead of the background `Bash` call made before it (r2 m5). | Results are logged per call and folded in call order; the order of a batch is the kernel's, not a model-visible rule noeta can see the reason for in the recording. |
| `e9d39432` | After a compaction Claude Code sends an empty system turn carrying `output_config` (its effort); noeta sends none. | noeta puts the effort in the request's top-level `output_config` on every call, which the comparator already matches; the empty turn carries nothing the model reads. |
| | `EnterWorktree` moves the shell (where `Bash` starts) into the worktree; the task's root — the environment note, the file tools' relative paths, the permission scope — stays the launch directory, so a worktree must lie inside it (`.noeta/worktrees/`, or an existing one inside the working directory), and a sandboxed command cannot write there (`.noeta` is read-only in the sandbox). | A task's working directory is its config, fixed for its exec env and its sandbox; changing it mid-task needs a kernel config change (and its confinement rules for servers) left for later. |
| | `ScheduleWakeup` wake-ups and `CronCreate` jobs are kernel timers in the task's log: they survive restarts and fire on any worker, so `CronCreate.durable` changes nothing and its description says so (Claude Code keeps session jobs in memory and writes durable ones to `.claude/scheduled_tasks.json`). | Durable tasks. |
| | A `Workflow` run is replayed from its recorded agent results (the script is re-executed, finished `agent()` calls answer from the log) instead of running once in memory; it survives restarts like any wait. | Durable tasks. |

## Permissions

| id | difference | reason |
|---|---|---|
| | A `Write` / `Edit` outside the working and additional directories (and the plan directory) is refused, not asked about. | The file tools fence writes below the permission layer, so a host without the permissions plugin stays fenced; a directory is opened with `additionalDirectories`. |
| | "Always" answers in a trusted project are kept in `.noeta/settings.local.json` (Claude Code's format), and `.noeta/` is protected the way Claude Code protects `.claude/`. | noeta reads and writes only its own places (owner, 2026-10-28). |
| | Plan files live in `<noeta home>/plans/`. | noeta's own config directory. |

## Sandbox

| id | difference | reason |
|---|---|---|
| | A sandboxed command's connection to a host outside `sandbox.network.allowedDomains` is refused and listed in `<sandbox_violations>`; nobody is asked mid-command (Claude Code prompts unless `strictAllowlist`). | The network filter answers while the command runs; asking would need the task to park inside a running tool call. The model retries with `dangerouslyDisableSandbox`, which is asked about. |
| | `credentials` entries in `mask` mode act as `deny` (the file is hidden, the variable unset) on every platform. | Claude Code substitutes sentinels and swaps them at the proxy only on Linux, and only for HTTPS with TLS termination; noeta's proxy does not terminate TLS. |
| | The sandbox section of `Bash`'s description sends the user to the `sandbox` section of their settings, not to a `/sandbox` command. | noeta has no `/sandbox` command yet. |

## Tools

| id | difference | reason |
|---|---|---|
| | `Workflow` scripts run in an embedded V8 (`mini-racer`), no system Node: a fresh context per replay with a time and memory limit; where mini-racer cannot load, the tool refuses and starts nothing. | Owner-confirmed (2026-10-28): the contract (JavaScript, the script API) is Claude Code's; Claude Code runs the script in its own JavaScript runtime, noeta in a capability-free engine inside the worker. |
| | `Workflow`'s `name` loads a saved workflow from the project's `.noeta/workflows/`, then `~/.noeta/workflows/`, never `.claude/workflows/`, and its parameter description says so. | noeta reads only its own config places (owner, 2026-10-28). |
| `cd29435e` `b5c9150c` | No `DesignSync` in the deferred-tool list. | It syncs claude.ai design projects, a Claude Code cloud feature (the parity spec's non-goals). |
| | No `LSP` tool. | Claude Code offers it only with a language-server plugin installed; it is not in the baseline's tool set. |
| | `WebSearch` needs a search backend (a Tavily key, `NOETA_WEB_SEARCH_API_KEY`) and is not offered without one; the parity replay sets a placeholder key. | Claude Code searches through Anthropic's server-side tool, which only an Anthropic API key reaches; noeta runs on any provider. |
| | When no model call is available for the digest (a host running tools without a runner) or the digest call fails, `WebFetch` returns the rendered page text, marked as such, instead of failing. | Tools reach a model only through the runner the host binds; a host without one still gets the page. |
| | A binary `WebFetch` body (`tool.WebFetch.r.6`) is saved and named, and that line is the whole result: its bytes are not decoded as text for the digest or the page, and the cache keeps the line, so a repeat fetch names the same file. | Bytes decoded as text are noise to the digest model and to the result; the saved file is what the model can use, and `Read` opens it. |
| | A `Bash` command past its timeout is killed, not moved to the background, on a host that offers no background jobs (`Shell(background=False)`, or no runner to post the end to). | The move needs the job table and a task inbox for the end notice; such a host has neither. |
| | `Bash` keeps the last 256 KB a command printed (and saves output past 30,000 characters to a file); there is no `BASH_MAX_OUTPUT_LENGTH` override. | The capture cap bounds a worker's memory for every command; the inline limit is Claude Code's default. |
| | `Bash` has no `allowed_domains` parameter and its sandbox section no platform notes. | Both exist in Claude Code only with the sandbox on (and the parameter only in auto mode); the baseline runs without a sandbox, and its `Bash` schema has neither. |
| | A background agent's transcript (`output_file`) lies under the host's spill directory (`<spill root>/<root task id>/tasks/<agent id>.output`) and holds one `{"type", "message"}` line per message in noeta's message encoding, appended to as the conversation grows (written whole again after a compaction, or by a worker that did not write it last); a host without a spill directory names no file. | The task log is the record; the file is a projection of it written by the agent, so a crash or a move to another worker loses nothing (Claude Code appends its own session-record lines under its per-project temp directory). |
| | `SendMessage(notify_when_idle: true)` subscribes to another open root task of the store (a "session" `ListAgents` lists), which must run the `subtasks` plugin to answer; one subscription per session at a time (a new one replaces the last; Claude Code keeps up to three), the notice names the session by its task id, and it always carries the last reply's first line. The subscription, the answer and the 12-hour expiry are inbox posts, records and a kernel timer, so they survive restarts. | Durable tasks; noeta's sessions are tasks of one store, not processes that prove their identity to each other. |
| | `Edit` / `Write` refuse an unread file, or (`Edit`) one changed since the read, after a person approved the call rather than before the prompt (`tool.Edit.6`). The skip itself follows Claude Code: only for a write the permission layer allowed outright by its mode or an allow rule, never on older Claude models (Opus 4.6, Sonnet 4.5, Haiku 4.5 and before); a host without a permission layer always keeps the check. | The file tools run after the permission layer and see its verdict only as `ToolContext.approved` / `allowed_outright`; the refusal comes from the tool, once the call runs. |
| | A file counts as changed since the read when its bytes differ from what was read, not when its modification time moved (Claude Code refuses a `Write` / `NotebookEdit` over a file merely touched since). | The read record keeps a digest of the whole file; comparing content cannot refuse a write over unchanged bytes, and a restart cannot lose it. |
| | A PDF of up to 10 pages read without `pages` comes back as page images, not as one PDF document block. | Not every provider noeta drives takes a document block; page images work everywhere. |
| | `Read`'s empty-file and offset-past-the-end warnings are plain text in the result; Claude Code wraps them in `<system-reminder>`. | A tool result never carries the reserved tag (the wire neutralizes it), so a result cannot pose as host context. |
| | `Workflow`'s description carries a short script-API reference (`tool.Workflow.8`): there is no bundled `workflow-authoring` skill to load. | noeta ships no bundled skills yet; the reference returns to a skill when it does. |

## Request envelope

| id | difference | reason |
|---|---|---|
| `41a7944c` `fd6eb0f6` | No billing header block before the identity line (Claude Code's `x-anthropic-billing-header`, its version and entry point). | It tells Anthropic's billing which Claude Code build sent the call; noeta is not Claude Code and an API key needs no such line. |
| `5a78a8ed` `0a9c6f15` | The two system cache marks sit on `system[0]` and `system[1]` instead of `system[1]` and `system[2]`. | Same blocks (identity, prompt); the index shifts only because the billing header is absent. |
| `7678a2ee` | A sub-agent's request has no billing header block either. | As for the main request. |
| `098ae292` | A sub-agent's two system cache marks sit on `system[0]` and `system[1]`. | As for the main request: the billing header is absent. |
| `d334efde` | The summarize call's two system cache marks sit on `system[0]` and `system[1]`. | As for the main request: the billing header is absent. |

## Main prompt and environment message

| id | difference | reason |
|---|---|---|
| | The `!`-prefix rule (ask the user to run an interactive command as `! <command>`) is left out of the main prompt (`main.session.1`). | No noeta front end runs `!` commands yet; the rule returns with that front-end feature. |
| | The environment section names noeta's own front ends instead of Claude Code's, and leaves out fast mode (`main.envp.2`, `main.envp.3`). | Product facts of Claude Code; noeta has no fast mode. |
| | The attribution note's PR footer links to noeta, and is left out for a model the catalog gives no display name. | The footer names the tool that wrote the PR; the trailer needs the model's name. |

## noeta's own files

noeta reads only its own places (owner, 2026-10-28): Claude Code's mechanisms and formats, noeta's
file names. Each row is a name the model sees; the files themselves are product facts.

| id | difference | reason |
|---|---|---|
| | The instructions reminder lists `NOETA.md`, `.noeta/NOETA.md`, `.noeta/rules/`, `NOETA.local.md`, the user's `~/.noeta/NOETA.md` and `~/.noeta/rules/`, and the managed `NOETA.md` (`/etc/noeta`, macOS `/Library/Application Support/Noeta`), in Claude Code's order, where Claude Code lists `CLAUDE.md` and `.claude/` paths; a directory with neither project file shows its `AGENTS.md` (`rem.instructions.5`). | noeta never picks up Claude Code's instructions; `AGENTS.md` is the cross-tool convention. |
| | Texts that name the instruction file say `NOETA.md` where Claude Code says `CLAUDE.md`: the memory section (`main.mem.15`), the sub-agent prompts and caller messages (`sub.common.1`), the hand-back frame (`sub.result.4`), `Agent`'s `effort` description, and the auto-mode classifier's instructions block, which is a `<user_noeta_md>` element (`cls.input.1`). | The file the text means is noeta's. |
| | `Agent`'s description names agent files in `.noeta/agents/` (`tool.Agent.20`). | noeta's agent directory. |
| | An isolated agent's worktree is under `.noeta/worktrees/`, not `.claude/worktrees/`, and the auto-mode policy names `.noeta/` trees and `NOETA.md` as the agent's configuration. | noeta's own directory. |
| | An MCP result cut to its budget names `NOETA_MAX_MCP_OUTPUT_TOKENS`. | noeta's environment variables carry its prefix (`NOETA_MCP_TIMEOUT`, `NOETA_MCP_TOOL_TIMEOUT`, `NOETA_AUTO_COMPACT_WINDOW`, `NOETA_AUTOCOMPACT_PCT_OVERRIDE` likewise). |

## Instruction files

| id | difference | reason |
|---|---|---|
| | An `@path` import from a repository file that leaves the repository directory and the working directory is skipped, not asked about (`rem.instructions.7`). | No noeta front end has Claude Code's approval dialog for external imports; skipping is the safe side of it. |
