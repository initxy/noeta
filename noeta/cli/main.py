"""The ``noeta`` command.

    noeta                      the terminal UI (the ``noeta-tui`` package, found through
                               the entry-point group ``noeta.frontends``)
    noeta -p "fix the test"    run one prompt in the current directory and print the answer;
                               Claude Code's headless flags and stream-json protocol
                               (``noeta.cli.headless``)
    noeta acp                  serve the Agent Client Protocol on stdio (editors);
                               ``--server URL`` drives a ``noeta serve`` server's tasks
    noeta worker               run tasks from the store until stopped (multi-machine)
    noeta serve                the client API over HTTP (and workers, unless --no-worker)
    noeta service ...          run noeta serve as this user's service (install, status, logs …)
    noeta update               upgrade noeta in place, keeping its extras
    noeta plugin ...           install (pinned), list and remove plugins
    noeta mcp ...              add, list, remove and sign in to MCP servers

Model and provider come from ``--model`` / ``--provider`` or the environment
(``NOETA_MODEL``, ``NOETA_PROVIDER``, ``NOETA_BASE_URL``, ``NOETA_API_KEY``)
or ``~/.noeta/config.toml``.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import logging
import os
import signal
import sys
from pathlib import Path
from typing import Any

from noeta.agents.agent_files import AgentFileError, PluginArgumentError
from noeta.cli import (
    crash_report,
    home_dir,
    mcp_command,
    plugin_command,
    service_command,
    update_command,
)
from noeta.cli.config import ConfigError, Settings, load_settings
from noeta.toolkit.permission_modes import MODE_ALIASES, PERMISSION_MODES

SUBCOMMANDS = ("acp", "worker", "serve", "service", "update", "trust", "plugins", "plugin", "mcp")

#: The extras the README's install line names.
README_EXTRAS: tuple[str, ...] = ()
#: The top-level modules each optional extra installs (``pyproject.toml``).
EXTRA_MODULES = {
    "uvicorn": "server", "starlette": "server", "websockets": "server",
    "acp": "acp", "psycopg": "postgres", "psycopg_pool": "postgres",
    "keyring": "keyring", "py_mini_racer": "workflow",
}
#: The built-in plugins that need an extra's module to do their work: plugin
#: name -> that module (``noeta plugins`` names the extra when it is missing).
PLUGIN_MODULES = {"workflow": "py_mini_racer"}


def _noeta(*extras: str) -> str:
    """noeta's install requirement with ``extras``."""
    named = ",".join(dict.fromkeys(extras))
    return f"noeta[{named}]" if named else "noeta"


def _uv_reinstall(*extras: str, with_: str = "") -> str:
    return f"uv tool install --reinstall{with_} '{_noeta(*README_EXTRAS, *extras)}'"


def _pip(requirement: str) -> str:
    return f"pip install '{requirement}'"


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
                         "on the server (default: the server's own)")
    ap.add_argument("--system-prompt", help="replace the main agent's system prompt")
    ap.add_argument("--append-system-prompt",
                    help="add text after the main agent's system prompt")


def _serve_arguments(sv: argparse.ArgumentParser) -> None:
    """``noeta serve``'s flags (also ``noeta service install``'s)."""
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8642)
    sv.add_argument("--no-worker", action="store_true", help="API only; workers run elsewhere")
    sv.add_argument("--no-token", action="store_true",
                    help="serve a loopback address without a bearer token")
    sv.add_argument("--allow-host", action="append", default=[], metavar="NAME",
                    help="a Host header name the server answers (repeatable) besides "
                         "the loopback names and, on another bind, this machine's own "
                         "names and addresses (a reverse proxy's name, say)")
    sv.add_argument("--allow-origin", action="append", default=[], metavar="ORIGIN",
                    help="a web origin (https://host:port) whose pages may call the API")
    sv.add_argument("--allow-bypass-permissions", action="store_true",
                    help="let requests start or switch tasks into bypassPermissions")
    sv.add_argument("--allow-plugin-install", action="store_true",
                    help="let requests install and remove plugins (always allowed on a "
                         "loopback address without --allow-host or --allow-origin)")
    sv.add_argument("--web", metavar="DIR",
                    help="serve this directory's files (the built web app, web/dist) at the "
                         "server's other paths (default: the web app noeta was built with, "
                         "if any)")


