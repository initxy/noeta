# Configuration

Where noeta's settings come from, which one wins, and what a project may change only once
you trust it. Every key and variable is listed in [Configuration keys](../reference/config.md).

## Two kinds of settings

noeta has two configuration surfaces, and they answer different questions:

- **noeta's own: which model, which provider, which store, which plugins.** These come from
  command-line flags, `NOETA_*` environment variables and `~/.noeta/config.toml`
  (`noeta/cli/config.py`).
- **Claude Code's formats: what the agent may do and how it behaves.** Permission rules,
  hooks, MCP servers, sandbox, `env`, language, attribution lines, the starting agent. These
  come from settings files in Claude Code's `settings.json` format, read from noeta's own
  places (`noeta/cli/settings_files.py`). Agent files, skills, commands, instruction files and
  plugin folders are Claude Code's formats too.

<Diagram name="start-config-places" caption="Higher wins. The two project settings files load only after noeta trust." />

## Model, provider, store: flags > environment > `config.toml`

For the model, the provider, the base URL, the API key, the store and the effort, the first
of these that sets a value wins (`load_settings` in `noeta/cli/config.py`):

1. a flag: `--model`, `--provider`, `--base-url`, `--store`, `--effort`;
2. an environment variable: `NOETA_MODEL`, `NOETA_PROVIDER`, `NOETA_BASE_URL`, `NOETA_API_KEY`,
   `NOETA_STORE`, `NOETA_EFFORT`, `NOETA_MODELS_FILE`;
3. a key in `~/.noeta/config.toml`: `model`, `provider`, `base_url`, `api_key`, `store`,
   `effort`, `models_file`;
4. for the model only: a settings file's `model`, when none of the above names one.

With no API key set, the `anthropic` provider also reads `ANTHROPIC_API_KEY` and
`openai_compat` reads `OPENAI_API_KEY`; `openai_responses` reads no variable of its own. A
missing key stops the start with a message naming
where to set one. The store defaults to `sqlite:///~/.noeta/tasks.db`.

A complete `config.toml` might look like this:

```toml
# ~/.noeta/config.toml
model = "claude-sonnet-5-5"
provider = "anthropic"            # or openai_compat, openai_responses, or a table's name
effort = "high"
# store = "postgres://noeta:…@db.internal/noeta"

[providers.deepseek]              # more providers; each model goes to the one that serves it
family = "openai_compat"
base_url = "https://api.deepseek.com/v1"
api_key = "{env:DEEPSEEK_API_KEY}"
models = ["deepseek-flash", "deepseek-v4-pro"]

[plugins.shell]                   # a plugin's arguments, by its name
background = true
[plugins.web]
enabled = false                   # switched off for every agent

[agents.main]
add_plugins = ["my_plugin"]       # plugins added to an agent without copying its file
```

