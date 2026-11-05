# Skills, agents and commands

Three kinds of markdown file change what noeta does without any code: skills (instructions the
model loads when a task calls for them), agent files (an agent's system prompt, tools and
plugins), and slash commands. All three are in Claude Code's formats, read from noeta's own
places. Claude Code plugin directories bring all three at once.

## Skills

A skill is a directory holding a `SKILL.md`: YAML frontmatter, then the instructions. The model
sees each skill's name and description in a listing and loads the whole file with the `Skill`
tool when it needs it; you can also type `/<name> [arguments]`.

```markdown
---
description: Cut a release: bump the version, update the changelog, tag. Use when asked to release or ship a version.
arguments: [version]
allowed-tools: Bash(git *), Edit
---
Release version $version of this project.

The last tag: !`git describe --tags --abbrev=0`

1. …
```

Save it as `~/.noeta/skills/release/SKILL.md`, and the skill is called `release`: a skill is named
by its directory; the frontmatter `name` is only a display name (`noeta/plugins/skills/registry.py`).

### Where skills are found

<Diagram name="guide-skills-lookup" caption="Where the skills plugin looks, highest precedence first; the first skill of a name wins" />

| Place | Read |
|---|---|
| the machine's managed directory's `.noeta/skills` (`/etc/noeta`, `/Library/Application Support/Noeta` on macOS) | always |
| `~/.noeta/skills/<name>/SKILL.md` | always |
| `.noeta/skills/` and `.agents/skills/` in the working directory and each directory above it up to the repository's root (nearest first; `.noeta` before `.agents` in one directory) | trusted projects |
| the same in each additional directory (`--add-dir`, `additionalDirectories`) | trusted projects |
| the managed directory's `.noeta/commands/`, `~/.noeta/commands/`, then the project's and additional directories' `.noeta/commands/` (trusted projects) | `*.md` files, as skills (below) |
| a Claude Code plugin's `skills/` | named `<plugin>:<skill>` |
| `.noeta/skills` and `.agents/skills` below the working directory, once a file tool touches a file there | trusted projects; never shadowing: one whose name is taken is named `<dir>:<skill>` (`apps/web:deploy`) |