def _parser() -> argparse.ArgumentParser:
    """The subcommands (``noeta acp`` …)."""
    ap = argparse.ArgumentParser(prog="noeta", description="Durable AI agents.")
    _common(ap)
    sub = ap.add_subparsers(dest="command")
    ac = sub.add_parser("acp", help="serve the Agent Client Protocol on stdio")
    ac.add_argument("--server", metavar="URL",
                    help="drive the tasks of a noeta server (token: NOETA_SERVER_TOKEN, else "
                         "~/.noeta/server-token) "
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
    w.add_argument("--restart-limit", type=int, default=None, metavar="N",
                   help="restarts in a row a task gets after its worker died mid-run, with "
                        "no step finished in between, before it ends failed (default 3; 0 "
                        "no limit)")
    w.add_argument("--allow-bypass-permissions", action="store_true",
                   help="let tasks run in bypassPermissions")
    _serve_arguments(sub.add_parser("serve", help="the client API over HTTP"))
    service_command.add_parser(sub, _serve_arguments)
    update_command.add_parser(sub)
    sub.add_parser("trust", help="load this project's plugins, agents, prompts and skills")
    sub.add_parser("plugins", help="list the plugins and where each comes from")
    plugin_command.add_parser(sub)
    mcp_command.add_parser(sub)
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
                    help="TUI: drive the tasks of a noeta server (token: NOETA_SERVER_TOKEN, "
                         "else ~/.noeta/server-token)")
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
    for line in broken_plugin_lines():
        print(f"(failed) {line}", file=sys.stderr)
    for line in plugin_extra_lines():
        print(f"(missing) {line}", file=sys.stderr)
    for notice in notices:
        print(notice, file=sys.stderr)


QUICKSTART_URL = "https://initxy.github.io/noeta/start/quickstart"
#: What a start with no model says: a model and a key for its provider.
NO_MODEL = (
    "noeta: no model configured. noeta needs a model and an API key for its provider:\n"
    "  Anthropic:\n"
    "    export NOETA_MODEL=sonnet ANTHROPIC_API_KEY=sk-ant-...\n"
    "  an OpenAI-compatible API (DeepSeek here):\n"
    "    export NOETA_PROVIDER=openai_compat NOETA_BASE_URL=https://api.deepseek.com/v1\n"
    "    export NOETA_MODEL=deepseek-flash NOETA_API_KEY=sk-...\n"
    "Flags (--model, --provider, --base-url) and ~/.noeta/config.toml work too. "
    f"Quickstart: {QUICKSTART_URL}")


def _settings(args: argparse.Namespace, *, need_model: bool = True,
              **layers: Any) -> Settings:
    """The settings; ``need_model``: exit when none names a model (a front
    end attached to a server uses the server's)."""
    s = load_settings(cwd=os.path.abspath(args.cwd or os.getcwd()),
                      model=args.model, provider=args.provider, base_url=args.base_url,
                      store=args.store, effort=args.effort, **layers)
    if need_model and not s.model:
        sys.exit(NO_MODEL)
    if args.system_prompt is not None:
        s.prompts["system/main"] = args.system_prompt
    if args.append_system_prompt is not None:
        s.prompts["system/main.append"] = args.append_system_prompt
    return s


def _task_defaults(s: Settings, args: argparse.Namespace) -> dict[str, Any]:
    """What new tasks' config starts with unless whoever creates one says
    otherwise: the effort (flag, ``NOETA_EFFORT`` or config.toml) and the
    command line's permission mode. A task keeps the config it was created
    with, whichever process later runs it."""
    return {**({"effort": s.effort} if s.effort else {}),
            **({"permission_mode": args.permission_mode} if args.permission_mode else {})}


def _check_serve(args: argparse.Namespace) -> None:
    """Exit when ``noeta serve``'s flags contradict each other."""
    from noeta.host.http import LOOPBACK_HOSTS
    if args.no_token and args.host not in LOOPBACK_HOSTS:
        sys.exit("noeta: --no-token serves only a loopback address")
    _refuse_bypass(args)


def _refuse_bypass(args: argparse.Namespace) -> None:
    if args.permission_mode == "bypassPermissions" and not args.allow_bypass_permissions:
        sys.exit("noeta: --permission-mode bypassPermissions needs --allow-bypass-permissions")


def _app(s: Settings, **kw: Any) -> Any:
    from noeta.cli.config import from_settings

    kw.setdefault("cwd", os.getcwd())
    return from_settings(s, **kw)


