"""Hooks a connected client answers itself (Claude Code's SDK hook
callbacks): the client registers, per hook event, matchers that carry
callback ids, and each time such a hook fires the front end asks the client
with the callback id and the event's input, and the client's reply is read
like a command hook's JSON output.

One :class:`HookCallbacks` is shared by the front end holding the client
(it calls :meth:`register`) and the hooks plugin of that process (it
:meth:`listen` s). Only that process can reach the client. So the front end
also records in the task's config (:data:`CLIENT_HOOKS_KEY`, inherited by
its child tasks) which events and matchers have callbacks and which client
answers them (:meth:`HookCallbacks.summary`): a step another worker runs
refuses a call such a PreToolUse or PermissionRequest callback would have
judged, instead of running it unjudged. A front end a person follows takes
the record away when it reopens the task (``noeta.toolkit.interactive``).
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

__all__ = ["CLIENT_HOOKS_KEY", "HookCallbacks", "Send", "parse_callbacks"]

#: The task config key recording a client's hook callbacks:
#: ``{"client": <id>, "events": {event: [matcher, ...]}}``.
CLIENT_HOOKS_KEY = "client_hooks"

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
        #: Names this process's client in the tasks' records.
        self.client_id = uuid.uuid4().hex

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

    def summary(self) -> dict[str, Any] | None:
        """The registered hooks as a task records them
        (:data:`CLIENT_HOOKS_KEY`); ``None`` when there are none."""
        events = {event: [str(g.get("matcher") or "") for g in groups]
                  for event, groups in self.hooks.items() if groups}
        return {"client": self.client_id, "events": events} if events else None

    def answers(self, record: Any) -> bool:
        """Whether this process reaches the client a task's record names."""
        return self._send is not None and isinstance(record, Mapping) \
            and record.get("client") == self.client_id

    async def call(self, callback_id: str, payload: dict[str, Any],
                   tool_use_id: str | None) -> dict[str, Any]:
        """Ask the client; raises ``RuntimeError`` when no client listens."""
        if self._send is None:
            raise RuntimeError("no client answers hook callbacks here")
        return await self._send(callback_id, payload, tool_use_id)
