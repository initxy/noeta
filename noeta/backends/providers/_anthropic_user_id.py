"""``metadata.user_id`` as Claude Code sends it for an API-key user: a JSON
string of a stable device id, an empty account id (an API key has no
account behind it) and the conversation's id. Its own file because the
field names are Anthropic's wire spelling (``scripts/lint-naming.py``
exempts it)."""

from __future__ import annotations

import json
import secrets
import uuid

#: The device id of a process whose host gave none: stable for the process.
_PROCESS_DEVICE_ID = secrets.token_hex(32)


def user_id(device_id: str | None, root_id: str | None) -> str:
    """The ``user_id`` value; ``root_id`` (a task tree) goes in UUID form."""
    conversation = ""
    if root_id:
        try:
            conversation = str(uuid.UUID(root_id))
        except ValueError:
            conversation = root_id
    return json.dumps({"device_id": device_id or _PROCESS_DEVICE_ID, "account_uuid": "",
                       "session_id": conversation}, separators=(",", ":"))
