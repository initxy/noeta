"""Noeta: durable AI agents.

The names a host or a plugin author needs, importable from ``noeta``
(loaded on first use, so ``import noeta`` stays cheap). Everything else is
imported from its module: ``noeta.core.*`` (the kernel), ``noeta.plugins.*``,
``noeta.backends.*``, ``noeta.presets``, ``noeta.apps.*``.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from noeta.apps.remote import RemoteClient
    from noeta.core.agent import Agent
    from noeta.core.agent_files import parse_agent_file, read_agent_dir
    from noeta.core.client import Client
    from noeta.core.extension import ContextPart, Extension, Reminder, TaskView
    from noeta.core.registry import provider, store
    from noeta.core.runner import Runtime
    from noeta.core.tool import Tool, ToolContext, ToolOutput, text_output
    from noeta.presets import default_agents

#: Public name -> the module that defines it.
_EXPORTS = {
    "Agent": "noeta.core.agent",
    "Client": "noeta.core.client",
    "ContextPart": "noeta.core.extension",
    "Extension": "noeta.core.extension",
    "Reminder": "noeta.core.extension",
    "Runtime": "noeta.core.runner",
    "TaskView": "noeta.core.extension",
    "Tool": "noeta.core.tool",
    "ToolContext": "noeta.core.tool",
    "ToolOutput": "noeta.core.tool",
    "default_agents": "noeta.presets",
    "parse_agent_file": "noeta.core.agent_files",
    "provider": "noeta.core.registry",
    "read_agent_dir": "noeta.core.agent_files",
    "store": "noeta.core.registry",
    "text_output": "noeta.core.tool",
}

__all__ = [
    "Agent",
    "Client",
    "ContextPart",
    "Extension",
    "Reminder",
    "Runtime",
    "TaskView",
    "Tool",
    "ToolContext",
    "ToolOutput",
    "connect",
    "default_agents",
    "parse_agent_file",
    "provider",
    "read_agent_dir",
    "store",
    "text_output",
]


def __getattr__(name: str) -> Any:
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module 'noeta' has no attribute {name!r}")
    value = getattr(import_module(module), name)
    globals()[name] = value
    return value


def connect(url: str, *, token: str | None = None, **kw: Any) -> Client | RemoteClient:
    """A client for the tasks at ``url``:

    * ``sqlite:///path`` or ``postgres://…`` — a ``Client`` on that store
      (in this process; run workers with ``noeta.Runtime``);
    * ``http://…`` / ``https://…`` — a ``RemoteClient`` for a ``noeta serve``
      server (``token`` is its bearer token).

    Extra keywords go to the client's constructor."""
    if url.startswith(("http://", "https://")):
        from noeta.apps.remote import RemoteClient

        return RemoteClient(url, token=token, **kw)
    from noeta.core.client import Client
    from noeta.core.registry import store

    return Client(store(url), **kw)