The first skill found under a name wins, as in Claude Code, and the order is deterministic, so
the same tree always gives the same listing. A skill directory more than one level deep holds no
skill. Claude Code's `.claude/skills` is never read. A project that is not trusted
([`noeta trust`](./terminal.md#install-and-start)) has its skill directories skipped, with a
notice. Skills are looked for again whenever new input is taken and before each model call when a
`SKILL.md` changed, so a skill you add mid-task is found at once.

### Frontmatter

| Key | What it does |
|---|---|
| `description` | What the listing shows; without one, the body's first line (up to 100 characters) |
| `when_to_use` | Follows the description in the listing, after a dash |
| `name` | A display name shown beside the skill's name |
| `arguments` | Names for the words after `/<name>`: `$version` is the first, and so on |
| `allowed-tools` | Tools the skill's work may use without asking (pre-approved while it is active) |
| `disable-model-invocation: true` | Not in the listing: only you can run it, by typing `/<name>` |
| `user-invocable: false` | Not offered as `/<name>`; the model may still load it |
| `paths` | Globs: the skill is listed only once a file tool has touched a matching file |
| `context: fork` | Runs in a sub-agent (`agent`, default `general-purpose`) instead of the conversation; `disallowed-tools` takes tools from that agent |
| `model`, `effort` | The rest of the turn that loads the skill runs with this model and effort |
| `version` | Kept as metadata |

In the body, `$ARGUMENTS` is the whole argument string, `$0`, `$1` (or `$ARGUMENTS[0]`) the
shell-quoted words, `$<name>` a named argument, `\$` a literal dollar; arguments no placeholder
took follow the body on an `ARGUMENTS:` line (`noeta/toolkit/arguments.py`).
`${NOETA_SKILL_DIR}` (or `${CLAUDE_SKILL_DIR}`) is the skill's directory, and the loaded text
starts with a "Base directory for this skill" line so the model can read the files beside it.
A `` !`command` `` (after a line start or a space), or a fenced block opened with ```` ```! ````,
runs when the skill loads and is replaced by the command's output. Each command goes through the
task's permission checks as a `Bash` call; one that would be refused, or would need your answer,
stops the skill from loading, since there is nobody to ask at that moment
(`noeta/toolkit/inline_commands.py`).

### The listing and its budget

The `Skill` tool's own description never changes; the skills reach the model as a listing
recorded once per task, as Claude Code does (`noeta/plugins/skills/listing.py`). A skill added
later, or whose description changed, is listed again under the same header; removals are not
announced.

The listing gets 1% of the model's context window (in characters: the window times the model's
characters per token). Each description is cut at 1,536 characters. Over budget, every skill
first gets a short form (its first sentence, at most 24 tokens), then, as budget allows, its full
description, in order of use (skills used most across the store's tasks first), then place, then
name. A name is never dropped. To change the budget:

| Setting | Where |
|---|---|
| `skillListingBudgetFraction` (0–1), `skillListingMaxDescChars` | a settings file (Claude Code's keys) |
| `NOETA_SLASH_COMMAND_TOOL_CHAR_BUDGET` | the budget in characters, for every task |

## Agent files

An agent is a markdown file: YAML frontmatter saying what it is, and its system prompt as the
body (`noeta/agents/agent_files.py`). It is Claude Code's format plus two fields of noeta's,
`plugins` and `one_shot`.

```markdown
---
name: reviewer
description: Reviews a diff for bugs. Use after making changes, before committing.
model: sonnet                 # inherit, an alias, or a model id
tools: Read, Bash             # offer only these ("*": all)
disallowedTools: Edit, Write
permissionMode: default
maxTurns: 40
effort: high
---
You review code changes. Read the diff with `git diff`, …
```

| Field | What it does |
|---|---|
| `name`, `description` | Its name, and what the model reads when choosing a sub-agent |
| `model` | `inherit` (the caller's model), an alias (`sonnet`, `opus`, `haiku`, `fable`) or an id |
| `tools`, `disallowedTools` | Offer only these tools; never offer these (a rule like `Bash(npm:*)` counts as its tool) |
| `permissionMode` | The mode it runs in |
| `maxTurns` | Model calls per turn |
| `effort` | Its effort |
| `omitClaudeMd: true` | Shown only the managed instruction files |
| `identity` | The one-line identity block that opens its system prompt |
| `plugins` | Its plugins, in order: names, or `name: {arguments}` (noeta's) |
| `one_shot: true` | End with the first turn's answer (noeta's) |
| `skills` | Skills loaded when it starts |
| `mcpServers` | MCP servers for it: inline definitions, or names configured elsewhere |
| `hooks` | Hooks while it runs (its `Stop` hooks become `SubagentStop` when it runs as a sub-agent) |
| `memory: user\|project\|local` | Its own memory directory ([memory](./memory-and-instructions.md#agent-memory)) |
| `isolation: worktree` | As a sub-agent, runs in a git worktree of its own |
| `initialPrompt`, `autoCompactWindow`, `criticalSystemReminder_EXPERIMENTAL` | as in Claude Code |

A field noeta does not act on (`cacheTtl`, `observer`, …) is logged once. A file with
no `plugins` (a plain Claude Code agent file) gets the default set: permissions, plan mode,
budget, hooks, shell, web, MCP, files, skills, instructions, environment, sub-agents, compaction
and a few more (`noeta/agents/__init__.py` `DEFAULT_PLUGINS`). Every agent also runs
`tool_search` and `structured_output`.

### Where agent files are found

| Place | Named |
|---|---|
| the built-ins, `noeta/agents/files/*.md` | `main`, `Explore`, `general-purpose`, `Plan` |
| a Claude Code plugin's `agents/**/*.md` | `<plugin>:<agent>` |
| `~/.noeta/agents/*.md` | by `name` |
| a trusted project's `.noeta/agents/*.md` | by `name` |
| `--agents '{"name": {"description": …, "prompt": …}}'` (headless) | by key |

A later file replaces an earlier one of the same name, in that order, so your
`~/.noeta/agents/Explore.md` replaces the built-in Explore. `/agents` in the terminal UI lists
them; `/reload-plugins` reads them again.

The built-in agents:

| Agent | What it is |
|---|---|
| `main` | The agent you talk to: every plugin, the main system prompt |
| `general-purpose` | A sub-agent for research and multi-step work, with the file and shell tools |
| `Explore` | A read-only search sub-agent (`Read` only among the file tools; no edits, no sub-agents of its own) |
| `Plan` | A read-only sub-agent that designs an implementation plan |

The model can also start `subagent_type: "fork"`, a copy of itself that inherits the whole
conversation. Running sub-agents, in the background and otherwise: [sub-agents](./subagents.md).

To start the terminal UI or `-p` with another agent than `main`: `noeta --agent reviewer`, or
`"agent": "reviewer"` in a settings file.

### Adding plugins without copying a file

`config.toml` adds plugins to an agent by name, after the file's own; `"*"` is every agent:

```toml
[agents.main]
add_plugins = ["repetition_guard", {my_plugin = {level = 2}}]

[agents."*"]
add_plugins = ["run_tests"]

[plugins.web]
enabled = false          # off for every agent
```

Of the plugins the built-in agents name, `memory`, `mcp` and `repetition_guard` stay off until a
host sets them; the `noeta` command turns memory and MCP on. Writing a plugin:
[Write a plugin](./plugins.md).

## Slash commands

A slash command is something you type as `/<name> [arguments]`. Besides the front end's own
([terminal UI](./terminal.md#slash-commands)), they come from the agent's plugins, so the same
commands work in the terminal UI, the web app, editors and headless runs.

**Markdown commands.** `~/.noeta/commands/<name>.md` and a trusted project's
`.noeta/commands/<name>.md` are Claude Code's command files. noeta reads them as skills (as
Claude Code now does): you can type `/<name>`, and the model can load them with `Skill`. A file in
a subdirectory is named with its path (`.noeta/commands/git/fixup.md` is `/git:fixup`).

```markdown
---
description: Make a fixup commit for the last change to a file
argument-hint: <path>
allowed-tools: Bash(git add *), Bash(git commit *)
---
Stage $ARGUMENTS and make a `git commit --fixup` for the last commit that touched it.
```

**Claude Code plugins' commands** (`commands/**/*.md` in a plugin directory) are typed through the
`markdown_commands` plugin (`noeta/plugins/markdown_commands.py`), named `<plugin>:<name>`; the
model can load them too. Typing one sends the command; the task adds its body with the
arguments filled in, runs its `` !`…` `` lines through the permission checks, and pre-approves
the tools its `allowed-tools` names from then on.

The same plugin offers two prompt commands of its own: `/init` (write a `NOETA.md` for this
project, or improve the `NOETA.md` or `AGENTS.md` there) and `/review [PR | branch | paths]`
(review changes for defects, changing no files).

Other plugins' commands: `/compact`, `/plan`, `/loop`, `/mcp`, `/hooks`, and an MCP server's
prompts as `/mcp__<server>__<prompt>`. A plugin adds its own with `commands()` and
`run_command()` ([Extension interface](../reference/extension.md)).

## Claude Code plugin directories

A Claude Code plugin directory (`.claude-plugin/plugin.json`, `skills/`, `agents/`, `commands/`,
`hooks/hooks.json`, `.mcp.json`) put in `~/.noeta/plugins/`, or a trusted project's
`.noeta/plugins/`, loads through the plugins that already read those formats
(`noeta/cli/claude_plugins.py`):

| Part | Loaded as |
|---|---|
| `skills/` | skills, named `<plugin>:<skill>` |
| `agents/**/*.md` | agent files, named `<plugin>:<agent>` |
| `commands/**/*.md` | typed commands and skills, named `<plugin>:<command>` |
| `hooks/hooks.json` | hooks |
| `.mcp.json` | MCP servers (never from a project's plugin: you are told to copy them into `.noeta/mcp.json`) |
| `settings.json` `agent` | the agent front ends start with, below your own settings |

`${CLAUDE_PLUGIN_ROOT}` and `${CLAUDE_PLUGIN_DATA}` (`~/.noeta/plugin-data/<name>`) are filled in
everywhere, and the manifest's `userConfig` values as Claude Code fills them. Monitors, `bin/`,
LSP servers, themes and output styles are not loaded. Install one pinned to a commit:

```bash
noeta plugin add https://github.com/you/your-plugin@<40-hex commit>
noeta plugin add --marketplace ./team-marketplace formatter     # from a marketplace.json
noeta plugins                                                    # what is loaded, and from where
```

Installing runs nothing from the plugin. More on installing and configuring them:
[Write a plugin](./plugins.md).
