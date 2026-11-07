"""``metadata.user_id``: this machine's random id (``~/.noeta/device_id``),
or one per process when the host gave none. A plain opaque string, not Claude
Code's JSON of device, account and session ids: noeta speaks the protocol, it
does not present itself as Claude Code."""

from __future__ import annotations

import secrets

#: The device id of a process whose host gave none: stable for the process.
_PROCESS_DEVICE_ID = secrets.token_hex(32)


def user_id(device_id: str | None, root_id: str | None = None) -> str:
    """The ``user_id`` value. ``root_id`` is accepted for callers that pass
    it and is not sent."""
    del root_id
    return device_id or _PROCESS_DEVICE_ID
