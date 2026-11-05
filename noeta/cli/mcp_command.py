"""``noeta mcp``: manage MCP servers as ``claude mcp`` does, in noeta's places.

    noeta mcp add [-s local|project|user] [-t stdio|sse|http|ws] [-e K=V]... [-H "K: V"]...
                  [--client-id ID] [--client-secret] [--callback-port N]
                  <name> <command-or-url> [args...]
    noeta mcp add-json [-s scope] [--client-secret] <name> '<json>'
    noeta mcp list | get <name> | remove [-s scope] <name>
    noeta mcp login [--no-browser] <name> | logout <name>

Scopes (``local`` is the default):

* ``user``: ``~/.noeta/settings.json``'s ``mcpServers``, for every project;
* ``project``: ``<project>/.noeta/mcp.json``, shared through the repository
  (a server there starts only with consent: ``enabledMcpjsonServers``);
* ``local``: ``<project>/.noeta/settings.local.json``'s ``mcpServers``, this
  project only and kept out of the repository (read once the project is
  trusted, as everything in it).

An OAuth client secret (``--client-secret``: typed, or
``NOETA_MCP_CLIENT_SECRET``) and sign-in tokens are kept with the server's
sign-in under ``~/.noeta/mcp-auth``, never in a settings file.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import getpass
import json
import os
import sys
import threading
from pathlib import Path
from typing import Any

from noeta.cli import config, home_dir
from noeta.cli.trust import project_trusted
from noeta.core import registry
from noeta.toolkit.mcp_sources import PROJECT_FILE, mcp_sources

__all__ = ["CLIENT_SECRET_ENV", "SCOPES", "add_parser", "run"]

SCOPES = ("local", "project", "user")
CLIENT_SECRET_ENV = "NOETA_MCP_CLIENT_SECRET"


def _admin() -> Any:
    """The MCP plugin's management side (``noeta.plugins.mcp.admin``),
    reached by name like any plugin."""
    try:
        return registry.load(registry.PLUGINS, "mcp").admin
    except (registry.UnknownName, AttributeError) as exc:
        raise SystemExit(f"noeta: no MCP plugin to manage servers with ({exc})") from exc


def add_parser(sub: Any) -> None:
    """Add ``mcp`` and its subcommands to the ``noeta`` parser."""
    mcp = sub.add_parser("mcp", help="configure and manage MCP servers")
    cmds = mcp.add_subparsers(dest="mcp_command", required=True)

    add = cmds.add_parser("add", help="add an MCP server")
    add.add_argument("-s", "--scope", choices=SCOPES, default="local")
    add.add_argument("-t", "--transport", choices=("stdio", "sse", "http", "ws"),
                     default="stdio")
    add.add_argument("-e", "--env", action="extend", nargs="+", default=[],
                     help="environment variables (KEY=value)")
    add.add_argument("-H", "--header", action="extend", nargs="+", default=[],
                     help='headers for remote servers ("Name: value")')
    add.add_argument("--client-id", help="OAuth client id for HTTP/SSE servers")
    add.add_argument("--client-secret", action="store_true",
                     help=f"ask for the OAuth client secret (or set {CLIENT_SECRET_ENV})")
    add.add_argument("--callback-port", type=int, help="fixed port for the OAuth redirect")
    add.add_argument("name")
    add.add_argument("target", metavar="command-or-url")
    add.add_argument("args", nargs="*")

    add_json = cmds.add_parser("add-json", help="add an MCP server from a JSON string")
    add_json.add_argument("-s", "--scope", choices=SCOPES, default="local")
    add_json.add_argument("--client-secret", action="store_true")
    add_json.add_argument("name")
    add_json.add_argument("json")

    cmds.add_parser("list", help="list the configured MCP servers and check them")
    get = cmds.add_parser("get", help="show one MCP server")
    get.add_argument("name")
    remove = cmds.add_parser("remove", help="remove an MCP server")
    remove.add_argument("-s", "--scope", choices=SCOPES)
    remove.add_argument("name")
    login = cmds.add_parser("login", help="sign in to an MCP server (OAuth)")
    login.add_argument("--no-browser", action="store_true",
                       help="print the authorization URL instead of opening a browser")
    login.add_argument("name")
    logout = cmds.add_parser("logout", help="forget an MCP server's sign-in")
    logout.add_argument("name")


# ---------------------------------------------------------------- the files


def scope_file(scope: str, cwd: str) -> Path:
    if scope == "user":
        return home_dir.HOME / "settings.json"
    if scope == "project":
        return Path(cwd) / PROJECT_FILE
    return Path(cwd) / ".noeta" / "settings.local.json"


def _read(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text("utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise SystemExit(f"noeta: cannot read {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise SystemExit(f"noeta: {path} is not a JSON object")
    return data


def _write(path: Path, data: dict[str, Any]) -> None:
    if path.parent == home_dir.HOME:
        home_dir.ensure_home()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", "utf-8")


def configured(cwd: str) -> list[tuple[str, str, dict[str, Any]]]:
    """``(scope, name, spec)`` of every server, in the order a later one of
    the same name loses: the MCP plugin's (local, project, user)."""
    scopes = {scope_file(s, cwd): s for s in SCOPES}
    order = mcp_sources([scope_file("user", cwd), scope_file("local", cwd)], [cwd])
    out = []
    for path, _ in order:
        scope = scopes[path]
        table = _read(path).get("mcpServers") or {}
        if isinstance(table, dict):
            out += [(scope, str(n), s) for n, s in table.items() if isinstance(s, dict)]
    return out