def _local_app(args: argparse.Namespace, cwd: str) -> tuple[Any, Settings, Any]:
    """The app the terminal UI and ``-p`` run here, with Claude Code's
    command-line flags (settings layers, permission rules, directories, MCP
    servers, agents, system prompt files, plugin directories) applied the
    same way in both; and the hooks plugin's client callbacks."""
    from noeta.cli import headless

    try:
        layers: dict[str, Any] = {
            "sources": headless.setting_sources(args),
            "flag": headless.settings_layer(args, home_dir.HOME / "flag-settings")}
        mcp = headless.mcp_arguments(args)
        hooks = headless.hooks_arguments(args)
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
               plugins={"hooks": hooks, **({"mcp": mcp} if mcp is not None else {})},
               setting_sources=layers["sources"], flag_settings=layers["flag"],
               agent_files=agent_files, plugin_dirs=args.plugin_dirs)
    return app, s, hooks["callbacks"]


#: How long ``noeta serve`` waits on this machine's names (DNS) before it
#: answers only its addresses and hostname.
MACHINE_NAMES_SECONDS = 5.0


async def _machine_names(host: str) -> list[str]:
    """This machine's names and addresses when ``host`` is no loopback bind
    (looked up off the event loop), else none."""
    from noeta.cli.serve_address import machine_names
    from noeta.host.http import LOOPBACK_HOSTS
    from noeta.toolkit.sandbox import own_addresses

    if host in LOOPBACK_HOSTS:
        return []
    try:
        return await asyncio.wait_for(asyncio.to_thread(machine_names), MACHINE_NAMES_SECONDS)
    except TimeoutError:
        import socket
        return [socket.gethostname(), *own_addresses()]


async def _serve(s: Settings, args: argparse.Namespace, cwd: str) -> None:
    import uvicorn

    from noeta.cli.home_dir import HOME
    from noeta.cli.serve_address import addresses, allowed_hosts
    from noeta.cli.server_token import serve_token
    from noeta.host.http import (
        LOOPBACK_HOSTS,
        bundled_web_dir,
        create_app,
        redact_token_logs,
    )

    host, with_worker = args.host, not args.no_worker
    web = os.path.abspath(args.web) if args.web else bundled_web_dir()
    server = None if args.no_token else serve_token()
    if server is not None and server.made:
        print(f"noeta: made a new bearer token, kept in {HOME / 'server-token'} (owner "
              "only; noeta --server and noeta acp --server on this machine read it)",
              file=sys.stderr)
    machine = await _machine_names(host)
    # The plugin refuses bypassPermissions itself, whatever way it is asked.
    permissions: dict[str, Any] = {} if args.allow_bypass_permissions else {
        "disable_bypass_mode": True}
    app = _app(s, cwd=cwd, with_channels=True, permissions=permissions)
    app.client.task_config.update(_task_defaults(s, args))
    await app.start(worker=with_worker)
    api = create_app(app.client, auth=server is not None,
                     token=server.token if server else None,
                     deltas=app.deltas, agents=app.runtime.agents,
                     app=app if with_worker else None,
                     allowed_hosts=allowed_hosts(host, args.allow_host, machine),
                     origins=args.allow_origin,
                     allow_bypass=args.allow_bypass_permissions, cwd=cwd,
                     web_dir=web,
                     # A Host or origin named besides the loopback ones: a
                     # reverse proxy may bring anyone to the loopback bind.
                     allow_plugin_install=args.allow_plugin_install
                     or (host in LOOPBACK_HOSTS and not args.allow_host
                         and not args.allow_origin),
                     plugin_admin=app.plugin_admin,
                     projects_file=str(home_dir.projects_file(s.store_url)),
                     scratch=str(HOME / "scratch"))
    what = "web app" if web else "API (no web app)"
    for url in addresses(host, args.port, server.token if server else None, args.allow_host,
                         machine):
        print(f"noeta: {what} at {url}", file=sys.stderr)
    config = uvicorn.Config(api, host=host, port=args.port)
    redact_token_logs()
    try:
        await uvicorn.Server(config).serve()
    finally:
        await app.stop()


async def _worker(s: Settings, queues: list[str], concurrency: int,
                  lease: float | None = None, snapshot_every: int | None = None, *,
                  restart_limit: int | None = None, allow_bypass: bool = False,
                  task_defaults: dict[str, Any] | None = None,
                  flags: tuple[str, ...] = ()) -> None:
    """``task_defaults``: the config of the tasks this worker's channels
    start (``flags``: the command-line flags that gave it, named on stderr:
    a worker runs tasks others created, which keep their own config)."""
    kw: dict[str, Any] = {"lease_seconds": lease} if lease else {}
    # As noeta serve: bypassPermissions refused in the plugin itself.
    kw["permissions"] = {} if allow_bypass else {"disable_bypass_mode": True}
    if snapshot_every is not None:
        kw["snapshot_every"] = snapshot_every
    if restart_limit is not None:
        kw["restart_limit"] = restart_limit
    app = _app(s, queues=tuple(queues), concurrency=concurrency, with_channels=True, **kw)
    app.client.task_config.update(task_defaults or {})
    if flags:
        where = "only to the tasks this worker's channels start" if app.channels else \
            "to no task: this worker has no channels, and it runs tasks others created"
        print(f"noeta: {' and '.join(flags)} apply {where}; a task keeps the config it was "
              "created with", file=sys.stderr)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    await app.run_until(stop)


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


