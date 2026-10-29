"""What the SQL stores share: the task-table columns, the row codec and the
event / inbox encoding."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import msgspec

from noeta.core.events import Event, InboxKind
from noeta.core.store import TaskRow

#: The schema version both SQL stores write. 2: holds moved to
#: ``tree_holds`` (one holding worker per tree), and ``tasks.tenant``,
#: ``tenant_usage`` and ``snapshots`` were added. 3: ``snapshots`` keyed by
#: task and fold version. 4: ``tasks.tenant`` and ``tenant_usage`` dropped. A
#: store at a later version than this code knows is refused.
SCHEMA_VERSION = 4

TASK_COLUMNS = (
    "task_id, root_id, agent, queue, status, last_seq, created_at, updated_at, parent_id, "
    "waiting_on, ready_at, owner, owner_epoch, lease_until, inbox_seq, inbox_read_seq, "
    "end_status"
)
_TASK_NAMES = [n.strip() for n in TASK_COLUMNS.split(",")]

event_decoder = msgspec.json.Decoder(Event)
inbox_decoder = msgspec.json.Decoder(InboxKind)
encode = msgspec.json.encode


def task_row(values: Sequence[Any]) -> TaskRow:
    """A ``tasks`` row selected as :data:`TASK_COLUMNS`."""
    return TaskRow(**dict(zip(_TASK_NAMES, values, strict=True)))


def inbox_kind(body: InboxKind) -> str:
    """The ``kind`` column of an inbox item: its type tag."""
    return type(body).__struct_config__.tag  # type: ignore[return-value]
