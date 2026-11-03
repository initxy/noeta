"""The task list as a fold of the task's log: every change a task tool makes
is a ``todos`` record, so any worker that folds the log has the same list.

Records (``ExtensionRecorded(extension="todos", kind, data)``):

* ``created`` — ``{"id", "subject", "description", "active_form"?, "metadata"?}``
* ``updated`` — ``{"id", "fields": {...}}``: the fields' new values
  (``subject``, ``description``, ``active_form``, ``status``, ``owner``,
  ``metadata``, already merged)
* ``linked`` — ``{"blocker", "blocked"}``: ``blocker`` must finish before
  ``blocked`` can start
* ``deleted`` — ``{"id"}``: gone, and out of every other item's links; its id
  is never given out again
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

__all__ = ["Item", "TodoState", "reduce_todos"]

#: An item's statuses, in the order the work moves through them.
STATUSES = ("pending", "in_progress", "completed")
#: The fields an ``updated`` record may set.
_UPDATABLE = frozenset({"subject", "description", "active_form", "status", "owner",
                        "metadata"})


@dataclass(frozen=True)
class Item:
    id: str
    subject: str
    description: str
    active_form: str | None = None
    status: str = "pending"
    owner: str | None = None
    blocks: tuple[str, ...] = ()
    blocked_by: tuple[str, ...] = ()
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True)
class TodoState:
    #: In id order.
    items: tuple[Item, ...] = ()
    #: The highest id ever given out (deleted ones included).
    highest: int = 0

    def get(self, item_id: str) -> Item | None:
        return next((i for i in self.items if i.id == item_id), None)

    def next_id(self) -> str:
        return str(self.highest + 1)


def _number(item_id: str) -> int:
    return int(item_id) if item_id.isdigit() else 0


def _put(items: tuple[Item, ...], item: Item) -> tuple[Item, ...]:
    kept = [i for i in items if i.id != item.id]
    return tuple(sorted([*kept, item], key=lambda i: _number(i.id)))


def reduce_todos(state: Any, kind: str, data: Any) -> TodoState:
    st = state if isinstance(state, TodoState) else TodoState()
    if not isinstance(data, dict):
        return st
    if kind == "created":
        item = Item(id=str(data["id"]), subject=str(data["subject"]),
                    description=str(data["description"]),
                    active_form=data.get("active_form"), metadata=data.get("metadata"))
        return replace(st, items=_put(st.items, item),
                       highest=max(st.highest, _number(item.id)))
    if kind == "updated":
        found = st.get(str(data["id"]))
        if found is None:
            return st
        fields = dict(data.get("fields") or {})
        return replace(st, items=_put(st.items, replace(found, **{
            k: v for k, v in fields.items() if k in _UPDATABLE})))
    if kind == "linked":
        blocker, blocked = st.get(str(data["blocker"])), st.get(str(data["blocked"]))
        if blocker is None or blocked is None:
            return st
        items = st.items
        if blocked.id not in blocker.blocks:
            items = _put(items, replace(blocker, blocks=(*blocker.blocks, blocked.id)))
        after = next(i for i in items if i.id == blocked.id)
        if blocker.id not in after.blocked_by:
            items = _put(items, replace(after, blocked_by=(*after.blocked_by, blocker.id)))
        return replace(st, items=items)
    if kind == "deleted":
        gone = str(data["id"])
        items = tuple(replace(i, blocks=tuple(b for b in i.blocks if b != gone),
                              blocked_by=tuple(b for b in i.blocked_by if b != gone))
                      for i in st.items if i.id != gone)
        return replace(st, items=items, highest=max(st.highest, _number(gone)))
    return st