def broken_plugin_lines() -> list[str]:
    """``path: why`` for each plugin file that failed, with how to install a
    module it imports that is not installed here (plugins install nothing)."""
    from noeta.core import registry

    missing = registry.missing_modules()
    return [f"{path}: {why}{_install_hint(missing[path]) if path in missing else ''}"
            for path, why in registry.broken_plugin_files().items()]


def plugin_extra_lines() -> list[str]:
    """``plugin: why`` for each installed built-in plugin whose extra is not
    installed here, with the install line."""
    from noeta.core import registry

    names = set(registry.available(registry.PLUGINS))
    return [f"{name}: the {module} module is not installed{_install_hint(module)}"
            for name, module in PLUGIN_MODULES.items()
            if name in names and importlib.util.find_spec(module) is None]


def _install_hint(module: str) -> str:
    extra = EXTRA_MODULES.get(module)
    if extra is not None:
        return (f"; it needs the {extra} extra: {_uv_reinstall(extra)} (name every extra and"
                " --with you use: a reinstall drops the rest; from a checkout, its path instead"
                f" of the name), or {_pip(_noeta(extra))}")
    return (f"; install it next to noeta: {_uv_reinstall(with_=f' --with {module}')} (name"
            " every extra and --with you use: a reinstall drops the rest; from a checkout, its"
            f" path instead of the name), or pip install {module} in noeta's"
            " environment; the package's name may differ from the module's")


def main(argv: list[str] | None = None) -> None:
    words = list(sys.argv[1:] if argv is None else argv)
    try:
        _main(words)
    except (ConfigError, AgentFileError, PluginArgumentError) as exc:
        sys.exit(f"noeta: {exc}")
    except KeyboardInterrupt:
        sys.exit(130)
    except ImportError as exc:
        extra = missing_extra(exc)
        if extra is None:
            sys.exit(crash_report.crash(exc, words))
        # A uv tool reinstall keeps only the extras it names.
        sys.exit(f"noeta: this needs the {extra} extra, which is not installed: "
                 f"{_pip(_noeta(extra))} (or, for a uv tool install, {_uv_reinstall(extra)}, "
                 "listing every extra you want: a reinstall drops the ones it does not name)")
    except Exception as exc:  # noqa: BLE001 - any other error: one line and a report
        sys.exit(crash_report.crash(exc, words))