def _find(name: str, cwd: str) -> tuple[str, dict[str, Any]]:
    for scope, n, spec in configured(cwd):
        if n == name:
            return scope, spec
    raise SystemExit(f"noeta: no MCP server named {name!r}")


def _transport(name: str, spec: dict[str, Any]) -> str:
    try:
        return str(_admin().transport(name, spec))
    except ValueError as exc:
        raise SystemExit(f"noeta: {exc} (see the log)") from exc


def _consented(name: str, cwd: str) -> bool:
    """Consent counts per project: from the project's own settings files."""
    files = config.file_settings(cwd).files
    settings = [_read(f) for f in files if f.is_file()]
    if any(name in (s.get("disabledMcpjsonServers") or []) for s in settings):
        return False
    project = Path(cwd).expanduser().resolve() / ".noeta"
    own = [_read(f) for f in files if f.is_file() and f.resolve().parent == project]
    return any(s.get("enableAllProjectMcpServers") is True
               or name in (s.get("enabledMcpjsonServers") or []) for s in own)


# ---------------------------------------------------------------- commands


def _pairs(values: list[str], sep: str, what: str) -> dict[str, str]:
    out = {}
    for value in values:
        key, found, rest = value.partition(sep)
        if not found or not key.strip():
            raise SystemExit(f"noeta: invalid {what} {value!r} (expected KEY{sep.strip()}VALUE)")
        out[key.strip()] = rest.strip() if sep == ":" else rest
    return out


def _secret(asked: bool) -> str | None:
    if not asked:
        return None
    secret = os.environ.get(CLIENT_SECRET_ENV)
    if secret:
        return secret
    if not sys.stdin.isatty():
        raise SystemExit(f"noeta: no terminal to ask for the client secret; set "
                         f"{CLIENT_SECRET_ENV}")
    return getpass.getpass("OAuth client secret: ") or None


def _save(name: str, spec: dict[str, Any], scope: str, cwd: str, secret: str | None) -> None:
    transport = _transport(name, spec)
    path = scope_file(scope, cwd)
    data = _read(path)
    table = data.setdefault("mcpServers", {})
    if not isinstance(table, dict):
        raise SystemExit(f"noeta: {path}: 'mcpServers' is not an object")
    if name in table:
        raise SystemExit(f"noeta: MCP server {name!r} already exists in {scope} config")
    table[name] = spec
    _write(path, data)
    if secret is not None:
        _admin().save_client_secret(name, spec, secret, home_dir.HOME)
    print(f"Added {transport} MCP server {name} to {scope} config ({path})")
    if scope == "project" and not _consented(name, cwd):
        print(f"It starts once you consent: add {name!r} to enabledMcpjsonServers in "
              f"{scope_file('local', cwd)}")
    if scope in ("project", "local") and not project_trusted(cwd):
        print("This project is not trusted yet; run `noeta trust` here to load its settings")


def _add(args: argparse.Namespace, cwd: str) -> int:
    rest = list(args.args)
    spec: dict[str, Any]
    if args.transport == "stdio":
        spec = {"type": "stdio", "command": args.target, "args": rest,
                "env": _pairs(args.env, "=", "environment variable")}
    else:
        spec = {"type": args.transport, "url": args.target}
        headers = _pairs(args.header, ":", "header")
        if headers:
            spec["headers"] = headers
        oauth = {k: v for k, v in (("clientId", args.client_id),
                                   ("callbackPort", args.callback_port)) if v}
        if oauth:
            if args.transport == "ws":
                raise SystemExit("noeta: OAuth options apply to http and sse servers only")
            spec["oauth"] = oauth
    if args.callback_port is not None and not 0 < args.callback_port <= 65535:
        raise SystemExit("noeta: --callback-port must be an integer in [1, 65535]")
    if args.client_secret and not args.client_id:
        raise SystemExit("noeta: --client-secret needs --client-id")
    _save(args.name, spec, args.scope, cwd, _secret(args.client_secret))
    return 0


def _add_json(args: argparse.Namespace, cwd: str) -> int:
    try:
        spec = json.loads(args.json)
    except ValueError as exc:
        raise SystemExit(f"noeta: invalid JSON: {exc}") from exc
    if not isinstance(spec, dict):
        raise SystemExit("noeta: the server must be a JSON object")
    _save(args.name, spec, args.scope, cwd, _secret(args.client_secret))
    return 0


