# Instructions and memory

What the model is told about your project and you before it starts work: instruction files
(`NOETA.md`, `AGENTS.md`, rules), the environment block, and auto memory, the notes the model
keeps for itself across conversations. Also the task list. Each is a plugin, and each follows
Claude Code's behavior in noeta's own files.

## Instruction files

Instruction files are markdown you write for the model: how to build and test, conventions,
things it would otherwise get wrong. The `instructions` plugin reads them where Claude Code reads
`CLAUDE.md`, under noeta's names (`noeta/plugins/instructions/`). Claude Code's own files
(`CLAUDE.md`, `.claude/`, `~/.claude/`) are never read.

```bash
noeta              # then type /init: the model writes a NOETA.md for this project
```

### Which files, in which order

<Diagram name="guide-instructions-sources" caption="Where instruction files come from at a task's start, and which ones follow later when a file is touched" />

When a task first starts, the plugin reads, in this order, and puts them all in one message before
your first prompt, each with its path and kind:

1. **Managed:** the machine's `NOETA.md` and `.noeta/rules/` in the managed directory
   (`/etc/noeta`, `/Library/Application Support/Noeta` on macOS).
2. **User:** `~/.noeta/NOETA.md` and `~/.noeta/rules/`.
3. **Project**, for every directory from the outermost ancestor of the working directory down to
   it: `NOETA.md` and `.noeta/NOETA.md`, or, in a directory with neither, its `AGENTS.md` (the
   cross-tool file); the rules under `.noeta/rules/`; then `NOETA.local.md` (yours, not checked
   in).
