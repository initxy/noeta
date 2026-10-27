"""Noeta: durable AI agents. ``noeta.connect(url)`` is the entry point for
hosts; everything else is imported from its module (``noeta.core.client``,
``noeta.core.agent``, ``noeta.core.runner``, ...)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from noeta.apps.remote import RemoteClient
    from noeta.core.client import Client

__all__ = ["connect"]


def connect(url: str, *, token: str | None = None, **kw: Any) -> Client | RemoteClient:
    """A client for the tasks at ``url``:

    * ``sqlite:///path`` or ``postgres://…`` — a ``Client`` on that store
      (in this process; run workers with ``noeta.core.runner.Runtime``);
    * ``http://…`` / ``https://…`` — a ``RemoteClient`` for a ``noeta serve``
      server (``token`` is its bearer token).

    Extra keywords go to the client's constructor."""
    if url.startswith(("http://", "https://")):
        from noeta.apps.remote import RemoteClient

        return RemoteClient(url, token=token, **kw)
    from noeta.apps.config import open_store
    from noeta.core.client import Client

    return Client(open_store(url), **kw)
