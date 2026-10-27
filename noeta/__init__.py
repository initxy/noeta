"""Noeta: durable AI agents. ``noeta.connect(url)`` is the entry point for
hosts; everything else is imported from its module (``noeta.client``,
``noeta.agent``, ``noeta.runner``, ...)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from noeta.client import Client
    from noeta.remote import RemoteClient

__all__ = ["connect"]


def connect(url: str, *, token: str | None = None, **kw: Any) -> Client | RemoteClient:
    """A client for the tasks at ``url``:

    * ``sqlite:///path`` or ``postgres://…`` — a ``Client`` on that store
      (in this process; run workers with ``noeta.runner.Runtime``);
    * ``http://…`` / ``https://…`` — a ``RemoteClient`` for a ``noeta serve``
      server (``token`` is its bearer token).

    Extra keywords go to the client's constructor."""
    if url.startswith(("http://", "https://")):
        from noeta.remote import RemoteClient

        return RemoteClient(url, token=token, **kw)
    from noeta.client import Client
    from noeta.config import open_store

    return Client(open_store(url), **kw)
