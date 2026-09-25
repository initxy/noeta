"""The memory-index delta note — what changed since the task's index snapshot.

The index resident is frozen per task (first-write-wins, like the
environment block): a ``memory_write`` mid-task no longer re-records it, so
the semi-stable segment — and the prompt-cache prefix hanging off it — stays
put for the life of the task. The model still learns about a page written or
re-described mid-task (the 2026-08-04 D9 guarantee) through this note: ONE
line, recorded at turn intake after the goal, naming the pages created,
re-described or removed since the snapshot the resident shows.

"Since the snapshot" is read off recorded state, not disk time: the snapshot
is the index text at the resident's active hash (``TaskState.active_content``
resolved through the content store), and the live side is the store's
``index_snapshot()`` at intake. Both are deterministic over ``(ledger,
store)``, so a resumed process computes the same line from the same log and
store; and the note is recorded, so resume folds a past note back instead of
re-deriving it.

Only the index line matters: a body-only rewrite that leaves a page's index
line alone is not listed (the tier-1 resident refresh in
:mod:`~noeta.builtins.memory.impl.recall` owns bodies). A page the snapshot
listed by name only (an over-budget index) cannot show a changed description,
and when the snapshot dropped entries outright a page it does not list may be
an old one it left out, so no ``+`` is claimed for it — ``memory_search``
still finds every page, as the snapshot's own closing line says.
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Optional, Sequence

from noeta.builtins.memory.impl.index import (
    MEMORY_INDEX_NAME,
    MEMORY_KIND,
    MemoryEntries,
    _entry_line,
)
from noeta.builtins.memory.impl.store import MemoryStore
from noeta.execution.reminders import RecallView, Reminder, ReminderProvider
from noeta.protocols.content_store import ContentStore
from noeta.protocols.messages import Message, TextBlock
from noeta.protocols.values import ContentRef


__all__ = [
    "INDEX_DELTA_MAX_BYTES",
    "INDEX_DELTA_PREFIX",
    "format_index_delta",
    "index_delta_items",
    "memory_index_delta_provider",
]


#: The note's opening words — also how the provider finds its own last note
#: in the visible history.
INDEX_DELTA_PREFIX = "Memory index changed this task: "
#: Cap on the whole line, in UTF-8 bytes; the oldest changes are dropped first.
INDEX_DELTA_MAX_BYTES = 512
#: A new page's description is shown up to this many characters.
_DESCRIPTION_MAX_CHARS = 80

#: An entry line's page name: store names are word characters, ``.`` and
#: ``-`` — never a space or a colon — so it ends at the first of either.
_LINE_NAME_RE = re.compile(r"- ([^\s:]+)")


def _listed(snapshot_text: str) -> tuple[dict[str, str], bool]:
    """``({name: its index line}, whether the snapshot dropped entries)``.

    Entry lines are the ones starting ``"- "`` (no preamble line does); a
    degraded index closes on a count line starting with a digit.
    """
    lines: dict[str, str] = {}
    dropped = False
    for line in snapshot_text.split("\n"):
        match = _LINE_NAME_RE.match(line)
        if match is not None:
            lines[match.group(1)] = line
        elif line[:1].isdigit():
            dropped = True
    return lines, dropped


def _short(text: str) -> str:
    if len(text) <= _DESCRIPTION_MAX_CHARS:
        return text
    return text[: _DESCRIPTION_MAX_CHARS - 1] + "…"


def index_delta_items(
    snapshot_text: str,
    entries: MemoryEntries,
    updated: Mapping[str, str],
) -> tuple[str, ...]:
    """The changes from ``snapshot_text`` (the index the task shows) to the
    live ``entries``, newest first.

    ``+name (description)`` for a page the snapshot does not list,
    ``~name`` for one whose index line changed, ``-name`` for one the store
    no longer holds. Live pages order by ``updated`` (newest first, then
    name); removed pages have no date and come last, by name.
    """
    listed, dropped = _listed(snapshot_text)
    live: dict[str, str] = {}
    changes: dict[str, str] = {}
    for name, summary, mem_type, _keywords in entries:
        live[name] = summary
        line = _entry_line(name, summary, mem_type)
        seen = listed.get(name)
        if seen is None:
            if not dropped:
                changes[name] = f"+{name} ({_short(summary)})" if summary else f"+{name}"
        elif seen != line and seen != f"- {name}":
            changes[name] = f"~{name}"
    # Name order first, then a stable newest-first sort on the date (a
    # ``reverse`` sort keeps equal keys in their prior order).
    order = sorted(changes)
    order.sort(key=lambda n: updated.get(n, ""), reverse=True)
    removed = sorted(name for name in listed if name not in live)
    return (*(changes[n] for n in order), *(f"-{n}" for n in removed))


def format_index_delta(
    items: Sequence[str], *, max_bytes: int = INDEX_DELTA_MAX_BYTES
) -> Optional[str]:
    """The one-line note for ``items`` (``None`` when empty), at most
    ``max_bytes`` UTF-8 bytes: items past the cap — the oldest — collapse
    into a closing ``and N more``."""
    if not items:
        return None
    kept: list[str] = []
    for i, item in enumerate(items):
        rest = len(items) - i - 1
        candidate = INDEX_DELTA_PREFIX + ", ".join([*kept, item])
        # Room for the closing count is reserved whenever anything follows.
        tail = f", and {rest} more" if rest else ""
        if len((candidate + tail).encode("utf-8")) > max_bytes:
            break
        kept.append(item)
    omitted = len(items) - len(kept)
    text = INDEX_DELTA_PREFIX + ", ".join(kept)
    if omitted:
        text += f"{', ' if kept else ''}and {omitted} more"
    return text


def _message_text(message: Message) -> str:
    return "".join(b.text for b in message.content if isinstance(b, TextBlock))


def _last_note(history: Sequence[Message]) -> Optional[str]:
    for message in reversed(history):
        if message.origin == "system":
            text = _message_text(message)
            if text.startswith(INDEX_DELTA_PREFIX):
                return text
    return None


def memory_index_delta_provider(
    store: MemoryStore, content_store: ContentStore
) -> ReminderProvider:
    """The ``turn_intake`` provider recording the index delta note.

    Silent when the task has no index resident, when nothing changed since
    its snapshot, and when the same note is still in the history the model
    sees (``RecallView.visible_history``) — so an unchanged delta is said
    once, and said again only after a compaction swallows it. Bound to the
    live ``store`` (read now — legal, the note is recorded) and the host's
    ``content_store`` (to read the snapshot bytes at the active hash).
    """

    def provider(view: RecallView) -> tuple[Reminder, ...]:
        state: Any = view.task_state
        active = getattr(state, "active_content", None) or {}
        snapshot_hash = active.get(MEMORY_KIND, {}).get(MEMORY_INDEX_NAME)
        if not snapshot_hash:
            return ()
        snapshot = content_store.get(
            ContentRef(hash=snapshot_hash, size=0, media_type="text/markdown")
        ).decode("utf-8")
        entries, updated = store.index_snapshot()
        text = format_index_delta(index_delta_items(snapshot, entries, updated))
        if text is None or text == _last_note(view.visible_history):
            return ()
        return (Reminder(text=text, origin="system"),)

    return provider
