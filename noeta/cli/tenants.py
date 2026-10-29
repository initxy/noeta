"""Tenants of a ``noeta serve`` server: the tokens that map a request to
one tenant (``~/.noeta/tokens.json``, owner-only, holding each token's
SHA-256 only), and ``noeta token add | list | remove``. The single server
token (``NOETA_SERVER_TOKEN``, or the one whose digest
``~/.noeta/server-token`` keeps) stays the default tenant's.

What a tenant is allowed lives in ``~/.noeta/tenants/<name>/config.toml``
(every worker and server reads its own copy; keep them the same)::

    workspace = "/srv/acme"      # its tasks run here or below (and here by default)
    queues = ["acme"]            # the queues its requests may use (default: "default", "acme")
    max_running = 4              # its tasks running at once; the rest wait their turn
    budget_tokens = 5_000_000    # per calendar month (UTC); past it a turn fails
    budget_usd = 50.0            #   with budget_spent
    provider = "openai_compat"   # its own model endpoint and key instead of the server's
    base_url = "https://llm.acme.example/v1"
    api_key = "{env:ACME_API_KEY}"   # or {keyring:service/user}; never the secret itself

No directory, no limits: a single-tenant server needs none of this."""

from __future__ import annotations

import argparse
import dataclasses
import hmac
import json
import os
import re
import secrets
import sys
import time
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from noeta.agents.agent_files import resolve_secrets
from noeta.cli import config, home_dir
from noeta.core.events import DEFAULT_TENANT
from noeta.core.tenants import Tenant, token_digest
from noeta.toolkit.sandbox import SandboxSettings

#: A tenant name: what a token, a tenant directory and the task rows use.
TENANT_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")


