"""The bearer token of ``noeta serve``: ``NOETA_SERVER_TOKEN``, else the one
kept in ``~/.noeta/server-token`` (owner-only), which a server makes and
clients on the same machine read."""

from __future__ import annotations

import contextlib
import os
import secrets
from dataclasses import dataclass

from noeta.cli import home_dir

#: How an older noeta's ``~/.noeta/server-token`` starts: it kept the token's
#: digest only, which no client can send.
_DIGEST_PREFIX = "sha256:"


def _kept() -> str:
    """The token ``~/.noeta/server-token`` holds, or ``""`` (no file, or an
    older noeta's digest)."""
    try:
        text = (home_dir.HOME / "server-token").read_text("utf-8").strip()
    except OSError:
        return ""
    return "" if text.startswith(_DIGEST_PREFIX) else text


def server_token() -> str | None:
    """The bearer token a client sends to ``noeta serve``:
    ``NOETA_SERVER_TOKEN``, else the one in ``~/.noeta/server-token``."""
    return os.environ.get("NOETA_SERVER_TOKEN") or _kept() or None


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
    where = home_dir.HOME / "server-token"
    if resp.status_code == 401:
        if token is None:
            return (f"the server at {url} needs its bearer token: set NOETA_SERVER_TOKEN "
                    f"to the token in its {where} (the #token= of the address it prints)")
        return (f"the server at {url} refused the token: set NOETA_SERVER_TOKEN to the "
                f"token in its {where} (the #token= of the address it prints)")
    if resp.is_error:
        return f"the server at {url} answered {resp.status_code} to GET /agents"
    return None


@dataclass(frozen=True)
class ServerToken:
    """The bearer token ``noeta serve`` runs with."""

    token: str
    #: Made just now (no file, or an older noeta's digest-only one).
    made: bool = False
    #: Taken from ``NOETA_SERVER_TOKEN`` (not written anywhere).
    from_env: bool = False


def serve_token() -> ServerToken:
    """``NOETA_SERVER_TOKEN``, else the token kept in ``~/.noeta/server-token``.
    A missing file, or one an older noeta left holding only a digest, gets a
    new token, written owner-only."""
    if os.environ.get("NOETA_SERVER_TOKEN"):
        return ServerToken(os.environ["NOETA_SERVER_TOKEN"], from_env=True)
    if kept := _kept():
        return ServerToken(kept)
    value = secrets.token_urlsafe(32)
    home_dir.ensure_home()
    path = home_dir.HOME / "server-token"
    fresh = path.with_name(f".server-token-{os.getpid()}")
    with contextlib.suppress(FileNotFoundError):
        fresh.unlink()
    fd = os.open(fresh, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(value + "\n")
    os.replace(fresh, path)
    return ServerToken(value, made=True)
