"""The paths in a project whose contents run or configure tools later (git
hooks, editor tasks, shell and package-manager settings, agent settings):
the permissions plugin asks before writing them in every mode, and the
command sandbox mounts them read-only.
"""

from __future__ import annotations

__all__ = ["OPEN_SUBDIRS", "PROTECTED_DIRS", "PROTECTED_FILES", "PROTECTED_NESTED"]

#: Directories whose contents run or configure tools later. ``.noeta`` is
#: protected as Claude Code protects ``.claude``: its :data:`OPEN_SUBDIRS`
#: are exempt for the permissions plugin (not its ``agents``: an agent file
#: names plugins and their arguments, code the next session runs).
#: ``.claude`` stays protected though noeta never reads it: Claude Code may
#: run in the same project.
PROTECTED_DIRS = (".git", ".vscode", ".idea", ".claude", ".husky", ".cargo", ".devcontainer",
                  ".yarn", ".mvn", ".noeta")
#: Nested protected directories.
PROTECTED_NESTED = ((".config", "git"),)
#: Files that configure a shell, a package manager or a hook runner
#: (lower case: names compare without case).
PROTECTED_FILES = frozenset(f.lower() for f in (
    ".gitconfig", ".gitmodules", ".bashrc", ".bash_profile", ".zshrc", ".zprofile", ".profile",
    ".zshenv", ".zlogin", ".zlogout", ".bash_login", ".bash_aliases", ".bash_logout", ".envrc",
    ".ripgreprc", ".mcp.json", ".claude.json", ".npmrc", ".yarnrc", ".yarnrc.yml", ".pnp.cjs",
    ".pnp.loader.mjs", ".pnpmfile.cjs", "bunfig.toml", ".bunfig.toml", ".bazelrc",
    ".bazelversion", ".bazeliskrc", ".pre-commit-config.yaml", "lefthook.yml", ".lefthook.yml",
    "lefthook.yaml", ".lefthook.yaml", "lefthook-local.yml", ".lefthook-local.yml",
    "lefthook-local.yaml", ".lefthook-local.yaml", "gradle-wrapper.properties",
    "maven-wrapper.properties",
    ".devcontainer.json", "pyrightconfig.json"))
#: Subdirectories of a protected directory the permissions plugin lets
#: through (skills and commands are prompts, not code).
OPEN_SUBDIRS = {".noeta": frozenset({"skills", "commands", "worktrees"}),
                ".claude": frozenset({"skills", "agents", "commands", "worktrees"})}
