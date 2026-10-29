"""Where a call reaches, judged without running it: the working
directories, Claude Code's protected paths, shell commands that only read
inside the working directories, and the file commands ``acceptEdits`` lets
through.

Everything here is pure: paths are compared as written (made absolute and
normalised); the extension resolves symlinks before it trusts a path (each
part lists the paths it reads in :attr:`Part.reads` for that).

A command is read-only only when parsing proves it
(:mod:`noeta.toolkit.shell_syntax`): every subcommand is a program of
:data:`READ_ONLY` whose every flag is on that program's list (an unknown
flag makes it not read-only) and whose paths are inside the working
directories; nothing is redirected to or from a file; nothing expands into
words the text does not show, but for globs whose expansion can do no harm
(``ls *.py``, ``rg x -g *.py``: see :class:`read_only_programs.Spec`).
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace

from noeta.plugins.permissions.read_only_programs import (
    FIND_FLAGS,
    FIND_PATH_VALUED,
    FIND_VALUED,
    GIT_GLOBAL,
    GIT_SUBS,
    READ_ONLY,
    XARGS_FLAGS,
    XARGS_LONG,
    XARGS_TARGETS,
    XARGS_VALUED,
    Spec,
    git_operands_ok,
    parse_flags,
)
from noeta.toolkit.protected_paths import (
    OPEN_SUBDIRS,
    PROTECTED_DIRS,
    PROTECTED_FILES,
    PROTECTED_NESTED,
)
from noeta.toolkit.shell_syntax import Command, Word, harmless_redirect, parse

__all__ = [
    "FILE_COMMANDS",
    "PROTECTED_DIRS",
    "PROTECTED_FILES",
    "READ_ONLY",
    "Part",
    "Scope",
    "file_command_paths",
    "is_protected",
    "read_only",
]

#: File commands ``acceptEdits`` runs without asking when every path they
#: name is inside the working directories (Claude Code's list).
FILE_COMMANDS = frozenset({"mkdir", "touch", "rm", "rmdir", "mv", "cp", "sed"})


def is_protected(path: str) -> bool:
    """Whether writing ``path`` (absolute, normalised) asks in every mode.
    Names compare without case (case-insensitive file systems)."""
    parts = [p.lower() for p in path.split("/") if p]
    for i, part in enumerate(parts):
        if part in PROTECTED_DIRS:
            if i + 1 < len(parts) and parts[i + 1] in OPEN_SUBDIRS.get(part, ()):
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
    home directory, all absolute. ``here`` is where relative paths resolve
    (the shell's directory; default the first working directory)."""

    dirs: tuple[str, ...]
    home: str
    here: str | None = None
    #: Collects every path a read-only check accepted (absolute; a glob as
    #: written, for the caller to expand), when set.
    seen: list[str] | None = field(default=None, compare=False, hash=False, repr=False)

    @property
    def cwd(self) -> str:
        return self.here or self.dirs[0]

    def at(self, here: str) -> Scope:
        return replace(self, here=here)

    def absolute(self, path: str) -> str:
        if path == "~" or path.startswith("~/"):
            path = self.home + path[1:]
        if not posixpath.isabs(path):
            path = posixpath.join(self.cwd, path)
        return posixpath.normpath(path)

    def contains(self, path: str) -> bool:
        target = self.absolute(path)
        return any(target == d or target.startswith(d.rstrip("/") + "/") for d in self.dirs)



# ---------------------------------------------------------------- parts


@dataclass(frozen=True)
class Part:
    """One subcommand: its text, whether it only reads inside the working
    directories, and, for a file command (:data:`FILE_COMMANDS`), the paths
    it writes (absolute; ``None`` when not one or they cannot be read off
    it)."""

    text: str
    read_only: bool
    writes: tuple[str, ...] | None = None
    argv: tuple[str, ...] = field(default=())
    #: The paths a read-only part reads (absolute, as written).
    reads: tuple[str, ...] = field(default=())
    #: The files its output redirections name (absolute, as written; a
    #: target bash expands is taken as it reads).
    redirects: tuple[str, ...] = field(default=())


