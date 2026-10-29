# ADR-004: noeta reads only its own configuration places, in Claude Code's formats

- Status: accepted
- Date: 2026-10-28

## Context

noeta supports Claude Code's configuration mechanisms (instruction layering, settings, hooks,
skills, agent files, MCP servers, plugin directories). The question is whose files it reads.
Reading `~/.claude/` and a project's `.claude/` would make noeta zero-setup for Claude Code
users, and would also run Claude Code's hooks, servers and permissions under a different
program, with the two tools changing each other's behavior.

## Decision

noeta reads only `~/.noeta/` (`NOETA_HOME`), a project's `.noeta/` (most of it only after
`noeta trust`), `NOETA.md` and the cross-tool `AGENTS.md`, and the machine's managed directory
(`/etc/noeta`). The mechanisms and file formats inside are Claude Code's. Claude Code's own
places (`~/.claude/`, `.claude/`, `CLAUDE.md`, `.mcp.json`, `CLAUDE_*` variables) are never
read (`noeta/cli/settings_files.py`, `noeta/plugins/instructions/files.py`). A trusted
project's MCP servers start only with Claude Code's consent keys in its own settings.

## Rationale

Two programs sharing one configuration directory means a hook or MCP server written for one
silently runs in the other, with different permission semantics; a user cannot reason about
either. Same formats, separate places keeps migration a copy, not a rewrite, and keeps each
tool's behavior its own. Gating project configuration on trust means a cloned repository
cannot configure the agent or start a process by itself.

Rejected: reading Claude Code's places directly or as a fallback (cross-tool side effects);
noeta-specific formats (users rewrite everything, and the models' priors on those files are
lost).

## Consequences

- Users copy or symlink what they want to share; texts the model sees name `NOETA.md` and
  `.noeta/` where they mean noeta's files (a listed parity deviation).
- Claude Code's environment knobs exist under the `NOETA_` prefix.
