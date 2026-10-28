# Changelog

All notable changes to Noeta are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). The 0.x history (packages
`noeta-runtime` and `noeta-sdk`) is in `CHANGELOG.md` at tag `v0.6.42`.

## [Unreleased]

### Fixed

- A second `registry.provider()` call for a family failed once that family's adapter module
  had been imported (the entry point named a package attribute the submodule rebound).

### Changed

- **1.0 is a rewrite, one package `noeta`.** Tasks are append-only event logs with one writer
  (conditional appends, claims in the store), an inbox as the only way in, and one `Extension`
  interface for every capability. Includes the `noeta` command (terminal UI, `-p`, `acp`,
  `worker`, `serve`). Nothing from 0.x is compatible: APIs, stores and plugins.
- **Plugins are set from one table.** `config.toml` takes `[plugins.<name>]` (arguments,
  `enabled = false`) and `[agents.<name>] add_plugins`; `default_agents` takes `plugins=` and
  `add=` instead of one keyword per built-in plugin.
- **MCP servers from settings.** The terminal front ends start the `mcpServers` of
  `~/.noeta/settings.json` (Claude Code's format) and the `.mcp.json` servers it consents to.
- **Plugin slash commands** show in the TUI's `/help` and as the editor's commands over ACP;
  MCP prompts are commands.