def _path_ok(word: Word, scope: Scope, *, lead_glob: bool = False) -> bool:
    """``word`` names paths inside the working directories only.
    ``lead_glob``: a word may start with a glob (see :attr:`Spec.lead_glob`)."""
    if word.expands or word.brace or (word.lead_glob and not lead_glob):
        return False
    text = word.text
    if word.tilde and not (text == "~" or text.startswith("~/")):
        return False
    if scope.seen is not None:
        scope.seen.append(scope.absolute(text))
    if word.glob:
        text = re.split(r"[*?\[]", text, maxsplit=1)[0] or "."
        if not text.endswith("/"):
            text = posixpath.dirname(text) or "."
    return scope.contains(text)


def _text_ok(word: Word, scope: Scope) -> bool:
    """A text operand: nothing it expands to can reach outside."""
    if word.expands or word.brace or word.lead_glob:
        return False
    return not word.glob or _path_ok(word, scope)


def _check_program(spec: Spec, words: Sequence[Word], scope: Scope) -> bool:
    parsed = parse_flags(spec, words)
    if parsed is None:
        return False
    flags, operands = parsed
    for name, value in flags:
        if name in spec.path_valued and (value is None or not _path_ok(Word(value), scope)):
            return False
    if spec.max_operands is not None and len(operands) > spec.max_operands:
        return False
    if spec.operands == "none" and operands:
        return False
    if spec.check is not None and not spec.check(flags, operands):
        return False
    paths: Iterable[Word] = operands
    if spec.operands == "text":
        return all(_text_ok(w, scope) for w in operands)
    if spec.operands == "pattern":
        given = any(name in spec.pattern_flags for name, _v in flags)
        if not given and operands:
            if not _text_ok(operands[0], scope):
                return False
            paths = operands[1:]
    return all(_path_ok(w, scope, lead_glob=spec.lead_glob) for w in paths)



def _xargs_ok(words: Sequence[Word], scope: Scope) -> bool:
    i = 0
    while i < len(words) and words[i].text.startswith("-"):
        flag = words[i]
        if not flag.plain:
            return False
        if flag.text in XARGS_VALUED:
            i += 2
        elif flag.text in XARGS_FLAGS or flag.text.startswith(XARGS_LONG) or (
                flag.text[:2] in XARGS_VALUED and len(flag.text) > 2):
            i += 1
        else:
            return False
    if i >= len(words) or not words[i].plain or words[i].text not in XARGS_TARGETS:
        return False
    return _check_program(READ_ONLY[words[i].text], words[i + 1:], scope)



def _find_ok(words: Sequence[Word], scope: Scope) -> bool:
    i = 0
    while i < len(words) and words[i].text in ("-H", "-P") and words[i].plain:
        i += 1
    while i < len(words) and not (words[i].text.startswith("-") or words[i].text in "()!,"):
        if not _path_ok(words[i], scope):
            return False
        i += 1
    while i < len(words):
        word = words[i]
        if not word.plain and not (word.glob and not word.lead_glob and not word.expands
                                   and not word.brace):
            return False
        if word.text in FIND_FLAGS:
            i += 1
        elif word.text in FIND_VALUED and i + 1 < len(words):
            if not _text_ok(words[i + 1], scope):
                return False
            i += 2
        elif word.text in FIND_PATH_VALUED and i + 1 < len(words):
            if not _path_ok(words[i + 1], scope):
                return False
            i += 2
        else:
            return False
    return True


