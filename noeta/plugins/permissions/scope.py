"""Where a call reaches, judged without running it: the working
directories, Claude Code's protected paths, shell commands that only read
inside the working directories, and the file commands ``acceptEdits`` lets
through.

Everything here is pure: paths are compared as written (made absolute and
normalised), never resolved on disk.
"""

from __future__ import annotations

import posixpath
import re
import shlex
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from noeta.toolkit.shell import subcommands

__all__ = [
    "FILE_COMMANDS",
    "PROTECTED_DIRS",
    "PROTECTED_FILES",
    "Scope",
    "is_protected",
    "shell_parts",
]

#: Directories whose contents run or configure tools later (git hooks, editor
#: tasks, agent settings): writing there asks in every mode. ``.claude``'s
#: ``skills``, ``agents`` and ``commands`` are exempt, as in Claude Code.
PROTECTED_DIRS = (".git", ".vscode", ".idea", ".claude", ".husky", ".cargo", ".devcontainer",
                  ".yarn", ".mvn", ".noeta")
#: Nested protected directories.
PROTECTED_NESTED = ((".config", "git"),)
#: Files that configure a shell, a package manager or a hook runner.
PROTECTED_FILES = frozenset(f.lower() for f in (
    ".gitconfig", ".gitmodules", ".bashrc", ".bash_profile", ".zshrc", ".zprofile", ".profile",
    ".zshenv", ".zlogin", ".zlogout", ".bash_login", ".bash_aliases", ".bash_logout", ".envrc",
    ".ripgreprc", ".mcp.json", ".claude.json", ".npmrc", ".yarnrc", ".yarnrc.yml", ".pnp.cjs",
    ".pnp.loader.mjs", ".pnpmfile.cjs", "bunfig.toml", ".bunfig.toml", ".bazelrc",
    ".bazelversion", ".bazeliskrc", ".pre-commit-config.yaml", "lefthook.yml", ".lefthook.yml",
    "lefthook.yaml", ".lefthook.yaml", "gradle-wrapper.properties", "maven-wrapper.properties",
    ".devcontainer.json", "pyrightconfig.json"))
_CLAUDE_OPEN = frozenset({"skills", "agents", "commands", "worktrees"})

#: File commands ``acceptEdits`` runs without asking when every path they
#: name is inside the working directories (Claude Code's list).
FILE_COMMANDS = frozenset({"mkdir", "touch", "rm", "rmdir", "mv", "cp", "sed"})

_SAFE_REDIRECT = re.compile(r"^(?:\d?>>?|&>)(?:/dev/null)$|^\d?>&\d$")
_REDIRECT = re.compile(r"^(?:\d?>>?|&>|<)")
_ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def is_protected(path: str) -> bool:
    """Whether writing ``path`` (absolute, normalised) asks in every mode."""
    parts = [p.lower() for p in path.split("/") if p]
    for i, part in enumerate(parts):
        if part in PROTECTED_DIRS:
            if part == ".claude" and i + 1 < len(parts) and parts[i + 1] in _CLAUDE_OPEN:
                continue
            return True
    for nested in PROTECTED_NESTED:
        n = len(nested)
        if any(tuple(parts[i:i + n]) == nested for i in range(len(parts) - n + 1)):
            return True
    return bool(parts) and parts[-1] in PROTECTED_FILES


@dataclass(frozen=True)
class Scope:
    """The working directories (the task's working directory first) and the
    home directory, all absolute."""

    dirs: tuple[str, ...]
    home: str

    @property
    def cwd(self) -> str:
        return self.dirs[0]

    def absolute(self, path: str) -> str:
        if path == "~" or path.startswith("~/"):
            path = self.home + path[1:]
        if not posixpath.isabs(path):
            path = posixpath.join(self.cwd, path)
        return posixpath.normpath(path)

    def contains(self, path: str) -> bool:
        target = self.absolute(path)
        return any(target == d or target.startswith(d.rstrip("/") + "/") for d in self.dirs)


def shell_parts(command: str) -> list[str]:
    """The subcommands of ``command`` (split at ``&&``, ``||``, ``;``, ``|``,
    ``&`` and newlines, honouring quotes)."""
    return subcommands(command) or [command]


def _argv(part: str) -> list[str] | None:
    try:
        return shlex.split(part, posix=True)
    except ValueError:
        return None


def _unsafe_text(command: str) -> bool:
    """Command substitution, process substitution, heredocs or backticks:
    what runs cannot be read off the text."""
    return any(token in command for token in ("$(", "`", "<(", ">(", "<<"))


def _strip(argv: list[str], scope: Scope) -> list[str] | None:
    """``argv`` without harmless redirections; ``None`` when one writes
    somewhere or reads outside the working directories."""
    out: list[str] = []
    i = 0
    while i < len(argv):
        word = argv[i]
        if _SAFE_REDIRECT.match(word):
            i += 1
            continue
        if word in (">", ">>", "2>", "&>", "2>>") and i + 1 < len(argv) and \
                argv[i + 1] == "/dev/null":
            i += 2
            continue
        if word == "<" and i + 1 < len(argv):
            if not scope.contains(argv[i + 1]):
                return None
            i += 2
            continue
        if _REDIRECT.match(word):
            return None
        out.append(word)
        i += 1
    return out