def _remove(args: argparse.Namespace, cwd: str) -> int:
    scopes = [args.scope] if args.scope else [
        s for s, n, _ in configured(cwd) if n == args.name]
    if not scopes:
        raise SystemExit(f"noeta: no MCP server named {args.name!r}")
    if len(scopes) > 1:
        raise SystemExit(f"noeta: MCP server {args.name!r} exists in several scopes "
                         f"({', '.join(scopes)}); name one with -s")
    path = scope_file(scopes[0], cwd)
    data = _read(path)
    table = data.get("mcpServers") or {}
    if args.name not in table:
        raise SystemExit(f"noeta: no MCP server named {args.name!r} in {scopes[0]} config")
    del table[args.name]
    _write(path, data)
    print(f"Removed MCP server {args.name} from {scopes[0]} config ({path})")
    return 0


def _target(spec: dict[str, Any]) -> str:
    if spec.get("command"):
        return " ".join([str(spec["command"]), *(str(a) for a in spec.get("args") or [])])
    return str(spec.get("url") or "")


def _status(scope: str, name: str, spec: dict[str, Any], cwd: str) -> str:
    # A check starts the server (a command, a headersHelper): nothing a
    # project's files name runs before the project is trusted.
    if scope in ("project", "local") and not project_trusted(cwd):
        return "project not trusted (not started; run `noeta trust` here)"
    if scope == "project" and not _consented(name, cwd):
        return "pending approval (not started)"
    status = str(asyncio.run(_admin().check(name, spec)))
    if status == "needs authentication":
        status += f" (noeta mcp login {name})"
    return status


def _list(cwd: str) -> int:
    servers = configured(cwd)
    if not servers:
        print("No MCP servers configured. Use `noeta mcp add` to add one.")
        return 0
    for scope, name, spec in servers:
        print(f"{name}: {_target(spec)} ({spec.get('type') or 'stdio'}, {scope}) - "
              f"{_status(scope, name, spec, cwd)}")
    return 0


def _get(args: argparse.Namespace, cwd: str) -> int:
    scope, spec = _find(args.name, cwd)
    print(f"{args.name}:")
    print(f"  Scope: {scope} ({scope_file(scope, cwd)})")
    print(f"  Status: {_status(scope, args.name, spec, cwd)}")
    print(f"  Type: {spec.get('type') or ('stdio' if spec.get('command') else 'http')}")
    if spec.get("command"):
        print(f"  Command: {spec['command']}")
        print(f"  Args: {' '.join(str(a) for a in spec.get('args') or [])}")
        for key, value in (spec.get("env") or {}).items():
            print(f"  Env: {key}={value}")
    else:
        print(f"  URL: {spec.get('url')}")
        for key, value in (spec.get("headers") or {}).items():
            print(f"  Header: {key}: {value}")
        if spec.get("headersHelper"):
            print(f"  Headers helper: {spec['headersHelper']}")
        if spec.get("oauth"):
            print(f"  OAuth: {json.dumps(spec['oauth'])}")
    print(f"\nTo remove this server, run: noeta mcp remove {args.name} -s {scope}")
    return 0


def _pasted() -> asyncio.Future[str]:
    """The next line typed, read on a thread that never holds up exit."""
    loop = asyncio.get_running_loop()
    line: asyncio.Future[str] = loop.create_future()

    def take(text: str) -> None:
        if not line.done():
            line.set_result(text)

    def read() -> None:
        text = sys.stdin.readline()
        with contextlib.suppress(RuntimeError):  # the loop is gone
            loop.call_soon_threadsafe(take, text)

    threading.Thread(target=read, daemon=True).start()
    return line


def _login(args: argparse.Namespace, cwd: str) -> int:
    _, spec = _find(args.name, cwd)

    def show(text: str) -> None:
        if text.startswith("http"):
            print(f"Open this URL to authorize {args.name}:\n\n  {text}\n")
            if args.no_browser:
                print("When the browser cannot reach the page it is sent back to, paste the "
                      "URL from its address bar here and press Enter.")
        else:
            print(text, file=sys.stderr)

    async def pasted() -> str:
        return await _pasted()

    try:
        asyncio.run(_admin().login(args.name, spec, home_dir.HOME, show=show,
                                   open_browser=not args.no_browser,
                                   pasted=pasted if args.no_browser else None))
    except Exception as exc:  # the plugin's own error type, not imported here
        raise SystemExit(f"noeta: sign-in to {args.name} failed: {exc}") from exc
    print(f"Authentication successful for {args.name}.")
    return 0


def _logout(args: argparse.Namespace, cwd: str) -> int:
    _, spec = _find(args.name, cwd)
    if _admin().logout(args.name, spec, home_dir.HOME):
        print(f"Removed the stored sign-in for {args.name}.")
    else:
        print(f"No stored sign-in for {args.name}.")
    return 0


def run(args: argparse.Namespace, cwd: str) -> int:
    """Run ``noeta mcp <command>``; the exit status."""
    commands = {"add": _add, "add-json": _add_json, "remove": _remove, "get": _get,
                "login": _login, "logout": _logout}
    if args.mcp_command == "list":
        return _list(cwd)
    return commands[args.mcp_command](args, cwd)