def _tokens_path() -> Any:
    return home_dir.HOME / "tokens.json"


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
    home_dir.ensure_home()
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
    entries.append({"sha256": token_digest(token), "tenant": tenant, "name": name,
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


def tenant_tokens(*, others: bool = True,
                  server_sha256: str | None = None) -> Callable[[str], str | None]:
    """``token -> tenant`` from the tokens file, read again whenever the
    file changes (a token added or removed works without a restart).
    ``others=False``: a token of a tenant other than the default one finds
    nothing (tenants are not isolated here; :func:`isolation_problem`).
    ``server_sha256``: the server token's digest, the default tenant's."""
    seen: dict[str, Any] = {"stamp": None, "table": {}}

    def lookup(token: str) -> str | None:
        digest = token_digest(token)
        if server_sha256 is not None and hmac.compare_digest(digest, server_sha256):
            return DEFAULT_TENANT
        try:
            st = _tokens_path().stat()
            stamp: Any = (st.st_mtime_ns, st.st_size)
        except FileNotFoundError:
            stamp = None
        if stamp != seen["stamp"]:
            seen["stamp"] = stamp
            seen["table"] = {str(e["sha256"]): str(e["tenant"]) for e in read_tokens()
                             if others or e["tenant"] == DEFAULT_TENANT}
        found: str | None = seen["table"].get(digest)
        return found

    return lookup


def other_tenants(places: dict[str, Any]) -> list[str]:
    """The tenants besides the default one that this machine knows of: in
    the tokens file or with a tenant directory."""
    named = {str(e["tenant"]) for e in read_tokens()} | set(places)
    return sorted(named - {DEFAULT_TENANT})


def workspace_problem(places: dict[str, Any]) -> str | None:
    """Why ``noeta serve`` may not serve the tenants this machine knows of,
    or ``None``: one besides the default has no workspace, so nothing would
    keep its tasks out of the server's directory and other tenants' work."""
    missing = [t for t in other_tenants(places)
               if places.get(t) is None or not places[t].workspace]
    if not missing:
        return None
    files = ", ".join(str(home_dir.HOME / "tenants" / t / "config.toml") for t in missing)
    return (f"tenant(s) {', '.join(missing)} have no workspace, and a tenant's tasks run only "
            f"in its own: set workspace = \"<directory>\" in {files}")


def _has_workspace(tenant: str) -> bool:
    """Whether ``tenant``'s config names a workspace directory."""
    try:
        data = tomllib.loads((home_dir.HOME / "tenants" / tenant / "config.toml")
                             .read_text("utf-8"))
    except (OSError, ValueError):
        return False
    workspace = data.get("workspace")
    return isinstance(workspace, str) and os.path.isdir(os.path.expanduser(workspace))


def isolation_problem(settings: config.Settings, cwd: str) -> str | None:
    """Why tenants' tools here would run with no boundary between them and
    this server, or ``None`` when they would not: the settings' sandbox
    confines every command (``enabled``, ``failIfUnavailable``, no
    ``allowUnsandboxedCommands``; never on macOS), or the operator accepted tenants
    separated at the API only (``[server] shared_os_user_tenants = true``)."""
    if settings.server.get("shared_os_user_tenants") is True:
        return None
    if sys.platform == "darwin":
        # Seatbelt's profile starts from (allow default): a command could still
        # ask launchd or another app over Mach to run something unsandboxed.
        return ("tenants' tools would run as this OS user: on macOS the sandbox (Seatbelt) "
                "is not a boundary between tenants (a command can ask launchd or another "
                "app to run something outside it). Run the workers in a Linux container or "
                "use a remote exec env, or accept tenants separated at the API only with "
                f"[server] shared_os_user_tenants = true in {home_dir.HOME / 'config.toml'}.")
    found = config.file_settings(cwd)
    box = SandboxSettings.from_settings(found.sandbox, found.sandbox_permissions,
                                        base_dir=cwd)
    missing = [what for what, ok in (("sandbox.enabled: true", box.enabled),
                                     ("sandbox.failIfUnavailable: true",
                                      box.fail_if_unavailable),
                                     ("sandbox.allowUnsandboxedCommands: false",
                                      not box.allow_unsandboxed)) if not ok]
    if not missing:
        return None
    return ("tenants' tools would run as this OS user with no boundary: they could read "
            f"{home_dir.HOME} (tokens, the store) and this process's environment. Set "
            f"{', '.join(missing)} in settings.json, or accept tenants separated at the API "
            f"only with [server] shared_os_user_tenants = true in "
            f"{home_dir.HOME / 'config.toml'}. (A remote exec env per task is the other "
            "boundary, set up by host code embedding noeta, not by this command.)")


@dataclass(frozen=True)
class TenantPlace:
    """One tenant directory read: its workspace and queues (for the HTTP
    host) and its limits and provider (for the workers)."""

    name: str
    workspace: str | None
    allowed: Tenant
    #: The queues its requests may create tasks on; ``None``: the default
    #: (``noeta.host.http.create_app``'s ``queues``).
    queues: tuple[str, ...] | None = None


_KEYS = {"workspace", "queues", "max_running", "budget_tokens", "budget_usd", "provider",
         "base_url", "api_key", "extra_headers"}


def load_tenants(settings: config.Settings) -> dict[str, TenantPlace]:
    """Every ``~/.noeta/tenants/<name>/config.toml``, by tenant name; secret
    references resolved now. Raises ``ValueError`` naming the file for a
    setting it does not know or cannot use."""
    root = home_dir.HOME / "tenants"
    out: dict[str, TenantPlace] = {}
    for folder in sorted(root.iterdir()) if root.is_dir() else ():
        path = folder / "config.toml"
        if not path.is_file():
            continue
        if not TENANT_NAME.match(folder.name):
            raise ValueError(f"{folder}: not a tenant name (lowercase letters, digits, - and _)")
        data = tomllib.loads(path.read_text("utf-8"))
        unknown = sorted(set(data) - _KEYS)
        if unknown:
            raise ValueError(f"{path}: unknown setting(s) {', '.join(unknown)}")
        data = resolve_secrets(data, str(path))
        out[folder.name] = _place(folder.name, path, data, settings)
    return out


def _place(name: str, path: Path, data: dict[str, Any],
           settings: config.Settings) -> TenantPlace:
    for key in ("max_running", "budget_tokens"):
        if key in data and (not isinstance(data[key], int) or data[key] < 1):
            raise ValueError(f"{path}: {key} must be a whole number of at least 1")
    if "budget_usd" in data and not isinstance(data["budget_usd"], int | float):
        raise ValueError(f"{path}: budget_usd must be a number")
    workspace = data.get("workspace")
    if workspace is not None:
        workspace = os.path.realpath(os.path.expanduser(str(workspace)))
        if not os.path.isdir(workspace):
            raise ValueError(f"{path}: workspace {workspace} is not a directory")
    queues = data.get("queues")
    if queues is not None and (not isinstance(queues, list) or not queues
                               or not all(isinstance(q, str) and q for q in queues)):
        raise ValueError(f"{path}: queues must be a non-empty list of queue names")
    headers = data.get("extra_headers", {})
    if not isinstance(headers, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in headers.items()):
        raise ValueError(f"{path}: extra_headers must be a table of header names to strings")
    provider_for = None
    if {"provider", "base_url", "api_key", "extra_headers"} & set(data):
        # Its own endpoint never borrows the server's key (nor, through the
        # provider's environment fallback, the worker's) or headers.
        own_key = data.get("api_key")
        if not isinstance(own_key, str) or not own_key:
            raise ValueError(f"{path}: a tenant with its own provider, base_url or "
                             "extra_headers sets its own api_key (\"{env:NAME}\" or "
                             "\"{keyring:service/user}\")")
        own = dataclasses.replace(settings, provider=data.get("provider", settings.provider),
                                  base_url=data.get("base_url"), api_key=own_key,
                                  extra_headers=dict(headers))
        made = own.make_provider()
        provider_for = lambda _model: made  # noqa: E731
    return TenantPlace(name=name, workspace=workspace,
                       queues=tuple(queues) if queues is not None else None, allowed=Tenant(
        max_running=data.get("max_running"), budget_tokens=data.get("budget_tokens"),
        budget_usd=float(data["budget_usd"]) if "budget_usd" in data else None,
        provider_for=provider_for))


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
            if args.tenant != DEFAULT_TENANT and not _has_workspace(args.tenant):
                path = home_dir.HOME / "tenants" / args.tenant / "config.toml"
                raise ValueError(f"tenant {args.tenant!r} has no workspace: set workspace = "
                                 f"\"<directory>\" in {path} first (a tenant's tasks run "
                                 "only in its own)")
            token = add_token(args.tenant, args.name)
            print(token)
            print(f"noeta: a token for tenant {args.tenant!r}; it is shown only now. A "
                  "noeta serve started with --no-token never reads it (every request is "
                  "the default tenant's): restart that one with a token",
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
