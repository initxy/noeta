"""The paths a shell command may write, read or remove whole, for the
``Edit`` and ``Read`` deny rules (Claude Code's shell path check).

Read off the command leniently (:func:`noeta.toolkit.shell_syntax.parse`
with ``lenient``): comments, heredocs, subshells, brace groups and the
commands inside substitutions are all looked at, since these paths only
ever refuse. Each relative path is resolved under every directory the
command may be in by then (a ``cd`` may fail, so the old one stays
possible; one bash would expand leaves only absolute paths judged). A
word bash expands is not judged; a glob is kept for the caller to expand.
Pure: nothing is read from disk here.
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass

from noeta.plugins.permissions.scope import Scope
from noeta.plugins.permissions.shell_paths import command_paths
from noeta.toolkit.shell_syntax import Command, Word, harmless_redirect, parse

__all__ = ["DenyPaths", "Target", "deny_paths"]


@dataclass(frozen=True)
class Target:
    """An absolute path; ``glob``: bash expands it (judge its matches)."""

    path: str
    glob: bool = False


@dataclass(frozen=True)
class DenyPaths:
    writes: tuple[Target, ...] = ()
    reads: tuple[Target, ...] = ()
    #: Paths removed or moved whole: a deny-rule path under one changes.
    trees: tuple[Target, ...] = ()


#: Redirections that write their target, and those that read it.
_OUT = frozenset({">", ">>", ">|", "&>", "&>>", "<>", ">&"})
_IN = frozenset({"<", "<&"})
_DEVICES = frozenset({"/dev/null", "/dev/stdin", "/dev/stdout", "/dev/stderr", "/dev/tty"})


def deny_paths(command: str, scope: Scope) -> DenyPaths | None:
    """What ``command`` may touch; ``None`` when even a lenient reading
    cannot tell its commands apart (unbalanced quotes)."""
    parsed = parse(command, lenient=True)
    if parsed.opaque:
        return None
    writes: list[Target] = []
    reads: list[Target] = []
    trees: list[Target] = []
    cwds: list[str] | None = [scope.cwd]
    for cmd in parsed.commands:
        found = command_paths(cmd.words)
        out = [r.target for r in cmd.redirects if r.op in _OUT and not harmless_redirect(r)]
        inp = [r.target for r in cmd.redirects if r.op in _IN and not harmless_redirect(r)
               and r.target.text not in _DEVICES]
        here = [scope.at(_moved(c, found.chdir)) for c in cwds] if cwds is not None else None
        writes += _targets([*found.writes, *(w for w in out if w.text not in _DEVICES)],
                           here, scope)
        reads += _targets([*found.reads, *inp], here, scope)
        trees += _targets(found.trees, here, scope)
        cwds = _next_cwds(cmd, cwds, scope)
    return DenyPaths(*(tuple(dict.fromkeys(x)) for x in (writes, reads, trees)))


def _moved(cwd: str, chdir: str | None) -> str:
    return cwd if chdir is None else posixpath.normpath(posixpath.join(cwd, chdir))


def _targets(words: list[Word], here: list[Scope] | None, scope: Scope) -> list[Target]:
    out: list[Target] = []
    for w in words:
        if w.expands or w.brace or not w.text or (
                w.tilde and not (w.text == "~" or w.text.startswith("~/"))):
            continue
        if here is None:
            if w.tilde or posixpath.isabs(w.text):
                out.append(Target(scope.absolute(w.text), w.glob))
            continue
        out += [Target(s.absolute(w.text), w.glob) for s in here]
    return out


#: More directories than this a command may be in: treated as unknown.
_MAX_CWDS = 64


def _next_cwds(cmd: Command, cwds: list[str] | None, scope: Scope) -> list[str] | None:
    """Where later commands may run after ``cmd`` (``None``: unknown). A
    ``cd`` / ``pushd`` may fail, so the old directories stay possible; a
    ``popd`` or a target bash expands makes it unknown."""
    argv = [w.text for w in cmd.words]
    if argv[:1] == ["popd"]:
        return None
    if cwds is None or argv[:1] not in (["cd"], ["pushd"]):
        return cwds
    rest = [w for w in cmd.words[1:] if not (w.text.startswith("-") and w.text != "-")]
    if not rest:
        target = scope.home if argv[0] == "cd" else None
        if target is None:
            return None
    elif len(rest) == 1 and not (rest[0].expands or rest[0].glob or rest[0].brace) and (
            not rest[0].tilde or rest[0].text == "~" or rest[0].text.startswith("~/")) \
            and rest[0].text != "-" and not re.match(r"^[+-]\d+$", rest[0].text):
        target = rest[0].text
    else:
        return None
    found = list(dict.fromkeys([*cwds, *(scope.at(c).absolute(target) for c in cwds)]))
    return found if len(found) <= _MAX_CWDS else None
