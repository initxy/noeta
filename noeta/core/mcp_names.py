"""The names an MCP tool had before noeta took Claude Code's MCP naming.

noeta used to lower-case a server's alias and cut it to 32 characters, and
to cut a whole tool name to 64 characters ending in ``_`` plus 8 hex digits
of the raw tool name's sha256. Today a tool is ``mcp__{alias}__{tool}`` with
case kept and nothing cut (Claude Code's). Settings written against the old
names (permission rules, ``--disallowedTools``, hook matchers, "always
allow" grants, ``NOETA_TERMINAL_MCP_TOOLS``, ``--permission-prompt-tool``)
and the ``ToolReference`` blocks of a task begun under them still name the
old form; :func:`legacy_names` gives it, so each of those places can accept
it as another name of the same tool instead of silently matching nothing.

An old name only ever narrows. A deny or ask rule, a disallowed list and a
hook's block match by it; an allow rule, an allowed list (``allowed_tools``,
an agent's ``tools:``) and a hook's allow do not (:func:`allowed_in`): the
old naming folded case and cut aliases, so ``mcp__github`` read as an old
name also covers a server ``GitHub`` some project adds next to the user's
``github``, and which servers are connected at a call (one may be down or
not yet started) cannot tell that apart. Where an old name picks one tool
(:func:`legacy_index`), one that two tools share picks neither.

The old form is derived from the current name: where the old name was cut
and hashed, the hash is taken of the tool part as it reads today, and of the
part before a trailing disambiguation hash when it has one. That misses only
a long name whose raw form held characters made ``_`` (the old hash was of
the raw name): the MCP plugin, which knows the raw name, computes that one
(:func:`old_name`) and records it with the tool; :func:`remember` makes it
known to this process when the tool is offered.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections import OrderedDict
from collections.abc import Collection, Iterable

__all__ = ["SHORT_NAME_MAX", "allowed_in", "legacy_index", "legacy_names", "named_in",
           "old_name", "remember", "short_name", "warn_legacy", "warn_not_allowed"]

log = logging.getLogger("noeta.mcp")

_PREFIX = "mcp__"
_OLD_ALIAS_MAX = 32
_OLD_NAME_MAX = 64
_HASH_LEN = 8
_HASHED_TAIL = re.compile(r"_[0-9a-f]{8}$")


def _old_forms(alias: str, rest: str, raw: str | None = None) -> list[str]:
    prefix = f"{_PREFIX}{alias.lower()[:_OLD_ALIAS_MAX]}__"
    full = prefix + rest
    if len(full) <= _OLD_NAME_MAX:
        return [full]
    keep = _OLD_NAME_MAX - len(prefix) - _HASH_LEN - 1
    digest = hashlib.sha256((rest if raw is None else raw).encode("utf-8")).hexdigest()
    digest = digest[:_HASH_LEN]
    out = [f"{prefix}{rest[:keep]}_{digest}"]
    if _HASHED_TAIL.search(rest):
        # Told apart from a same-looking name: the hash was already there.
        out.append(f"{prefix}{rest[:-_HASH_LEN - 1][:keep]}{rest[-_HASH_LEN - 1:]}")
    return out


def old_name(alias: str, safe: str, raw: str) -> str:
    """The name noeta's old naming gave the server tool ``raw`` (``safe``
    as today's naming writes it) of the server ``alias``, not told apart
    from another."""
    return _old_forms(alias, safe, raw)[0]


#: Old names only the MCP plugin could compute (:func:`old_name`): current
#: name -> its old names, for the tools this process has offered. One table
#: for the process: an entry is a function of the server's alias and the
#: tool's raw name alone, so tasks never disagree on it; two servers of two
#: tasks giving one current name to tools whose raw names differ add both
#: old forms (each only narrows, see the module's note). The least recently
#: offered entries go first past :data:`_REMEMBER_MAX`; the MCP plugin
#: remembers its tools' each time it offers them.
_remembered: OrderedDict[str, frozenset[str]] = OrderedDict()
_REMEMBER_MAX = 10_000


def remember(name: str, old: str) -> None:
    """Makes ``old`` one of :func:`legacy_names` for ``name``."""
    _remembered[name] = _remembered.get(name, frozenset()) | {old}
    _remembered.move_to_end(name)
    while len(_remembered) > _REMEMBER_MAX:
        _remembered.popitem(last=False)


def legacy_names(name: str) -> tuple[str, ...]:
    """The names the MCP tool ``name`` had under noeta's old naming, other
    than ``name`` itself (none for a tool that is not an MCP one, or whose
    name did not change). Every ``__`` after ``mcp__`` is tried as the end
    of the server's alias, since an alias may hold ``__`` itself."""
    if not name.startswith(_PREFIX):
        return ()
    body = name[len(_PREFIX):]
    out: list[str] = []
    at = body.find("__")
    while at > 0:
        rest = body[at + 2:]
        if rest:
            out += _old_forms(body[:at], rest)
        at = body.find("__", at + 1)
    out += sorted(_remembered.get(name, ()))
    return tuple(n for n in dict.fromkeys(out) if n != name)


