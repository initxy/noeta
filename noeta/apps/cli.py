"""The ``noeta`` command.

    noeta                      the terminal UI (needs the ``tui`` extra)
    noeta -p "fix the test"    run one prompt in the current directory and print the answer
    noeta acp                  serve the Agent Client Protocol on stdio (editors)
    noeta worker               run tasks from the store until stopped (multi-machine)
    noeta serve                the client API over HTTP (and workers, unless --no-worker)

Model and provider come from ``--model`` / ``--provider`` or the environment
(``NOETA_MODEL``, ``NOETA_PROVIDER``, ``NOETA_BASE_URL``, ``NOETA_API_KEY``)
or ``~/.noeta/config.toml``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import sys
from typing import Any

from noeta.apps.config import Settings, load_settings


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="noeta", description="Durable AI agents.")
    ap.add_argument("-p", "--print", dest="prompt", help="run one prompt and print the answer")
    ap.add_argument("--json", action="store_true", help="with -p: stream events as JSON lines")
    ap.add_argument("--model")
    ap.add_argument("--effort", choices=("low", "medium", "high", "xhigh", "max"))
    ap.add_argument("--permission-mode", default=None,
                    choices=("default", "acceptEdits", "bypassPermissions"),
                    help="ask before risky tools (default), let edits through, or never ask")
    ap.add_argument("--provider", choices=("anthropic", "openai_compat", "openai_responses"))
    ap.add_argument("--base-url")
    ap.add_argument("--store", help="sqlite:///path or postgres://... (default ~/.noeta/tasks.db)")
    ap.add_argument("--cwd", default=None, help="working directory (default: current)")
    ap.add_argument("--system-prompt", help="replace the main agent's system prompt")
    ap.add_argument("--append-system-prompt",
                    help="add text after the main agent's system prompt")
    ap.add_argument("--resume", nargs="?", const="", default=None,
                    help="TUI: resume a task (id, or pick from a list)")
    ap.add_argument("--server", help="TUI: attach to a noeta server URL")
    sub = ap.add_subparsers(dest="command")
    sub.add_parser("acp", help="serve the Agent Client Protocol on stdio")
    w = sub.add_parser("worker", help="run tasks from the store until stopped")
    w.add_argument("--queue", action="append", default=None)
    w.add_argument("--concurrency", type=int, default=8)
    sv = sub.add_parser("serve", help="the client API over HTTP")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8642)
    sv.add_argument("--no-worker", action="store_true", help="API only; workers run elsewhere")
    sub.add_parser("trust", help="load this project's .noeta/plugins from now on")
    sub.add_parser("plugins", help="list the plugins and where each comes from")
    return ap


def _plugins(cwd: str) -> None:
    from noeta.apps.config import load_plugin_files
    from noeta.core import registry

    notices = load_plugin_files(cwd)
    for name, source in registry.sources(registry.PLUGINS).items():
        print(f"{name}\t{source}")
    for path, why in registry.broken_plugin_files().items():
        print(f"(failed) {path}: {why}", file=sys.stderr)
    for notice in notices:
        print(notice, file=sys.stderr)


def _settings(args: argparse.Namespace) -> Settings:
    s = load_settings(model=args.model, provider=args.provider, base_url=args.base_url,
                      store=args.store, effort=args.effort)
    if not s.model:
        sys.exit("noeta: no model configured (--model or NOETA_MODEL)")
    if args.system_prompt is not None:
        s.prompts["system/main"] = args.system_prompt
    if args.append_system_prompt is not None:
        s.prompts["system/main.append"] = args.append_system_prompt
    return s


def _app(s: Settings, **kw: Any) -> Any:
    from noeta.apps.config import from_settings

    kw.setdefault("cwd", os.getcwd())
    return from_settings(s, **kw)


def _describe(request: Any) -> str:
    """What a waiting sub-agent waits for, in one line."""
    who = f"sub-agent {request.agent or 'agent'!r} (task {request.task_id})"
    if request.kind == "approval":
        what = request.prompt or request.tool or request.request_id
        return f"{who} is waiting for approval: {what}"
    questions = [str(q.get("question") or "") for q in request.detail.get("questions") or []]
    return f"{who} is waiting for an answer: {' / '.join(questions) or request.request_id}"


async def _print_mode(s: Settings, prompt: str, *, cwd: str, as_json: bool,
                      permission_mode: str | None) -> int:
    import msgspec

    from noeta.core.events import ExtensionRecorded, ModelReplied, TaskEnded, Waiting
    from noeta.core.loop.state import CORE
    from noeta.core.pending import follow_tree
    from noeta.core.types import Usage

    async with _app(s, cwd=cwd, own_tasks_only=True) as app:
        for notice in app.notices:
            print(f"noeta: {notice}", file=sys.stderr)
        config: dict[str, Any] = {"cwd": cwd, "model": s.model}
        if s.effort:
            config["effort"] = s.effort
        if permission_mode:
            config["permission_mode"] = permission_mode
        tid = await app.client.create("main", prompt, config=config)
        usage = Usage()

        def summary(answer: str) -> None:
            if as_json:
                print(json.dumps({"answer": answer, "task_id": tid, "model": s.model,
                                  "usage": msgspec.to_builtins(usage)}), flush=True)
        if as_json:
            live: dict[int, int] = {}

            def on_delta(task_id: str, delta: Any) -> None:
                if task_id != tid or delta.attempt < live.get(delta.call_seq, 1):
                    return  # the tail of an attempt that failed
                live[delta.call_seq] = delta.attempt
                print(json.dumps({"delta": delta.kind, "text": delta.text,
                                  "call": delta.call_seq, "attempt": delta.attempt}),
                      flush=True)

            app.deltas(on_delta)
        async for rec in follow_tree(app.client, tid):
            if isinstance(rec, list):  # a sub-agent waits on a person
                print(f"noeta: {_describe(rec[0])}; use the TUI or an editor for "
                      "interactive work", file=sys.stderr)
                return 2
            ev = rec.event
            if as_json:
                sys.stdout.write(msgspec.json.encode(rec).decode() + "\n")
                sys.stdout.flush()
            if isinstance(ev, ModelReplied):
                usage = usage + ev.response.usage
            if (isinstance(ev, ExtensionRecorded) and ev.extension == CORE
                    and ev.kind == "notice" and not as_json):
                print(f"noeta: {(ev.data or {}).get('text', '')}", file=sys.stderr)
            if isinstance(ev, Waiting) and ev.on == "input":
                failed = ev.detail.get("turn_failed")
                answer = ev.detail.get("answer", "")
                summary(answer)
                if not as_json:
                    print(answer if not failed else f"error: {failed}")
                return 1 if failed else 0
            if isinstance(ev, Waiting) and ev.on in ("approval", "question"):
                print(f"noeta: the agent is waiting for {ev.on}; use the TUI or an editor "
                      "for interactive work", file=sys.stderr)
                return 2
            # children, a timer, an external signal: the task goes on by itself.
            if isinstance(ev, TaskEnded):
                summary(ev.answer)
                if not as_json:
                    print(ev.answer or ev.error)
                return 0 if ev.status == "done" else 1
    return 1


async def _serve(s: Settings, host: str, port: int, with_worker: bool) -> None:
    import uvicorn

    from noeta.host.server import create_app

    app = _app(s)
    if with_worker:
        await app.start()
    api = create_app(app.client, token=os.environ.get("NOETA_SERVER_TOKEN") or None,
                     deltas=app.deltas if with_worker else None)
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
    args = _parser().parse_args(argv)
    cwd = os.path.abspath(args.cwd or os.getcwd())
    if args.command == "trust":
        from noeta.apps.config import trust_project
        print(f"trusted {trust_project(cwd)}: its .noeta/plugins load from now on")
        return
    if args.command == "plugins":
        _plugins(cwd)
        return
    if args.command == "acp":
        from noeta.apps.acp import EditorServers, serve_stdio
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
        if args.host not in ("127.0.0.1", "localhost") and not os.environ.get("NOETA_SERVER_TOKEN"):
            sys.exit("noeta: set NOETA_SERVER_TOKEN before serving on a non-local address")
        asyncio.run(_serve(_settings(args), args.host, args.port, not args.no_worker))
        return
    if args.prompt is not None:
        sys.exit(asyncio.run(_print_mode(_settings(args), args.prompt, cwd=cwd,
                                         as_json=args.json,
                                         permission_mode=args.permission_mode)))
    try:
        from noeta.apps.tui import run_tui
    except ImportError:
        sys.exit("noeta: the terminal UI needs the tui extra: pip install 'noeta[tui]'")
    run_tui(_settings(args), cwd=cwd, resume=args.resume, server=args.server,
            permission_mode=args.permission_mode)


if __name__ == "__main__":
    main()
