"""HTTP to this machine goes direct. httpx sends a request through the
proxy the environment names, else the one macOS's system settings name,
whose exceptions (localhost among them) it does not read: a server on this
machine (a noeta server, a local model, an MCP server) was then asked
through the proxy, which answered 502. Every client noeta makes passes
``mounts=DIRECT_LOOPBACK``."""

from __future__ import annotations

#: httpx ``mounts``: the loopback names with no proxy.
DIRECT_LOOPBACK: dict[str, None] = {
    "all://localhost": None, "all://127.0.0.1": None, "all://[::1]": None}