def legacy_index(names: Iterable[str]) -> dict[str, str]:
    """Old name -> current name, for the old names of ``names`` that name
    exactly one of them and are not the current name of another: an old
    name two tools share picks neither, whatever their order."""
    current = set(names)
    seen: dict[str, set[str]] = {}
    for name in current:
        for old in legacy_names(name):
            seen.setdefault(old, set()).add(name)
    return {old: next(iter(owners)) for old, owners in seen.items()
            if len(owners) == 1 and old not in current}


#: OpenAI's function-name limit (``^[a-zA-Z0-9_-]{1,64}$``); noeta's tool
#: names follow the Messages API's 128 (an MCP tool's can pass 64).
SHORT_NAME_MAX = 64


def short_name(name: str, limit: int = SHORT_NAME_MAX) -> str:
    """``name`` as a wire that takes at most ``limit`` characters names it:
    a longer one is cut and ends in ``_`` plus 8 hex digits of its sha256,
    the same every request, so the history keeps one name for the tool."""
    if len(name) <= limit:
        return name
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:_HASH_LEN]
    return f"{name[: limit - _HASH_LEN - 1]}_{digest}"


_warned: set[tuple[str, str, str]] = set()


def warn_legacy(where: str, written: str, name: str) -> None:
    """Logs, once per process for each ``(where, written, name)``, that
    ``written`` met the tool ``name`` only by its old name."""
    key = (where, written, name)
    if key in _warned:
        return
    _warned.add(key)
    log.warning("%s %r names the MCP tool %r only by its old name (before MCP tool names "
                "kept their case and length); it still applies, but write it with the "
                "current name", where, written, name)


def warn_not_allowed(where: str, written: str, name: str) -> None:
    """Logs, once per process for each ``(where, written, name)``, that
    ``written`` names the tool ``name`` only by its old name, which lets
    nothing through."""
    key = (f"allow:{where}", written, name)
    if key in _warned:
        return
    _warned.add(key)
    log.warning("%s %r names the MCP tool %r only by its old name (before MCP tool names "
                "kept their case and length); an old name never lets a tool through, so it "
                "is asked about or left out: write it with the current name", where, written,
                name)


def named_in(names: Collection[str], name: str, where: str) -> bool:
    """Whether ``names`` (a setting that refuses the tools it lists) holds
    the tool ``name``, by its current name or, with a warning, by its old
    one."""
    if name in names:
        return True
    old = next((n for n in legacy_names(name) if n in names), None)
    if old is None:
        return False
    warn_legacy(where, old, name)
    return True


def allowed_in(names: Collection[str], name: str, where: str) -> bool:
    """Whether ``names`` (a setting that lets through, or offers, the tools
    it lists) holds the tool ``name``: by its current name only. A listed
    old name is logged and counts for nothing (see the module's note)."""
    if name in names:
        return True
    old = next((n for n in legacy_names(name) if n in names), None)
    if old is not None:
        warn_not_allowed(where, old, name)
    return False