def _main(argv: list[str]) -> None:
    if not _subcommand(argv):
        _front(argv)
        return
    args = _parser().parse_args(argv)
    cwd = os.path.abspath(args.cwd or os.getcwd())
    if args.permission_mode in MODE_ALIASES:
        args.permission_mode = MODE_ALIASES[args.permission_mode]
    if args.command == "trust":
        from noeta.cli.trust import trust_project
        print(f"trusted {trust_project(cwd)}: its plugins, agent files, prompt overrides, "
              "skills and Claude-Code-format settings load from now on")
        return
    if args.command == "plugins":
        _plugins(cwd)
        return
    if args.command == "update":
        sys.exit(update_command.run(args))
    if args.command == "service":
        if args.service_action == "install":
            _check_serve(args)
            _settings(args)  # a service without a model would only restart and fail
        sys.exit(service_command.run(args, serve_arguments=_serve_arguments, common=_common))
    if args.command == "plugin":
        sys.exit(plugin_command.run(args, cwd))
    if args.command == "mcp":
        sys.exit(mcp_command.run(args, cwd))
    if args.command == "acp":
        from noeta.host.acp import EditorServers, serve_stdio
        if args.server:
            from noeta.cli.server_token import server_problem, server_token
            from noeta.host.remote import RemoteApp
            token = server_token()
            if (problem := server_problem(args.server, token)) is not None:
                sys.exit(f"noeta: {problem}")
            # The editor's directory is a path on this machine, and the
            # model is the server's unless --model names one.
            asyncio.run(serve_stdio(RemoteApp(args.server, token=token),
                                    model=args.model or "", editor_cwd=False,
                                    cwd=args.cwd or "", config=_task_defaults(
                                        _settings(args, need_model=False), args)))
            return
        s = _settings(args)
        servers = EditorServers()
        asyncio.run(serve_stdio(_app(s, cwd=cwd, plugins={"mcp": {"servers": servers}},
                                     own_tasks_only=True),
                                model=s.model, servers=servers,
                                config=_task_defaults(s, args)))
        return
    if args.command == "worker":
        _refuse_bypass(args)
        s = _settings(args)
        flags = tuple(f for f, v in (("--effort", args.effort),
                                     ("--permission-mode", args.permission_mode)) if v)
        asyncio.run(_worker(s, args.queue or ["default"], args.concurrency,
                            args.lease, args.snapshot_every, restart_limit=args.restart_limit,
                            allow_bypass=args.allow_bypass_permissions,
                            task_defaults=_task_defaults(s, args), flags=flags))
        return
    if args.command == "serve":
        _check_serve(args)
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
    if args.dangerously_skip_permissions:
        args.permission_mode = "bypassPermissions"
    if headless.headless(args):
        if args.server:
            sys.exit("noeta: -p runs its task here; --server attaches the terminal UI "
                     "(or noeta acp --server) to a server")
        app, s, hook_callbacks = _local_app(args, cwd)
        sys.exit(asyncio.run(headless.run(app, s, args, cwd=cwd,
                                          hook_callbacks=hook_callbacks)))
    run = front_end("tui")
    from noeta.cli.config import settings_rows
    from noeta.host.app import FrontEndStart, LoggedWarnings
    from noeta.host.stream_json import noeta_version

    # The terminal UI's screen covers stderr: what is logged from here on
    # is shown in it instead.
    warnings = LoggedWarnings()
    logging.getLogger().addHandler(warnings)
    common: dict[str, Any] = {"cwd": cwd, "resume": args.resume, "warnings": warnings,
                              "continue_last": args.continue_last,
                              "permission_mode": args.permission_mode, "prompt": args.prompt,
                              "home": str(home_dir.HOME), "version": noeta_version()}
    backend: Any
    if args.server:
        if local := headless.local_app_flags(args):
            sys.exit(f"noeta: {', '.join(local)} apply to the tasks this process runs; with "
                     "--server the server's settings and plugins run")
        from noeta.cli.server_token import server_problem, server_token
        from noeta.host.remote import RemoteApp

        s = _settings(args, need_model=False)
        token = server_token()
        if (problem := server_problem(args.server, token)) is not None:
            sys.exit(f"noeta: {problem}")
        backend = RemoteApp(args.server, token=token)
        # The server's model and directory unless --model / --cwd name one;
        # a local path or a model only this machine knows means nothing there.
        config = {k: v for k, v in (("cwd", args.cwd), ("model", args.model)) if v}
        start = FrontEndStart(agent=args.agent or "main",
                              model=str(config.get("model") or "the server's model"),
                              task_config=config, on_server=True,
                              settings=lambda: settings_rows(s, cwd), **common)
    else:
        from noeta.cli.trust import project_trusted

        unused = headless.unused_in_terminal(args)
        args.permission_prompt_tool = None
        backend, s, _ = _local_app(args, cwd)
        backend.notices += [f"{flag} is used only with -p; ignored here" for flag in unused]
        warnings.forget(backend.notices)  # shown as the app's notices
        start = FrontEndStart(agent=args.agent or backend.main_agent, model=s.model,
                              task_config={"cwd": cwd, "model": s.model,
                                           **({"effort": s.effort} if s.effort else {})},
                              settings=lambda: settings_rows(s, cwd),
                              trust=None if project_trusted(cwd) else
                              lambda: trust_here(backend, cwd), **common)
    run(backend, start)


def trust_here(app: Any, cwd: str) -> None:
    """Trust the project at ``cwd`` (the terminal UI's trust question) and
    load what a trusted project loads, its "not trusted" notices dropped."""
    from noeta.cli.trust import PROJECT_GATED_DIRS, trust_project, untrusted_notice

    stale = {untrusted_notice(cwd, sub) for sub in PROJECT_GATED_DIRS}
    trust_project(cwd)
    app.notices = [n for n in app.notices if n not in stale]
    app.reload_plugins()


def front_end(name: str) -> Any:
    """The front end ``name`` (entry-point group ``noeta.frontends``), or
    exit naming how to install it."""
    from noeta.core import registry

    try:
        return registry.load(registry.FRONTENDS, name)
    except registry.UnknownName:
        sys.exit(f"noeta: the terminal UI is its own package, noeta-tui, which is not "
                 f"installed: {_uv_reinstall('tui')} (name every extra and --with you use: a "
                 "reinstall drops the rest; from a checkout, '.[tui]' in it), or "
                 f"{_pip('noeta-tui')}")

if __name__ == "__main__":
    main()
