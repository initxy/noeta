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

__all__ = ["SHORT_NAME_MAX", "legacy_names", "named_in", "old_name", "remember", "short_name",
           "warn_legacy"]

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
#: name -> old name, for the tools this process has offered.
_remembered: dict[str, str] = {}
_REMEMBER_MAX = 10_000


def remember(name: str, old: str) -> None:
    """Makes ``old`` one of :func:`legacy_names` for ``name``."""
    if len(_remembered) >= _REMEMBER_MAX and name not in _remembered:
        _remembered.clear()
    _remembered[name] = old


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
    if name in _remembered:
        out.append(_remembered[name])
    return tuple(n for n in dict.fromkeys(out) if n != name)


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


def named_in(names: frozenset[str] | set[str], name: str, where: str) -> bool:
    """Whether the set ``names`` (a setting listing tools) holds the tool
    ``name``, by its current name or, with a warning, by its old one."""
    if name in names:
        return True
    old = next((n for n in legacy_names(name) if n in names), None)
    if old is None:
        return False
    warn_legacy(where, old, name)
    return True
