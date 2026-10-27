---
status: active
created: 2026-10-27
branch: v1-extend
---

# Extension surface: one plugin table, MCP config, plugin commands, a real third-party plugin

## Goal

Close the gaps a review of the 1.0 layout found between "everything is a plugin" and what a user
or a host can actually do without editing noeta: set and switch plugins from config, add a plugin
to an agent without copying its file, use MCP from the terminal, reach a plugin's slash commands,
and prove a separately installed plugin package works.

## Non-goals

- Plugins as loose `.py` files (no package); a plugin is a Python distribution.
- UI for third-party plugins beyond what `ask_user` and the command list give.
- Claude Code plugin bundles (directories of skills/agents/commands/MCP config).

## Context

After the layout work: `presets.default_agents` takes one keyword per built-in plugin
(`permissions`, `skills`, `mcp`, `compaction`, `web`, `memory_root`) and switches memory and MCP
off by name; `config.toml` has no plugin settings; the CLI and TUI cannot configure MCP servers
(only ACP gets them from the editor); `Extension.commands()` is implemented by skills and read by
nothing; the third-party path is tested only with a monkeypatched entry point.

## Decisions

- One plugin table, at every level (`build_agents`, `default_agents`, `from_settings`,
  `config.toml`): per plugin name its arguments (`enabled: false` among them switches it off),
  a ready `Extension`, or `True`/`False`. Argument tables of one plugin merge key by key;
  anything else replaces.
- `add` (`[agents.<name>] add_plugins` in `config.toml`, `"*"` for every agent) appends plugins
  after an agent file's own; a plugin the agent already has is not added twice.
- The built-in agents keep memory and MCP off until the table has a setting for them
  (`OFF_UNLESS_SET`): memory writes under a directory, MCP starts processes.
- Plugin commands: `Extension.commands(ctx)` is async and takes a `CommandContext` (cwd, task
  id), since MCP prompts are listed over the network and skills depend on the workspace. A
  command either has a `template` (filled with `{args}`) or the extension's `run_command`
  returns the message text. `noeta.core.commands` lists and expands them for every front end;
  the front end's own commands win on a name, then the first extension offering it.
- The `noeta.providers` entry points are `<family>_provider`: importing the submodule
  `anthropic` rebinds the package attribute `anthropic`, so a second `registry.provider` call
  loaded a module (found while testing `from_settings`).
- MCP servers in Claude Code's `mcpServers` format: user servers from `~/.noeta/settings.json`;
  a workspace's `.mcp.json` only with Claude Code's consent keys
  (`enableAllProjectMcpServers`, `enabledMcpjsonServers`, `disabledMcpjsonServers`) — a cloned
  repository must not start processes by itself.

## Plan

- [x] One plugin table in `build_agents` / `default_agents`; `config.toml` `[plugins.<name>]`
      and `[agents.<name>] add_plugins`; `from_settings` merges front end, config and caller
- [x] MCP servers from `~/.noeta/settings.json` and consented `.mcp.json` for the CLI and TUI
- [x] Plugin slash commands reachable from the TUI and ACP (or the interface removed)
- [x] An example plugin package under `examples/`, installed and run in CI
- [ ] README / CONTEXT updated

## Acceptance criteria

- `make check` green; the request golden unchanged.
- A `config.toml` can switch a built-in plugin off, pass it arguments, and add a third-party
  plugin to `main`, with tests.
- `noeta` in a terminal starts the MCP servers of `~/.noeta/settings.json`.
- CI installs the example plugin package and an agent naming it runs its tool.

## Risks

- Project `.mcp.json` consent: reading the user's settings for consent keys is the only gate; a
  wrong default would run repository code.

## Progress log

- 2026-10-27: step 1 done; `make check` 1342 passed. Provider entry-point shadowing fixed on the
  way (`tests/core/test_registry.py`).
- 2026-10-27: step 2 done; `make check` 1345 passed. One run failed
  `test_tui_drops_a_failed_attempts_output` under load (44 s run); 6 full runs and 24 concurrent
  single runs did not reproduce it. Left as is.
- 2026-10-27: step 3 done: TUI `/help` and unknown `/x` go to plugin commands; ACP announces
  them (`available_commands_update`) and expands them; MCP prompts are commands. One full run
  stalled after 58 tests while the example package was being built alongside; 5 clean runs after
  (~23 s each).
