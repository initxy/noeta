"""The ``noeta`` command.

    noeta                      the terminal UI (needs the ``tui`` extra)
    noeta -p "fix the test"    run one prompt in the current directory and print the answer;
                               Claude Code's headless flags and stream-json protocol
                               (``noeta.cli.headless``)
    noeta acp                  serve the Agent Client Protocol on stdio (editors)
    noeta worker               run tasks from the store until stopped (multi-machine)
    noeta serve                the client API over HTTP (and workers, unless --no-worker)
    noeta mcp ...              add, list, remove and sign in to MCP servers

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

from noeta.cli import mcp_command
from noeta.cli.config import Settings, load_settings
from noeta.toolkit.permission_modes import MODE_ALIASES, PERMISSION_MODES

SUBCOMMANDS = ("acp", "worker", "serve", "trust", "plugins", "mcp")


def _common(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--model")
    ap.add_argument("--effort", choices=("low", "medium", "high", "xhigh", "max"))
    ap.add_argument("--permission-mode", default=None,
                    choices=(*PERMISSION_MODES, *MODE_ALIASES),
                    help="the starting permission mode (default: auto)")
    ap.add_argument("--provider", choices=("anthropic", "openai_compat", "openai_responses"))
    ap.add_argument("--base-url")
    ap.add_argument("--store", help="sqlite:///path or postgres://... (default ~/.noeta/tasks.db)")
    ap.add_argument("--cwd", default=None, help="working directory (default: current)")
    ap.add_argument("--system-prompt", help="replace the main agent's system prompt")
    ap.add_argument("--append-system-prompt",
                    help="add text after the main agent's system prompt")


def _parser() -> argparse.ArgumentParser:
    """The subcommands (``noeta acp`` …)."""
    ap = argparse.ArgumentParser(prog="noeta", description="Durable AI agents.")
    _common(ap)
    sub = ap.add_subparsers(dest="command")
    sub.add_parser("acp", help="serve the Agent Client Protocol on stdio")
    w = sub.add_parser("worker", help="run tasks from the store until stopped")
    w.add_argument("--queue", action="append", default=None)
    w.add_argument("--concurrency", type=int, default=8)
    sv = sub.add_parser("serve", help="the client API over HTTP")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8642)
    sv.add_argument("--no-worker", action="store_true", help="API only; workers run elsewhere")
    sv.add_argument("--no-token", action="store_true",
                    help="serve a loopback address without a bearer token")
    sv.add_argument("--allow-origin", action="append", default=[], metavar="ORIGIN",
                    help="a web origin (https://host:port) whose pages may call the API")
    sv.add_argument("--allow-bypass-permissions", action="store_true",
                    help="let requests start or switch tasks into bypassPermissions")
    sv.add_argument("--allow-root", action="append", default=[], metavar="PATH",
                    help="a directory besides the server's own tasks may run in")
    sub.add_parser("trust", help="load this project's plugins, agents, prompts and skills")
    sub.add_parser("plugins", help="list the plugins and where each comes from")
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
    ap.add_argument("--server", help="TUI: attach to a noeta server URL")
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
    from noeta.cli.config import load_plugin_files
    from noeta.core import registry

    notices = load_plugin_files(cwd)
    for name, source in registry.sources(registry.PLUGINS).items():
        print(f"{name}\t{source}")
    for path, why in registry.broken_plugin_files().items():
        print(f"(failed) {path}: {why}", file=sys.stderr)
    for notice in notices:
        print(notice, file=sys.stderr)


def _settings(args: argparse.Namespace) -> Settings:
    s = load_settings(cwd=os.path.abspath(args.cwd or os.getcwd()),
                      model=args.model, provider=args.provider, base_url=args.base_url,
                      store=args.store, effort=args.effort)
    if not s.model:
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


async def _serve(s: Settings, args: argparse.Namespace, cwd: str) -> None:
    import uvicorn

    from noeta.cli.config import HOME, server_token
    from noeta.host.http import LOOPBACK_HOSTS, create_app

    host, with_worker = args.host, not args.no_worker
    token = None if args.no_token else server_token(create=True)
    if token is not None and not os.environ.get("NOETA_SERVER_TOKEN"):
        print(f"noeta: bearer token in {HOME / 'server-token'}: {token}", file=sys.stderr)
    # The plugin refuses bypassPermissions itself, whatever way it is asked.
    refuse = {} if args.allow_bypass_permissions else {
        "permissions": {"disable_bypass_mode": True}}
    app = _app(s, cwd=cwd, **refuse)
    if with_worker:
        await app.start()
    api = create_app(app.client, token=token,
                     deltas=app.deltas if with_worker else None, agents=app.runtime.agents,
                     app=app if with_worker else None,
                     allowed_hosts=LOOPBACK_HOSTS if host in LOOPBACK_HOSTS else None,
                     origins=args.allow_origin,
                     allow_bypass=args.allow_bypass_permissions, cwd=cwd,
                     roots=[os.path.abspath(r) for r in args.allow_root])
    port = args.port
    try:
        await uvicorn.Server(uvicorn.Config(api, host=host, port=port)).serve()
    finally:
        await app.stop()


async def _worker(s: Settings, queues: list[str], concurrency: int) -> None:
    app = _app(s, queues=tuple(queues), concurrency=concurrency)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    await app.runtime.serve(stop)
    await app.store.close()


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not _subcommand(argv):
        _front(argv)
        return
    args = _parser().parse_args(argv)
    cwd = os.path.abspath(args.cwd or os.getcwd())
    if args.command == "trust":
        from noeta.cli.config import trust_project
        print(f"trusted {trust_project(cwd)}: its plugins, agent files, prompt overrides "
              "skills and Claude Code settings load from now on")
        return
    if args.command == "plugins":
        _plugins(cwd)
        return
    if args.command == "mcp":
        sys.exit(mcp_command.run(args, cwd))
    if args.command == "acp":
        from noeta.host.acp import EditorServers, serve_stdio
        s = _settings(args)
        servers = EditorServers()
        asyncio.run(serve_stdio(_app(s, cwd=cwd, plugins={"mcp": {"servers": servers}},
                                     own_tasks_only=True),
                                model=s.model, servers=servers))
        return
    if args.command == "worker":
        asyncio.run(_worker(_settings(args), args.queue or ["default"], args.concurrency))
        return
    if args.command == "serve":
        if args.no_token and args.host not in ("127.0.0.1", "localhost", "::1"):
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
        s = _settings(args)
        if args.system_prompt_file:
            s.prompts["system/main"] = Path(args.system_prompt_file).read_text("utf-8")
        if args.append_system_prompt_file:
            s.prompts["system/main.append"] = Path(
                args.append_system_prompt_file).read_text("utf-8")
        app = _app(s, cwd=cwd, own_tasks_only=True,
                   permissions=headless.permission_arguments(args))
        sys.exit(asyncio.run(headless.run(app, s, args, cwd=cwd)))
    try:
        from noeta.cli.tui import run_tui
    except ImportError:
        sys.exit("noeta: the terminal UI needs the tui extra: pip install 'noeta[tui]'")
    run_tui(_settings(args), cwd=cwd, resume=args.resume, server=args.server,
            permission_mode=args.permission_mode)


if __name__ == "__main__":
    main()
