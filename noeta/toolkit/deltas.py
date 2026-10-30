"""Telling the model that a list it was given changed mid-conversation
(deferred tools, agent types, MCP server instructions), as Claude Code's
delta notes do: what joined, what left, never the whole list again. Each
plugin records what it last announced in the task log, so the next note,
on any worker, is computed against the same state."""

from __future__ import annotations

from collections.abc import Iterable, Mapping

__all__ = ["AMBIENT_NOTE", "changed"]

#: Closes a note that says something left (Claude Code's).
AMBIENT_NOTE = ("This is background information: do not bring it up with the user unless they "
                "ask or it bears directly on their request.")


def changed(before: Mapping[str, str] | Iterable[str],
            now: Mapping[str, str] | Iterable[str]) -> tuple[list[str], list[str]]:
    """``(joined, left)``: the keys of ``now`` that ``before`` lacks or
    holds with another value, in ``now``'s order, and the keys of
    ``before`` that ``now`` lacks, in ``before``'s order. Plain name lists
    compare by name only."""
    old = before if isinstance(before, Mapping) else dict.fromkeys(before, "")
    new = now if isinstance(now, Mapping) else dict.fromkeys(now, "")
    joined = [k for k, v in new.items() if k not in old or old[k] != v]
    left = [k for k in old if k not in new]
    return joined, left