def _git_ok(words: Sequence[Word], scope: Scope) -> bool:
    i = 0
    while i < len(words) and words[i].text.startswith("-"):
        flag = words[i]
        if not flag.plain:
            return False
        if flag.text == "-C" and i + 1 < len(words):
            if not _path_ok(words[i + 1], scope):
                return False
            scope = scope.at(scope.absolute(words[i + 1].text))
            i += 2
            continue
        if flag.text not in GIT_GLOBAL:
            return False
        i += 1
    if i >= len(words) or not words[i].plain:
        return False
    sub = words[i].text
    spec = GIT_SUBS.get(sub)
    if spec is None:
        return False
    parsed = parse_flags(spec, words[i + 1:])
    if parsed is None:
        return False
    flags, operands = parsed
    for name, value in flags:
        if name in spec.path_valued and (value is None or not _path_ok(Word(value), scope)):
            return False
    if not all(_text_ok(w, scope) for w in operands):
        return False
    return git_operands_ok(sub, flags, operands)


_ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def _read_only_command(cmd: Command, scope: Scope) -> bool:
    for r in cmd.redirects:
        if not harmless_redirect(r):
            return False
    words = cmd.words
    if not words:
        return True
    head = words[0]
    if head.text == "[" and not (head.expands or head.brace or head.tilde):
        head = Word("[")  # the ``test`` builtin, not a glob
    if not head.plain or "/" in head.text or _ENV_ASSIGN.match(head.text):
        return False
    rest = words[1:]
    if head.text == "git":
        return _git_ok(rest, scope)
    if head.text == "xargs":
        return _xargs_ok(rest, scope)
    if head.text == "find":
        return _find_ok(rest, scope)
    if head.text == "cd":
        return len(rest) <= 1 and all(_path_ok(w, scope) for w in rest)
    spec = READ_ONLY.get(head.text)
    if spec is None:
        return False
    if head.text in ("test", "[") or spec.any_flags:
        return all(not (w.expands or w.brace or w.lead_glob) and (not w.glob or _path_ok(w, scope))
                   for w in rest)
    return _check_program(spec, rest, scope)


# ---------------------------------------------------------------- file commands

_SED_SCRIPT = re.compile(
    r"^\s*(?:(?:\d+|\$)(?:,(?:\d+|\$))?)?\s*(?:"
    r"s(?P<d>[^\\\n\sA-Za-z0-9])(?:\\.|(?!(?P=d)).)*(?P=d)(?:\\.|(?!(?P=d)).)*(?P=d)"
    r"[gIiMm0-9]*|d|p)\s*$", re.DOTALL)


def _sed_writes(words: Sequence[Word]) -> list[Word] | None:
    """The files ``sed -i`` edits in place, when its script only substitutes,
    deletes or prints (no ``w`` file, no ``e`` command, no script file)."""
    in_place = False
    scripts: list[str] = []
    operands: list[Word] = []
    i = 0
    while i < len(words):
        a = words[i].text
        if a == "--":
            operands += words[i + 1:]
            break
        if not a.startswith("-") or a == "-":
            operands.append(words[i])
        elif a in ("-i", "--in-place") or (a.startswith("-i") and len(a) > 2):
            suffix = a[2:]
            if "/" in suffix or "*" in suffix:
                return None
            in_place = True
        elif a.startswith("--in-place="):
            suffix = a.partition("=")[2]
            if "/" in suffix or "*" in suffix:
                return None
            in_place = True
        elif a in ("-e", "--expression"):
            i += 1
            if i >= len(words):
                return None
            scripts.append(words[i].text)
        elif a.startswith("--expression="):
            scripts.append(a.partition("=")[2])
        elif a in ("-E", "-r", "-s", "-n", "-z", "--regexp-extended", "--separate",
                   "--quiet", "--silent", "--null-data", "--posix"):
            pass
        else:
            return None
        i += 1
    if not in_place:
        return None
    if not scripts:
        if not operands:
            return None
        scripts.append(operands.pop(0).text)
    for script in scripts:
        for line in re.split(r"[;\n]", script):
            if line.strip() and not _SED_SCRIPT.match(line):
                return None
    return operands


