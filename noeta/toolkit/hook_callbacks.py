"""Hooks a connected client answers itself (Claude Code's SDK hook
callbacks): the client registers, per hook event, matchers that carry
callback ids, and each time such a hook fires the front end asks the client
with the callback id and the event's input, and the client's reply is read
like a command hook's JSON output.

One :class:`HookCallbacks` is shared by the front end holding the client
(it calls :meth:`register`) and the hooks plugin of that process (it
:meth:`listen` s). Only that process can reach the client: a step that
another worker runs has no such hooks, as a Claude Code session's callbacks
end with its client.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any

__all__ = ["HookCallbacks", "Send", "parse_callbacks"]

#: Asks the client: ``(callback_id, event input, tool_use_id)`` -> its reply.
Send = Callable[[str, dict[str, Any], "str | None"], Awaitable[dict[str, Any]]]
#: Told the registered hooks (Claude Code's ``hooks`` object, each hook
#: ``{"type": "callback", "callback_id", "timeout"?}``) whenever they change.
Listener = Callable[[dict[str, Any]], None]


def parse_callbacks(raw: Any) -> dict[str, Any]:
    """The ``hooks`` of a client's ``initialize`` request (``{event:
    [{"matcher"?, "hookCallbackIds": [ids], "timeout"?}]}``) as a ``hooks``
    object of callback hooks. Raises ``ValueError`` on another shape."""
    problem = ValueError("initialize: hooks must map hook events to lists of matchers, each "
                         "with a 'hookCallbackIds' list of strings and an optional string "
                         "'matcher'")
    if not isinstance(raw, Mapping):
        raise problem
    out: dict[str, Any] = {}
    for event, groups in raw.items():
        if not isinstance(groups, list):
            raise problem
        for group in groups:
            ids = group.get("hookCallbackIds") if isinstance(group, Mapping) else None
            matcher = group.get("matcher") if isinstance(group, Mapping) else None
            if not isinstance(ids, list) or not all(isinstance(i, str) and i for i in ids) \
                    or (matcher is not None and not isinstance(matcher, str)):
                raise problem
            timeout = group.get("timeout")
            hooks = [{"type": "callback", "callback_id": i,
                      **({"timeout": timeout} if timeout is not None else {})} for i in ids]
            if hooks:
                out.setdefault(str(event), []).append({"matcher": matcher or "",
                                                       "hooks": hooks})
    return out


class HookCallbacks:
    """The hooks the client registered and how to ask it."""

    def __init__(self) -> None:
        self.hooks: dict[str, Any] = {}
        self._send: Send | None = None
        self._listeners: list[Listener] = []

    def register(self, hooks: dict[str, Any], send: Send) -> None:
        """The client's hooks (:func:`parse_callbacks`), replacing any it
        registered before, and how to ask it."""
        self.hooks, self._send = hooks, send
        for listener in self._listeners:
            listener(hooks)

    def listen(self, listener: Listener) -> None:
        """Call ``listener`` with the registered hooks now and on every
        change."""
        self._listeners.append(listener)
        listener(self.hooks)

    async def call(self, callback_id: str, payload: dict[str, Any],
                   tool_use_id: str | None) -> dict[str, Any]:
        """Ask the client; raises ``RuntimeError`` when no client listens."""
        if self._send is None:
            raise RuntimeError("no client answers hook callbacks here")
        return await self._send(callback_id, payload, tool_use_id)