4. **Memory:** the auto memory index, `MEMORY.md` ([below](#auto-memory)).

The message opens with a line saying these instructions override the model's default behavior.
The files are part of the conversation's log, so a resumed task, on any worker, sends the same
bytes.

### Rules, imports and conditional files

- **Rules** are `*.md` files under a `.noeta/rules/` directory (subdirectories included). A rule
  whose frontmatter has `paths:` is conditional:

  ```markdown
  ---
  paths: ["src/api/**", "tests/api/**"]
  ---
  API handlers return `Result`, never raise.
  ```

  It is left out at the start and shown once a file it matches is touched.
- **Imports:** `@path` in a file (outside code) imports that file right after it, with the same
  kind, up to four imports deep; each file is read once. An import from a project or local file
  must stay inside the working directory or the folder of the file that began the chain
  (Claude Code asks before following one outside; noeta has no such prompt and skips it).
- **Comments:** a leading YAML frontmatter is cut, and HTML comments standing as blocks of their
  own are dropped, so `<!-- notes for humans -->` costs nothing.

### Files that follow the work

When `Read`, `Edit`, `Write` or `NotebookEdit` touches a file inside the working directory, or a
`Bash` command shows one (`cat`, `head` and the like), the instruction files that apply to it and
were not shown yet follow the tool's result: those of every directory between the working
directory and the file, and the `paths:` rules whose globs match it. So `packages/api/NOETA.md`
reaches the model the first time it works in `packages/api/`.

With each new message from you, the files shown so far are read again, and those whose text
changed are shown again. A compaction puts them all, read again, ahead of its summary.

A file a `Read` deny rule covers (`"deny": ["Read(./NOETA.local.md)"]`) is not loaded, and you
are told so. The `InstructionsLoaded` hook fires once per file shown
([hooks](./permissions.md#hooks)). An agent file with `omitClaudeMd: true` (the built-in
`Explore` and `Plan`) is shown only the managed files. `/memory` in the terminal UI lists the
instruction files and opens one in `$EDITOR`.

Untrusted projects still have their `NOETA.md` and `AGENTS.md` read: they are text for the model,
not code. What a project can run or change (its plugins, agents, prompt overrides, skills and
settings) waits for `noeta trust`.

## The environment block

The `environment` plugin tells the model where it is, as Claude Code's environment section does
(`noeta/plugins/environment/`). With the first request, after the system prompt:

```text
# Environment
This conversation runs in the following environment:
 - Primary working directory: /home/me/src/my-project
 - Is a git repository: true
 - Additional working directories:
  - /home/me/src/shared-lib
 - Platform: linux
 - Shell: bash (login shell: zsh)
 - OS Version: Linux 6.8.0
 - Anything you download or unpack is untrusted input. …

Model: Sonnet 5.5, exact id `claude-sonnet-5-5`; knowledge cutoff ….
```

and, in the first user turn, a git snapshot (branch, `git status` cut at 2,000 characters, the
last five commits) and the commit and PR attribution note. Then, as the task goes on:

- **Changes** are told as they happen: the model switched, the directory became a repository or a
  worktree, a directory added. The git snapshot is taken again at the start of a later turn when
  the branch or HEAD moved since the previous one ended.
- **The date** is today's in your time zone, which noeta's front ends send (`timezone`, an IANA
  zone). A task with none (a server driven by a client that sends none) is given the UTC date,
  marked as UTC's, never the server's own. Past midnight the model is told the new date.
- **Deferred tools** (loaded with `ToolSearch`) are listed by name.

Its settings come from Claude Code's settings keys and the plugin's arguments:

| Setting | Where | What it does |
|---|---|---|
| `language` | a settings file | the model answers in this language (told again when it changes) |
| `attribution.commit`, `attribution.pr` | a settings file | the line for commits and PRs; `""` for none (`includeCoAuthoredBy: false` sets both) |
| `git_status = false` | `[plugins.environment]` | no git snapshot |
| `model_name = false` | `[plugins.environment]` | no model line |
| `date_change = false` | `[plugins.environment]` | not told when the date changes |
| `enabled = false` | `[plugins.environment]` | no environment block at all |

## Auto memory

Auto memory is Claude Code's: a directory of markdown files the model writes for itself, one fact
per file, kept across conversations (`noeta/plugins/memory/`). The `noeta` command turns it on.

- **Where:** `~/.noeta/projects/<project>/memory/`, where `<project>` is the repository's main
  checkout (else the working directory) with every character that is not a letter or digit made
  `-`. Worktrees of one repository share it.
- **How it is written:** the system prompt's memory section tells the model the directory and the
  format (frontmatter `name`, `description`, `metadata.type`: `user`, `feedback`, `project` or
  `reference`). The model creates and edits the files with the ordinary `Write` and `Edit` tools,
  and markdown writes there never ask for permission, in any mode. You can edit them yourself.
- **The index:** `MEMORY.md` in that directory, one line per memory. Its first 200 lines and
  25,000 characters are shown with the instruction files when a task starts, and again after a
  compaction.
- **Recall (optional):** with `recall_model` set, each message of yours asks that model which
  memory files bear on it, and up to five are attached (each cut at 200 lines and 4,096 bytes; at
  most 61,440 characters over a task). A memory last changed two or more days ago comes with a note
  that it may be out of date. A file the model already read is not recalled again.

```toml
[plugins.memory]
recall_model = "claude-haiku-5-5"   # off when unset
# enabled = false                    # no auto memory
```

`/memory` in the terminal UI shows the index path. The memory files are files in the task's
environment, not store rows; on several machines, `~/.noeta/projects` must be shared or live in a
remote environment ([the server guide](./server.md)).

### Agent memory

An agent file's `memory:` field gives that agent a directory of its own (Claude Code's agent
memory):

| `memory:` | Directory |
|---|---|
| `user` | `~/.noeta/agent-memory/<agent>/` |
| `project` | `<cwd>/.noeta/agent-memory/<agent>/` |
| `local` | `<cwd>/.noeta/agent-memory-local/<agent>/` |

A sub-agent without the memory plugin is shown its caller's index, without the memory section.

## The task list

The `todos` plugin gives the model Claude Code's task list: `TaskCreate`, `TaskGet`,
`TaskUpdate` and `TaskList` (all loaded through `ToolSearch`), and a reminder after ten model turns
without them (`noeta/plugins/todos/`). As in Claude Code 2.1.312, only the Claude models older than
its lean prompt (Claude 3 and Claude 4, but not Opus 4.8) get them by default; the Claude 5 family
and every other model (DeepSeek, a gateway's name) get none. To give them to every model:

```toml
[plugins.todos]
all_models = true
```

Only a top-level task gets them (sub-agents run in the background). The list is a fold of the
task's log, so it survives a compaction, is the same on any worker, and a forked or rewound task
starts with it. `Ctrl+T` in the terminal UI shows it; the web app has a panel; editors get it as
the ACP plan.
