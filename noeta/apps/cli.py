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

from noeta.apps.config import Settings, load_settings, open_store


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
    return ap


def _settings(args: argparse.Namespace) -> Settings:
    s = load_settings(model=args.model, provider=args.provider, base_url=args.base_url,
                      store=args.store, effort=args.effort)
    if not s.model:
        sys.exit("noeta: no model configured (--model or NOETA_MODEL)")
    return s


def _app(s: Settings, *, agents_kw: dict[str, Any] | None = None,
         own_tasks_only: bool = False, **runtime_kw: Any) -> Any:
    """A store, the default agents and an in-process worker. Front ends
    (``-p``, ``acp``, the TUI) pass ``own_tasks_only`` so their worker never
    picks up another process's task from the shared store; ``worker`` and
    ``serve`` serve every task."""
    from noeta.apps.config import HOME
    from noeta.apps.local import LocalApp
    from noeta.presets import default_agents

    kw: dict[str, Any] = {"memory_root": str(HOME / "memory"), **(agents_kw or {})}
    return LocalApp(open_store(s.store_url), default_agents(s.model, **kw), s.make_provider(),
                    own_tasks_only=own_tasks_only, **runtime_kw)


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

    from noeta.core.events import ModelReplied, TaskEnded, Waiting
    from noeta.core.pending import follow_tree
    from noeta.core.types import Usage

    async with _app(s, own_tasks_only=True) as app:
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

    from noeta.apps.server import create_app

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
    if args.command == "acp":
        from noeta.apps.acp import EditorServers, serve_stdio
        from noeta.plugins.mcp import Mcp

        s = _settings(args)
        servers = EditorServers()
        asyncio.run(serve_stdio(_app(s, agents_kw={"mcp": Mcp(servers)}, own_tasks_only=True),
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