def _paths_inside(args: Sequence[str], scope: Scope) -> bool:
    for arg in args:
        if arg.startswith("-") or not arg:
            continue
        if "$" in arg:
            return False
        if arg.startswith(("/", "~")) or ".." in arg.split("/"):
            if not scope.contains(arg.split("*", 1)[0] or "/"):
                return False
    return True


def _find_ok(args: list[str]) -> bool:
    banned = {"-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprint", "-fprintf",
              "-fprint0", "-fls"}
    return not banned.intersection(args)


def _rg_ok(args: list[str]) -> bool:
    return not any(a in ("--pre", "--hostname-bin") or a.startswith(("--pre=", "--pre-glob"))
                   for a in args)


def _sort_ok(args: list[str]) -> bool:
    return not any(a == "-o" or a.startswith(("-o", "--output")) for a in args)


_GIT_READ = frozenset({"status", "diff", "log", "show", "rev-parse", "ls-files", "blame",
                       "describe", "shortlog", "cat-file", "ls-tree", "grep", "reflog",
                       "merge-base", "name-rev", "count-objects", "whatchanged"})


def _git_ok(args: list[str]) -> bool:
    words = [a for a in args if not a.startswith("-")]
    if any(a in ("-c", "--exec-path", "--output", "-o", "--ext-diff") or a.startswith(
            ("--output=", "--exec-path=", "-c")) for a in args):
        return False
    if not words:
        return False
    sub, rest = words[0], words[1:]
    if sub in _GIT_READ:
        return True
    if sub == "branch":
        return not any(a in ("-d", "-D", "-m", "-M", "-c", "-C", "--delete", "--move",
                             "--copy", "-f", "--force", "-u", "--set-upstream-to",
                             "--unset-upstream", "--edit-description") for a in args) \
            and (not rest or "--list" in args or "-l" in args)
    if sub == "remote":
        return rest in ([], ["-v"]) or rest[:1] == ["get-url"]
    if sub == "tag":
        return not rest or "-l" in args or "--list" in args
    if sub == "config":
        return any(a in ("--get", "--get-all", "--list", "-l", "--get-regexp") for a in args)
    if sub == "stash":
        return rest[:1] in (["list"], ["show"])
    return False


_Check = Callable[[list[str]], bool]
_ANY: _Check = lambda _args: True  # noqa: E731
#: Programs that only read (or print), with a check of their arguments.
READ_ONLY: dict[str, _Check] = {
    **{name: _ANY for name in (
        "cat", "head", "tail", "wc", "ls", "pwd", "echo", "printf", "which", "type", "file",
        "stat", "du", "df", "tree", "grep", "egrep", "fgrep", "uniq", "cut", "tr", "nl", "diff",
        "cmp", "basename", "dirname", "realpath", "readlink", "date", "whoami", "id", "uname",
        "hostname", "printenv", "true", "false", "sleep", "test", "[", "seq", "md5sum",
        "sha1sum", "sha256sum", "shasum", "column", "strings", "hexdump", "od", "comm",
        "less", "more", "jq", "yq", "cd", "pgrep", "ps", "lsof", "env", "uptime", "free")},
    "find": _find_ok,
    "rg": _rg_ok,
    "sort": _sort_ok,
    "git": _git_ok,
}


def _read_only_argv(argv: list[str], scope: Scope) -> bool:
    if not argv:
        return True
    program, args = posixpath.basename(argv[0]), argv[1:]
    if program == "xargs":
        rest = [a for a in args if not a.startswith("-")]
        return bool(rest) and _read_only_argv(rest, scope)
    if program == "env" and args:
        return False
    check = READ_ONLY.get(program)
    if check is None or not check(args):
        return False
    pathish = args[1:] if program in ("grep", "egrep", "fgrep", "rg") and args else args
    if program in ("echo", "printf", "test", "["):
        return True
    return _paths_inside(pathish, scope)


def read_only_part(part: str, scope: Scope) -> bool:
    """One subcommand only reads, and only inside the working directories."""
    argv = _argv(part)
    if argv is None:
        return False
    if any(_ENV_ASSIGN.match(w) for w in argv[:1]):
        return False
    stripped = _strip(argv, scope)
    return stripped is not None and _read_only_argv(stripped, scope)


def read_only(command: str, scope: Scope) -> bool:
    """Every part of ``command`` only reads inside the working directories
    (Claude Code runs these without asking and without the classifier)."""
    if not command.strip() or _unsafe_text(command):
        return False
    return all(read_only_part(p, scope) for p in shell_parts(command))


def file_command_paths(part: str, scope: Scope) -> list[str] | None:
    """The paths a file command (:data:`FILE_COMMANDS`) names, absolute;
    ``None`` when ``part`` is not one or its targets cannot be read off it."""
    argv = _argv(part)
    if not argv or posixpath.basename(argv[0]) not in FILE_COMMANDS:
        return None
    stripped = _strip(argv, scope)
    if stripped is None:
        return None
    program, args = posixpath.basename(stripped[0]), stripped[1:]
    if program == "sed":
        if not any(a == "-i" or a.startswith(("-i", "--in-place")) for a in args):
            return None
        words = [a for a in args if not a.startswith("-")]
        args = words[1:]
    paths = [a for a in args if not a.startswith("-")]
    if not paths or any("$" in p or (p.startswith("~") and not p.startswith("~/")) for p in paths):
        return None
    return [scope.absolute(p) for p in paths]
