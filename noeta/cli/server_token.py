"""The bearer token of ``noeta serve``: what a client sends, and the digest
the server keeps in ``~/.noeta/server-token``."""

from __future__ import annotations

import contextlib
import hashlib
import os
import secrets
from dataclasses import dataclass

from noeta.cli import home_dir

#: How ``~/.noeta/server-token`` starts: it keeps the token's digest only.
_DIGEST_PREFIX = "sha256:"


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def server_token() -> str | None:
    """The bearer token a client sends to ``noeta serve``:
    ``NOETA_SERVER_TOKEN``, else one an older noeta still keeps in plain text
    in ``~/.noeta/server-token`` (a server keeps only its digest there)."""
    if os.environ.get("NOETA_SERVER_TOKEN"):
        return os.environ["NOETA_SERVER_TOKEN"]
    try:
        text = (home_dir.HOME / "server-token").read_text("utf-8").strip()
    except OSError:
        return None
    return text if text and not text.startswith(_DIGEST_PREFIX) else None


def server_problem(url: str, token: str | None) -> str | None:
    """Why ``url`` cannot be driven with ``token``, in one line naming what
    to set, or ``None`` when its ``/agents`` answers."""
    import httpx

    from noeta.toolkit.http import DIRECT_LOOPBACK

    headers = {"authorization": f"Bearer {token}"} if token else {}
    try:
        with httpx.Client(timeout=15, mounts=DIRECT_LOOPBACK) as client:
            resp = client.get(f"{url.rstrip('/')}/agents", headers=headers)
    except httpx.HTTPError as exc:
        return f"cannot reach the server at {url}: {str(exc) or type(exc).__name__}"
    if resp.status_code == 401:
        if token is None:
            return (f"the server at {url} needs its bearer token: set NOETA_SERVER_TOKEN "
                    "to the token noeta serve printed when it made it")
        return (f"the server at {url} refused the token: set NOETA_SERVER_TOKEN to the "
                "token noeta serve printed when it made it")
    if resp.is_error:
        return f"the server at {url} answered {resp.status_code} to GET /agents"
    return None


@dataclass(frozen=True)
class ServerToken:
    """The server's bearer token as ``noeta serve`` holds it."""

    #: Its SHA-256 in hex (as ``noeta.host.http.token_digest`` makes it).
    sha256: str
    #: The token itself when it was made, or moved out of a plain-text file,
    #: just now: shown this once, never again.
    shown: str | None = None


def server_token_digest() -> ServerToken:
    """``NOETA_SERVER_TOKEN``'s digest, else the one kept in
    ``~/.noeta/server-token`` (owner-only). A missing file gets a new token;
    a file of an older noeta holding the token itself is rewritten to hold
    its digest. Either way the token is :attr:`ServerToken.shown`."""
    if os.environ.get("NOETA_SERVER_TOKEN"):
        return ServerToken(_digest(os.environ["NOETA_SERVER_TOKEN"]))
    path = home_dir.HOME / "server-token"
    try:
        text = path.read_text("utf-8").strip()
    except OSError:
        text = ""
    if text.startswith(_DIGEST_PREFIX):
        return ServerToken(text.removeprefix(_DIGEST_PREFIX))
    value = text or secrets.token_urlsafe(32)
    home_dir.ensure_home()
    fresh = path.with_name(f".server-token-{os.getpid()}")
    with contextlib.suppress(FileNotFoundError):
        fresh.unlink()
    fd = os.open(fresh, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(_DIGEST_PREFIX + _digest(value) + "\n")
    os.replace(fresh, path)
    return ServerToken(_digest(value), shown=value)
