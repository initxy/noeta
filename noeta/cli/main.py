"""The ``noeta`` command.

    noeta                      the terminal UI (needs the ``tui`` extra)
    noeta -p "fix the test"    run one prompt in the current directory and print the answer;
                               Claude Code's headless flags and stream-json protocol
                               (``noeta.cli.headless``)
    noeta acp                  serve the Agent Client Protocol on stdio (editors);
                               ``--server URL`` drives a ``noeta serve`` server's tasks
    noeta worker               run tasks from the store until stopped (multi-machine)
    noeta serve                the client API over HTTP (and workers, unless --no-worker)
    noeta plugin ...           install (pinned), list and remove plugins
    noeta mcp ...              add, list, remove and sign in to MCP servers
    noeta token ...            add, list and remove the server's tenant tokens

Model and provider come from ``--model`` / ``--provider`` or the environment
(``NOETA_MODEL``, ``NOETA_PROVIDER``, ``NOETA_BASE_URL``, ``NOETA_API_KEY``)
or ``~/.noeta/config.toml``.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys
from pathlib import Path
from typing import Any

from noeta.cli import home_dir, mcp_command, plugin_command, tenants
from noeta.cli.config import Settings, load_settings
from noeta.toolkit.permission_modes import MODE_ALIASES, PERMISSION_MODES

SUBCOMMANDS = ("acp", "worker", "serve", "trust", "plugins", "plugin", "mcp", "token")

#: Where an extra is installed from (README). noeta 1.0 is not on PyPI, and
#: the bare name there may be someone else's package.
SOURCE_URL = "git+https://github.com/initxy/noeta"
#: The extras the README's install line names.
README_EXTRAS = ("tui", "images")
#: The top-level modules each optional extra installs (``pyproject.toml``).
EXTRA_MODULES = {
    "uvicorn": "server", "starlette": "server", "websockets": "server",
    "textual": "tui", "acp": "acp", "psycopg": "postgres", "psycopg_pool": "postgres",
    "PIL": "images", "keyring": "keyring",
}


def _common(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--model")
    ap.add_argument("--effort", choices=("low", "medium", "high", "xhigh", "max"))
    ap.add_argument("--permission-mode", default=None,
                    choices=(*PERMISSION_MODES, *MODE_ALIASES),
                    help="the starting permission mode (default: auto)")
    ap.add_argument("--provider", choices=("anthropic", "openai_compat", "openai_responses"))
    ap.add_argument("--base-url")
    ap.add_argument("--store", help="sqlite:///path or postgres://... (default ~/.noeta/tasks.db)")
    ap.add_argument("--cwd", default=None,
                    help="working directory (default: current); with --server, a directory "
                         "on the server (default: the server's own, or the tenant's workspace)")
    ap.add_argument("--system-prompt", help="replace the main agent's system prompt")
    ap.add_argument("--append-system-prompt",
                    help="add text after the main agent's system prompt")


def _parser() -> argparse.ArgumentParser:
    """The subcommands (``noeta acp`` …)."""
    ap = argparse.ArgumentParser(prog="noeta", description="Durable AI agents.")
    _common(ap)
    sub = ap.add_subparsers(dest="command")
    ac = sub.add_parser("acp", help="serve the Agent Client Protocol on stdio")
    ac.add_argument("--server", metavar="URL",
                    help="drive the tasks of a noeta server (token: NOETA_SERVER_TOKEN) "
                         "instead of running them here")
    # The top-level --cwd and --model, also taken after "acp" (the later wins).
    ac.add_argument("--cwd", default=argparse.SUPPRESS, help="as noeta --cwd")
    ac.add_argument("--model", default=argparse.SUPPRESS, help="as noeta --model")
    w = sub.add_parser("worker", help="run tasks from the store until stopped")
    w.add_argument("--queue", action="append", default=None)
    w.add_argument("--concurrency", type=int, default=8)
    w.add_argument("--lease", type=float, default=None, metavar="SECONDS",
                   help="how long a claim lasts without renewal (default 30); a dead "
                        "worker's tasks wait about this long before another takes them")
    w.add_argument("--snapshot-every", type=int, default=None, metavar="EVENTS",
                   help="write a task's fold snapshot at most every this many events "
                        "(default 100; 0 never)")
    w.add_argument("--server-dir", action="append", default=[], metavar="PATH",
                   help="a directory of the server's to hide from other tenants' tasks, "
                        "besides this one and [server] dirs (repeatable)")
    w.add_argument("--allow-bypass-permissions", action="store_true",
                   help="let tasks run in bypassPermissions")
    sv = sub.add_parser("serve", help="the client API over HTTP")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8642)
    sv.add_argument("--no-worker", action="store_true", help="API only; workers run elsewhere")
    sv.add_argument("--no-token", action="store_true",
                    help="serve a loopback address without a bearer token")
    sv.add_argument("--allow-host", action="append", default=[], metavar="NAME",
                    help="a Host header name the server answers (repeatable); without "
                         "it Host is checked only on a loopback bind")
    sv.add_argument("--allow-origin", action="append", default=[], metavar="ORIGIN",
                    help="a web origin (https://host:port) whose pages may call the API")
    sv.add_argument("--allow-bypass-permissions", action="store_true",
                    help="let requests start or switch tasks into bypassPermissions")
    sv.add_argument("--allow-root", action="append", default=[], metavar="PATH",
                    help="a directory besides the server's own tasks may run in")
    sub.add_parser("trust", help="load this project's plugins, agents, prompts and skills")
    sub.add_parser("plugins", help="list the plugins and where each comes from")
    plugin_command.add_parser(sub)
    mcp_command.add_parser(sub)
    tenants.add_parser(sub)
    return ap


def _front_parser() -> argparse.ArgumentParser:
    """The terminal UI and ``-p`` (Claude Code's flags)."""
    from noeta.cli.headless import add_arguments

    ap = argparse.ArgumentParser(
        prog="noeta", description="Durable AI agents. Subcommands: " + ", ".join(SUBCOMMANDS))
    _common(ap)
    ap.add_argument("-r", "--resume", nargs="?", const="", default=None,
                    help="resume a task (id; the TUI offers a list without one)")
    ap.add_argument("--server", metavar="URL",
                    help="TUI: drive the tasks of a noeta server (token: NOETA_SERVER_TOKEN)")
    add_arguments(ap)
    return ap


def _subcommand(argv: list[str]) -> bool:
    """Whether ``argv`` names a subcommand (as its first word that is not
    an option or an option's value)."""
    takes_value = {"--model", "--effort", "--permission-mode", "--provider", "--base-url",
                   "--store", "--cwd", "--system-prompt", "--append-system-prompt"}
    i = 0
    while i < len(argv):
        word = argv[i]
        if word in takes_value:
            i += 2
            continue
        if word.startswith("-"):
            if "=" not in word and word not in takes_value:
                return False  # a flag only the front ends know
            i += 1
            continue
        return word in SUBCOMMANDS
    return False


def _plugins(cwd: str) -> None:
    from noeta.cli.config import claude_plugins, load_plugin_files
    from noeta.core import registry

    notices = load_plugin_files(cwd)
    for name, source in registry.sources(registry.PLUGINS).items():
        print(f"{name}\t{source}")
    for name, source in registry.sources(registry.CHANNELS).items():
        print(f"{name}\t{source}\t(channel)")
    found, more = claude_plugins(cwd)
    for plugin in found:
        print(f"{plugin.name}\t{plugin.root}\t(Claude Code plugin {plugin.version})".rstrip())
    notices += more
    for path, why in registry.broken_plugin_files().items():
        print(f"(failed) {path}: {why}", file=sys.stderr)
    for notice in notices:
        print(notice, file=sys.stderr)


def _settings(args: argparse.Namespace, *, need_model: bool = True,
              **layers: Any) -> Settings:
    """The settings; ``need_model``: exit when none names a model (a front
    end attached to a server uses the server's)."""
    s = load_settings(cwd=os.path.abspath(args.cwd or os.getcwd()),
                      model=args.model, provider=args.provider, base_url=args.base_url,
                      store=args.store, effort=args.effort, **layers)
    if need_model and not s.model:
        sys.exit("noeta: no model configured (--model or NOETA_MODEL)")
    if args.system_prompt is not None:
        s.prompts["system/main"] = args.system_prompt
    if args.append_system_prompt is not None:
        s.prompts["system/main.append"] = args.append_system_prompt
    return s


def _app(s: Settings, **kw: Any) -> Any:
    from noeta.cli.config import from_settings

    kw.setdefault("cwd", os.getcwd())
    return from_settings(s, **kw)


def _tenants(s: Settings) -> dict[str, tenants.TenantPlace]:
    try:
        return tenants.load_tenants(s)
    except (ValueError, OSError) as exc:
        sys.exit(f"noeta: {exc}")


def _workspaces(places: dict[str, tenants.TenantPlace]) -> dict[str, str]:
    return {name: p.workspace for name, p in places.items() if p.workspace}


def _server_dirs(s: Settings, cwd: str, extra: list[str]) -> list[str]:
    """The server's own directories, hidden from other tenants' tasks:
    ``cwd``, ``extra`` and ``[server] dirs`` in ``config.toml`` (for
    workers started apart from ``noeta serve``)."""
    dirs = s.server.get("dirs", [])
    if not isinstance(dirs, list) or not all(
            isinstance(d, str) and os.path.isabs(os.path.expanduser(d)) for d in dirs):
        sys.exit("noeta: config.toml: [server] dirs is a list of absolute paths")
    return list(dict.fromkeys([cwd, *(os.path.abspath(e) for e in extra),
                               *(os.path.normpath(os.path.expanduser(d)) for d in dirs)]))


def _check_isolation(s: Settings, cwd: str,
                     places: dict[str, tenants.TenantPlace]) -> str | None:
    """Why tools of tenants other than the default one may not run here, or
    ``None``; exits when such tenants are configured and may not. A task of
    another tenant such a worker still claims fails its turn with the reason
    (the permissions plugin's ``tenant_refusal``)."""
    problem = tenants.isolation_problem(s, cwd)
    others = tenants.other_tenants(places)
    if problem is not None and others:
        sys.exit(f"noeta: tenant(s) {', '.join(others)} configured, but {problem}")
    return problem


def _allowed_hosts(args: argparse.Namespace) -> list[str] | None:
    """The ``Host`` names ``noeta serve`` answers: the loopback names on a
    loopback bind, plus ``--allow-host``; any on another bind without it."""
    from noeta.host.http import LOOPBACK_HOSTS

    base = list(LOOPBACK_HOSTS) if args.host in LOOPBACK_HOSTS else []
    return [*base, *args.allow_host] if base or args.allow_host else None


async def _serve(s: Settings, args: argparse.Namespace, cwd: str) -> None:
    import uvicorn

    from noeta.cli.home_dir import HOME
    from noeta.cli.server_token import server_token_digest
    from noeta.host.http import create_app, redact_token_logs

    host, with_worker = args.host, not args.no_worker
    places = _tenants(s)
    if args.no_token:
        # Every request would be the default tenant's: refuse rather than
        # ignore the tenants' tokens.
        known = sorted({str(e["tenant"]) for e in tenants.read_tokens()} | set(places))
        if known:
            sys.exit(f"noeta: --no-token serves every request as the default tenant, but "
                     f"tenant(s) {', '.join(known)} are configured ({HOME / 'tokens.json'} "
                     f"or {HOME / 'tenants'}); serve with a token")
    if (unplaced := tenants.workspace_problem(places)) is not None:
        sys.exit(f"noeta: {unplaced}")
    server = None if args.no_token else server_token_digest()
    if server is not None and server.shown is not None:
        print(f"noeta: bearer token (shown only now; {HOME / 'server-token'} keeps its "
              f"SHA-256): {server.shown}", file=sys.stderr)
    elif server is not None and not os.environ.get("NOETA_SERVER_TOKEN"):
        print(f"noeta: bearer token: the one shown when {HOME / 'server-token'} was made "
              "(delete that file and restart for a new one)", file=sys.stderr)
    # The plugin refuses bypassPermissions itself, whatever way it is asked.
    permissions: dict[str, Any] = {} if args.allow_bypass_permissions else {
        "disable_bypass_mode": True}
    # The default tenant's workspace: hidden from other tenants' tasks.
    permissions["server_dirs"] = _server_dirs(s, cwd, args.allow_root)
    problem = _check_isolation(s, cwd, places) if with_worker else None
    isolated = problem is None
    if problem is not None:
        permissions["tenant_refusal"] = problem
    app = _app(s, cwd=cwd, with_channels=True,
               tenants={name: p.allowed for name, p in places.items()},
               tenant_workspaces=_workspaces(places), permissions=permissions)
    await app.start(worker=with_worker)
    api = create_app(app.client, auth=server is not None,
                     tokens=tenants.tenant_tokens(
                         others=isolated, server_sha256=server.sha256 if server else None),
                     deltas=app.deltas, agents=app.runtime.agents,
                     app=app if with_worker else None,
                     allowed_hosts=_allowed_hosts(args),
                     origins=args.allow_origin,
                     allow_bypass=args.allow_bypass_permissions, cwd=cwd,
                     roots=[os.path.abspath(r) for r in args.allow_root],
                     workspaces=_workspaces(places),
                     queues={name: p.queues for name, p in places.items()
                             if p.queues is not None})
    config = uvicorn.Config(api, host=host, port=args.port)
    redact_token_logs()
    try:
        await uvicorn.Server(config).serve()
    finally:
        await app.stop()


async def _worker(s: Settings, queues: list[str], concurrency: int,
                  lease: float | None = None, snapshot_every: int | None = None, *,
                  server_dirs: list[str] | None = None, allow_bypass: bool = False) -> None:
    from noeta.host.channels import run_channels

    kw: dict[str, Any] = {"lease_seconds": lease} if lease else {}
    places = _tenants(s)
    cwd = os.getcwd()
    problem = _check_isolation(s, cwd, places)
    # As noeta serve: bypassPermissions refused in the plugin itself, and
    # the server's directories hidden from other tenants' tasks.
    permissions: dict[str, Any] = {} if allow_bypass else {"disable_bypass_mode": True}
    permissions["server_dirs"] = _server_dirs(s, cwd, server_dirs or [])
    if problem is not None:
        permissions["tenant_refusal"] = problem
    kw["permissions"] = permissions
    kw["tenants"] = {name: p.allowed for name, p in places.items()}
    kw["tenant_workspaces"] = _workspaces(places)
    if snapshot_every is not None:
        kw["snapshot_every"] = snapshot_every
    app = _app(s, queues=tuple(queues), concurrency=concurrency, with_channels=True, **kw)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    await asyncio.gather(app.runtime.serve(stop), run_channels(app.channels, app.client, stop))
    await app.store.close()


def missing_extra(exc: BaseException) -> str | None:
    """The extra whose module ``exc`` (or an exception it was raised from)
    failed to import, or ``None`` when it is not an extra's module."""
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if isinstance(cur, ModuleNotFoundError) and cur.name:
            extra = EXTRA_MODULES.get(cur.name.split(".", 1)[0])
            if extra is not None:
                return extra
        cur = cur.__cause__ or cur.__context__
    return None


def main(argv: list[str] | None = None) -> None:
    try:
        _main(list(sys.argv[1:] if argv is None else argv))
    except ImportError as exc:
        extra = missing_extra(exc)
        if extra is None:
            raise
        # A uv tool reinstall keeps only the extras it names.
        wanted = ",".join(dict.fromkeys((*README_EXTRAS, extra)))
        sys.exit(f"noeta: this needs the {extra} extra, which is not installed: "
                 f"pip install 'noeta[{extra}] @ {SOURCE_URL}' (or, for a uv tool install, "
                 f"uv tool install --reinstall 'noeta[{wanted}] @ {SOURCE_URL}', "
                 "listing every extra you want: a reinstall drops the ones it does not name)")


def _main(argv: list[str]) -> None:
    if not _subcommand(argv):
        _front(argv)
        return
    args = _parser().parse_args(argv)
    cwd = os.path.abspath(args.cwd or os.getcwd())
    if args.command == "trust":
        from noeta.cli.trust import trust_project
        print(f"trusted {trust_project(cwd)}: its plugins, agent files, prompt overrides, "
              "skills and Claude-Code-format settings load from now on")
        return
    if args.command == "plugins":
        _plugins(cwd)
        return
    if args.command == "plugin":
        sys.exit(plugin_command.run(args, cwd))
    if args.command == "mcp":
        sys.exit(mcp_command.run(args, cwd))
    if args.command == "token":
        sys.exit(tenants.run(args))
    if args.command == "acp":
        from noeta.host.acp import EditorServers, serve_stdio
        if args.server:
            from noeta.cli.server_token import server_token
            from noeta.host.remote import RemoteApp
            # The editor's directory is a path on this machine, and the
            # model is the server's unless --model names one.
            asyncio.run(serve_stdio(RemoteApp(args.server, token=server_token()),
                                    model=args.model or "", editor_cwd=False,
                                    cwd=args.cwd or ""))
            return
        s = _settings(args)
        servers = EditorServers()
        asyncio.run(serve_stdio(_app(s, cwd=cwd, plugins={"mcp": {"servers": servers}},
                                     own_tasks_only=True),
                                model=s.model, servers=servers))
        return
    if args.command == "worker":
        asyncio.run(_worker(_settings(args), args.queue or ["default"], args.concurrency,
                            args.lease, args.snapshot_every, server_dirs=args.server_dir,
                            allow_bypass=args.allow_bypass_permissions))
        return
    if args.command == "serve":
        from noeta.host.http import LOOPBACK_HOSTS
        if args.no_token and args.host not in LOOPBACK_HOSTS:
            sys.exit("noeta: --no-token serves only a loopback address")
        asyncio.run(_serve(_settings(args), args, cwd))
        return


def _front(argv: list[str]) -> None:
    from noeta.cli import headless

    args = _front_parser().parse_args(argv)
    if args.version:
        from noeta.host.stream_json import noeta_version
        print(f"{noeta_version()} (noeta)")
        return
    cwd = os.path.abspath(args.cwd or os.getcwd())
    if args.permission_mode in MODE_ALIASES:
        args.permission_mode = MODE_ALIASES[args.permission_mode]
    if headless.headless(args):
        if args.server:
            sys.exit("noeta: -p runs its task here; --server attaches the terminal UI "
                     "(or noeta acp --server) to a server")
        try:
            layers: dict[str, Any] = {
                "sources": headless.setting_sources(args),
                "flag": headless.settings_layer(args, home_dir.HOME / "flag-settings")}
            mcp = headless.mcp_arguments(args)
            agent_files = headless.agent_files(args)
        except headless.FlagError as exc:
            sys.exit(f"noeta: {exc}")
        s = _settings(args, **layers)
        if args.system_prompt_file:
            s.prompts["system/main"] = Path(args.system_prompt_file).read_text("utf-8")
        if args.append_system_prompt_file:
            s.prompts["system/main.append"] = Path(
                args.append_system_prompt_file).read_text("utf-8")
        app = _app(s, cwd=cwd, own_tasks_only=True,
                   permissions=headless.permission_arguments(args),
                   plugins={"mcp": mcp} if mcp is not None else None,
                   setting_sources=layers["sources"], flag_settings=layers["flag"],
                   agent_files=agent_files)
        sys.exit(asyncio.run(headless.run(app, s, args, cwd=cwd)))
    from noeta.cli.tui import run_tui

    if args.server:
        # The server's model and directory unless --model / --cwd name one;
        # a local path or a model only this machine knows means nothing there.
        run_tui(_settings(args, need_model=False), cwd=cwd, resume=args.resume,
                server=args.server, permission_mode=args.permission_mode,
                task_config={k: v for k, v in (("cwd", args.cwd), ("model", args.model))
                             if v})
        return
    run_tui(_settings(args), cwd=cwd, resume=args.resume,
            permission_mode=args.permission_mode)


if __name__ == "__main__":
    main()