How a model id finds its provider, and how a task records that choice so every worker sends
it the same way, is in [Models and providers](../guides/models.md) and
[Providers and model routing](../how-it-works/models.md). Plugin tables are covered under
[Plugin settings](../reference/config.md#plugin-settings).

`config.toml` exists only in `~/.noeta/`. A project has no `config.toml`; what a project may
set goes in its settings files.

## Settings files (Claude Code's format)

Settings files are JSON objects in Claude Code's `settings.json` format. noeta reads them in
this order, each later one winning a key the earlier ones set:

| # | File | Whose | Read |
|---|---|---|---|
| 1 | `~/.noeta/settings.json` | yours | always |
| 2 | `<project>/.noeta/settings.json` | the project's, checked in | once the project is trusted |
| 3 | `<project>/.noeta/settings.local.json` | the project's, kept out of git | once the project is trusted |
| 4 | the `--settings` file or JSON string | yours, for one `noeta -p` run | when given |
| 5 | `managed-settings.json` in the machine's directory | the machine's administrator | always |

The machine's directory is `/etc/noeta` on Linux, `/Library/Application Support/Noeta` on
macOS and `C:\Program Files\Noeta` on Windows. `--setting-sources user,project,local` (a
`noeta -p` flag) picks which of 1–3 are read; the managed file is always read.

How the files combine:

- A plain key a later file sets wins: `model`, `agent` (the agent front ends start with),
  `outputStyle`, `language`, each `attribution` line, each `env` variable,
  `permissions.defaultMode`.
- Permission rule lists (`permissions.allow`, `deny`, `ask`) and
  `permissions.additionalDirectories` add up across files, without repeats.
- `permissions.disableBypassPermissionsMode: "disable"` in any file switches bypass mode off;
  no file switches it back on.
- `sandbox` merges list by list, but a project's files may only tighten it: they cannot turn
  it off, open more paths, hosts or commands, or set `bwrapPath` (which only the managed file
  sets).
- Hooks and MCP consent (`enabledMcpjsonServers`, `disabledMcpjsonServers`,
  `enableAllProjectMcpServers`) are read from the same file list by the hooks and MCP plugins.

**`env` comes first.** The merged `env` object is set into noeta's process before anything
else is read, as Claude Code does: a `"env": {"NOETA_MODEL": "…"}` in a settings file acts like
that environment variable, and every command, hook and MCP server noeta starts sees it.

What each settings key does is in the guides: permissions and hooks in
[Permissions and hooks](../guides/permissions.md), MCP servers in [MCP servers](../guides/mcp.md),
the sandbox in [Where commands run](../guides/sandbox.md). The keys noeta reads are listed in
[Configuration keys](../reference/config.md#settings-files).

## Trust

A cloned repository must not be able to change what your agent may do, or start a process,
by itself. So a project's own configuration loads only after you trust it:

```bash
cd ~/code/some-project
noeta trust
```

or answer "Yes, I trust this folder" when the terminal UI asks on first opening a folder ("No,
exit" quits). The trusted folders are kept in `~/.noeta/trusted.json` (`noeta/cli/trust.py`); the check is on
the exact folder noeta starts in.

| In a project | Before trust | After trust |
|---|---|---|
| `.noeta/settings.json`, `.noeta/settings.local.json` | not read | read |
| `.noeta/plugins/` (Python plugin files, Claude Code plugin folders) | not loaded, with a notice | loaded |
| `.noeta/agents/` | not loaded, with a notice | loaded |
| `.noeta/skills/`, `.agents/skills/` | not loaded, with a notice | loaded |
| `.noeta/commands/` | not loaded | loaded |
| `.noeta/prompts/` (prompt overrides) | not read, with a notice | read |
| `.noeta/mcp.json` | servers never start | a server starts only with consent in the project's own settings files: `enableAllProjectMcpServers: true` or its name in `enabledMcpjsonServers` |
| `NOETA.md`, `.noeta/NOETA.md`, `NOETA.local.md`, `.noeta/rules/`, `AGENTS.md` | read | read |

Instruction files are read either way, as Claude Code reads `CLAUDE.md`: they are text for the
model, not settings. Consent for a project's MCP servers given in your own
`~/.noeta/settings.json` does not count, so a repository that reuses a server's name cannot
start it through your consent.

The settings files and plugin folders are read for the folder a front end or server starts in.
Trusting a folder at the terminal UI's question loads its plugins, agents, skills and prompts at
once, and reads the settings files again, so the project's `.noeta/settings*.json` apply from
the next turn without a restart: permission rules, hooks, MCP servers (and the consent for its
`.noeta/mcp.json`), skills, plan mode, language. `/reload-plugins` reads them again the same way
after you edit one. What is read only at the start (`env`, `model`, `agent`, `sandbox`) is not
reapplied: when it changed, the UI says to restart noeta, and says so at every new task until
you do. Prompt overrides, skills and the permission checks look at each task's own working
directory, so on a server a task running in an untrusted directory does not get that
directory's prompts or skills.

## Instructions: `NOETA.md` and `AGENTS.md`

Project instructions work like Claude Code's `CLAUDE.md`, under noeta's names
(`noeta/plugins/instructions/files.py`):

| File | Kind |
|---|---|
| `NOETA.md` and `.noeta/rules/` in the machine's directory | the administrator's |
| `~/.noeta/NOETA.md` and `~/.noeta/rules/` | yours, every project |
| `NOETA.md` or `.noeta/NOETA.md` in each folder from the outermost parent down to the working directory | the project's |
| `AGENTS.md`, in a folder that has neither of the above | the cross-tool file |
| `NOETA.local.md` | yours for this project, kept out of git |
| `.noeta/rules/*.md` | rules, optionally limited to `paths:` globs in their frontmatter |

`@path` mentions import another file; imports nest at most four deep (the file that starts
the chain is depth 0, and a file at depth 5 is not read). An import from a project file
must stay inside the working directory or the folder of the file that started the chain.
`/init` writes a `NOETA.md` for the project. More in
[Instructions and memory](../guides/memory-and-instructions.md).

## Where else noeta looks

Everything under `~/.noeta` is noeta's home; set `NOETA_HOME` to move it.

| Path | Holds |
|---|---|
| `~/.noeta/config.toml` | model, provider, store, plugin tables |
| `~/.noeta/settings.json` | your settings (Claude Code's format); also `pluginConfigs` for Claude Code plugins |
| `~/.noeta/tasks.db` | the default sqlite store: every task's log |
| `~/.noeta/trusted.json` | trusted projects |
| `~/.noeta/agents/`, `<project>/.noeta/agents/` | agent files; a later file wins a name |
| `~/.noeta/skills/`, `~/.agents/skills/`, `<project>/.noeta/skills/`, `<project>/.agents/skills/` | skills (`~/.agents/skills` is under your home directory whatever `$NOETA_HOME` says); the project's are read in the working directory and each folder above it up to the repository's root |
| `~/.noeta/commands/`, `<project>/.noeta/commands/` | typed slash commands (Claude Code's `commands/*.md`) |
| `~/.noeta/prompts/`, `<project>/.noeta/prompts/` | prompt overrides, by prompt name |
| `~/.noeta/plugins/`, `<project>/.noeta/plugins/` | plugin files and Claude Code plugin folders |
| `~/.noeta/plugin-sources.json` | where each `noeta plugin add` install came from |
| `~/.noeta/plugin-data/<name>/` | a Claude Code plugin's `${CLAUDE_PLUGIN_DATA}` |
| `~/.noeta/mcp-auth/` | MCP sign-ins and OAuth client secrets (owner-only) |
| `~/.noeta/server-token` | the SHA-256 of `noeta serve`'s token |
| `~/.noeta/cache/models/` | providers' model listings, kept a day |
| `~/.noeta/tool-results/<task>/` | tool outputs too long to return inline |
| `~/.noeta/image-cache/<task>/` | images pasted into a prompt |
| `~/.noeta/plans/`, `~/.noeta/projects/<project>/memory/`, `~/.noeta/agent-memory/` | plan files and auto memory |
| `~/.noeta/device_id` | the random device id Claude Code also sends in `metadata.user_id` |

Several hosts must see the same `~/.noeta` (copied, or `NOETA_HOME` on shared storage) for the
same plugins and agents; which parts must be shared is in
[Run it as a server](../guides/server.md).

noeta never reads `~/.claude/`, `.claude/`, `CLAUDE.md`, `.mcp.json` or `CLAUDE_*` variables
([ADR-004](https://github.com/initxy/noeta/blob/main/docs/adr/004-noeta-reads-only-its-own-config-places.md)).
To reuse Claude Code files, copy or link them into noeta's places; the formats are the same.

## Secrets: `{env:…}` and `{keyring:…}`

Plugin arguments, channel arguments, provider tables and agent files' plugin settings may hold
a secret as a reference instead of the value:

```toml
[providers.claude]
family = "anthropic"
api_key = "{env:ANTHROPIC_API_KEY}"

[channels.chat_bridge]
token = "{keyring:chat-bridge/bot}"     # needs the keyring extra
```

- `{env:NAME}` is replaced by that environment variable when the plugin is made; an unset
  variable is an error naming where it was used.
- `{keyring:service/user}` is read from the operating system's keyring; it needs the `keyring`
  extra.
- Only the reference is stored in config files and in task logs.
- A variable a reference names becomes noeta's own credential: commands, hooks and MCP servers
  noeta starts do not inherit it. `NOETA_API_KEY`, `NOETA_STORE`, `NOETA_SERVER_TOKEN`,
  `NOETA_MCP_CLIENT_SECRET` and `NOETA_WEB_SEARCH_API_KEY` are always withheld the same way
  (`noeta/toolkit/local_env.py`).

The same references work in the top-level `api_key` and `extra_headers` of `config.toml`, so the
default provider's key need not be written in the file either.

## The machine's say: `[policy]`

An administrator can allow and block plugins by name over every other setting, in the managed
directory's `config.toml` (`noeta/cli/plugin_policy.py`):

```toml
# /etc/noeta/config.toml
[policy]
allowed_plugins = ["my_company_plugin"]   # when set, only these third-party plugins load
blocked_plugins = ["web"]                 # never load, built-in or not
```

`allowed_plugins` never blocks the plugins noeta ships; `blocked_plugins` blocks anything. A
plugin file is checked by its file name before it is imported, every plugin, channel and
Claude Code plugin by its name.