def _file_writes(cmd: Command, scope: Scope) -> tuple[str, ...] | None:
    if any(not harmless_redirect(r) for r in cmd.redirects) or not cmd.words:
        return None
    head = cmd.words[0]
    if not head.plain or head.text not in FILE_COMMANDS:
        return None
    rest = cmd.words[1:]
    if head.text == "sed":
        found = _sed_writes(rest)
        if found is None:
            return None
        paths = found
    else:
        paths = []
        ended = False
        for w in rest:
            if not ended and w.text == "--":
                ended = True
            elif not ended and w.text.startswith("-") and w.text != "-":
                if "=" in w.text or not w.plain:
                    return None
            else:
                paths.append(w)
    if not paths:
        return None
    for w in paths:
        if not w.plain and not (w.tilde and w.text.startswith("~/") and not (
                w.expands or w.glob or w.brace)):
            return None
    return tuple(scope.absolute(w.text) for w in paths)


# ---------------------------------------------------------------- whole commands


def _next_cwds(cmd: Command, cwds: list[str] | None, scope: Scope) -> list[str] | None:
    """Where later subcommands may run after ``cmd`` (``None``: unknown).
    A ``cd`` may fail, so the old directory stays possible; one in a
    pipeline or in the background changes nothing that follows, but its
    target is not known to hold either."""
    head = cmd.argv[:1]
    if head in (["pushd"], ["popd"]):
        return None
    if head != ["cd"] or cwds is None:
        return cwds
    rest = cmd.words[1:]
    if not rest:
        target = scope.home
    elif len(rest) == 1 and rest[0].plain and rest[0].text != "-":
        target = rest[0].text
    elif len(rest) == 1 and rest[0].tilde and rest[0].text.startswith("~/") and not (
            rest[0].expands or rest[0].glob or rest[0].brace):
        target = rest[0].text
    else:
        return None
    if cmd.after in ("|", "|&", "&") or cmd.before in ("|", "|&"):
        return cwds
    return list(dict.fromkeys([*cwds, *(scope.at(c).absolute(target) for c in cwds)]))


#: Redirection operators that write their target.
_OUTPUT_REDIRECTS = frozenset({">", ">>", ">|", "&>", "&>>", "<>"})


def parts(command: str, scope: Scope) -> list[Part] | None:
    """``command``'s subcommands, judged; ``None`` when what runs cannot be
    read off the text."""
    parsed = parse(command)
    if parsed.opaque:
        return None
    out: list[Part] = []
    cwds: list[str] | None = [scope.cwd]
    for cmd in parsed.commands:
        if cwds is None:
            out.append(Part(cmd.text, False, None, tuple(cmd.argv)))
            continue
        seen: list[str] = []
        here = [replace(scope.at(c), seen=seen) for c in cwds]
        ro = all(_read_only_command(cmd, s) for s in here)
        writes: tuple[str, ...] | None = None
        found = [_file_writes(cmd, s) for s in here]
        if found and all(f is not None for f in found):
            writes = tuple(dict.fromkeys(p for f in found for p in f or ()))
        redirected = tuple(dict.fromkeys(
            s.absolute(r.target.text) for s in here for r in cmd.redirects
            if r.op in _OUTPUT_REDIRECTS and not harmless_redirect(r) and r.target.text))
        out.append(Part(cmd.text, ro, writes, tuple(cmd.argv),
                        tuple(dict.fromkeys(seen)) if ro else (), redirected))
        cwds = _next_cwds(cmd, cwds, scope)
        if cwds is not None and not all(scope.contains(c) for c in cwds):
            cwds = None
    return out


def read_only(command: str, scope: Scope) -> bool:
    """Every part of ``command`` only reads inside the working directories
    (Claude Code runs these without asking and without the classifier)."""
    if not command.strip():
        return False
    found = parts(command, scope)
    return found is not None and all(p.read_only for p in found)


def file_command_paths(part: str, scope: Scope) -> list[str] | None:
    """The paths a file command (:data:`FILE_COMMANDS`) writes, absolute;
    ``None`` when ``part`` is not one or its targets cannot be read off it."""
    found = parts(part, scope)
    if found is None or len(found) != 1 or found[0].writes is None:
        return None
    return list(found[0].writes)
