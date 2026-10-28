"""Tenants of a ``noeta serve`` server: the tokens that map a request to
one tenant (``~/.noeta/tokens.json``, owner-only, holding each token's
SHA-256 only), and ``noeta token add | list | remove``. The single server
token (``NOETA_SERVER_TOKEN`` or ``~/.noeta/server-token``) stays the
default tenant's."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import sys
import time
from collections.abc import Callable
from typing import Any

from noeta.cli import config

#: A tenant name: what a token, a tenant directory and the task rows use.
TENANT_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")


def _tokens_path() -> Any:
    return config.HOME / "tokens.json"


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def read_tokens() -> list[dict[str, Any]]:
    """The entries of the tokens file: ``sha256``, ``tenant``, ``name``,
    ``created``; none when there is no file."""
    try:
        data = json.loads(_tokens_path().read_text("utf-8"))
    except FileNotFoundError:
        return []
    entries = data.get("tokens") if isinstance(data, dict) else None
    return [e for e in entries or [] if isinstance(e, dict) and e.get("sha256")
            and isinstance(e.get("tenant"), str)]


def _write_tokens(entries: list[dict[str, Any]]) -> None:
    path = _tokens_path()
    config.HOME.mkdir(parents=True, exist_ok=True)
    fresh = path.with_name(f".tokens-{os.getpid()}.json")
    fd = os.open(fresh, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump({"tokens": entries}, fh, indent=2)
    os.replace(fresh, path)


def add_token(tenant: str, name: str = "") -> str:
    """Make a token for ``tenant`` and keep its digest; returns the token
    (shown once, never stored)."""
    if not TENANT_NAME.match(tenant):
        raise ValueError(f"a tenant name is lowercase letters, digits, '-' and '_' "
                         f"(at most 63): {tenant!r}")
    token = secrets.token_urlsafe(32)
    entries = read_tokens()
    entries.append({"sha256": _digest(token), "tenant": tenant, "name": name,
                    "created": time.time()})
    _write_tokens(entries)
    return token


def remove_tokens(prefix: str) -> int:
    """Forget every token whose digest starts with ``prefix`` (at least 8
    characters, as ``noeta token list`` shows them); how many went."""
    if len(prefix) < 8:
        raise ValueError("give at least the 8 characters noeta token list shows")
    entries = read_tokens()
    kept = [e for e in entries if not str(e["sha256"]).startswith(prefix)]
    if len(kept) != len(entries):
        _write_tokens(kept)
    return len(entries) - len(kept)


def tenant_tokens() -> Callable[[str], str | None]:
    """``token -> tenant`` from the tokens file, read again whenever the
    file changes (a token added or removed works without a restart)."""
    seen: dict[str, Any] = {"stamp": None, "table": {}}

    def lookup(token: str) -> str | None:
        try:
            st = _tokens_path().stat()
            stamp: Any = (st.st_mtime_ns, st.st_size)
        except FileNotFoundError:
            stamp = None
        if stamp != seen["stamp"]:
            seen["stamp"] = stamp
            seen["table"] = {str(e["sha256"]): str(e["tenant"]) for e in read_tokens()}
        found: str | None = seen["table"].get(_digest(token))
        return found

    return lookup


# ---------------------------------------------------------------- noeta token


def add_parser(sub: Any) -> None:
    token = sub.add_parser("token", help="add, list and remove the server's tenant tokens")
    cmds = token.add_subparsers(dest="token_command", required=True)
    add = cmds.add_parser("add", help="make a bearer token for a tenant (shown once)")
    add.add_argument("--tenant", required=True)
    add.add_argument("--name", default="", help="a note on whose token it is")
    cmds.add_parser("list", help="the tenant tokens (their digests, never the tokens)")
    remove = cmds.add_parser("remove", help="revoke tokens by the digest noeta token list shows")
    remove.add_argument("digest")


def run(args: argparse.Namespace) -> int:
    """Run ``noeta token <command>``; the exit status."""
    try:
        if args.token_command == "add":
            token = add_token(args.tenant, args.name)
            print(token)
            print(f"noeta: a token for tenant {args.tenant!r}; it is shown only now",
                  file=sys.stderr)
        elif args.token_command == "list":
            for e in read_tokens():
                when = time.strftime("%Y-%m-%d", time.localtime(float(e.get("created") or 0)))
                print(f"{str(e['sha256'])[:12]}\t{e['tenant']}\t{when}\t{e.get('name') or ''}"
                      .rstrip())
        else:
            gone = remove_tokens(args.digest)
            print(f"removed {gone} token(s)")
            return 0 if gone else 1
    except ValueError as exc:
        print(f"noeta: {exc}", file=sys.stderr)
        return 1
    return 0
