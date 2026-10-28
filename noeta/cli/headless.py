"""``noeta -p``: Claude Code's headless mode on stdin and stdout, with its
flags (``noeta.host.stream_json`` speaks the protocol).

    noeta -p "fix the test"                        print the answer
    noeta -p --output-format json "…"              print the result object
    noeta -p --output-format stream-json --verbose "…"
    noeta --input-format stream-json --output-format stream-json --verbose
                                                   what Claude Code's Agent SDK runs

Flags Claude Code has and noeta does not use (``--setting-sources``,
``--mcp-config``, ``--betas`` …) are accepted and ignored, so an SDK's
command line runs unchanged; each ignored one is named on stderr.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from noeta.cli.config import Settings

#: Accepted for compatibility, not applied (value-taking, then flags).
IGNORED_WITH_VALUE = ("--setting-sources", "--settings", "--mcp-config", "--fallback-model",
                      "--betas", "--max-thinking-tokens", "--thinking", "--thinking-display",
                      "--agents", "--plugin-dir", "--plugin-url", "--task-budget", "--debug-file",
                      "--name", "--autocompact", "--file", "--system-prompt-snapshot")
IGNORED_FLAGS = ("--strict-mcp-config", "--include-hook-events", "--no-session-persistence",
                 "--session-mirror", "--bare", "--allow-dangerously-skip-permissions",
                 "--exclude-dynamic-system-prompt-sections", "--forward-subagent-text",
                 "--disable-slash-commands")


def add_arguments(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("-p", "--print", dest="print_mode", action="store_true",
                    help="run the prompt without the terminal UI and print the result")
    ap.add_argument("prompt", nargs="?", help="the prompt (with -p: read from stdin if left out)")
    ap.add_argument("--output-format", choices=("text", "json", "stream-json"), default=None)
    ap.add_argument("--input-format", choices=("text", "stream-json"), default="text")
    ap.add_argument("--include-partial-messages", action="store_true")
    ap.add_argument("--replay-user-messages", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--max-turns", type=int)
    ap.add_argument("--max-budget-usd", type=float)
    ap.add_argument("--json-schema", help="JSON Schema the answer must match (structured output)")
    ap.add_argument("--allowedTools", "--allowed-tools", dest="allowed_tools", nargs="+",
                    action="extend", default=[], help='allow rules, e.g. "Bash(git *) Edit"')
    ap.add_argument("--disallowedTools", "--disallowed-tools", dest="disallowed_tools",
                    nargs="+", action="extend", default=[], help="deny rules or tool names")
    ap.add_argument("--tools", nargs="+", action="extend", default=None,
                    help='the tools offered ("default": all, "": none)')
    ap.add_argument("--add-dir", nargs="+", action="extend", default=[])
    ap.add_argument("--permission-prompt-tool",
                    help='"stdio": the client answers permission prompts (can_use_tool)')
    ap.add_argument("--permission-prompts", choices=("host", "none"), default="host")
    ap.add_argument("--dangerously-skip-permissions", action="store_true")
    ap.add_argument("-c", "--continue", dest="continue_last", action="store_true",
                    help="continue the newest task started in this directory")
    ap.add_argument("--fork-session", dest="fork", action="store_true",
                    help="with --resume/--continue: go on in a new task")
    ap.add_argument("--session-id", dest="new_task_id", help="the id the new task gets")
    ap.add_argument("--agent", default="main")
    ap.add_argument("--system-prompt-file")
    ap.add_argument("--append-system-prompt-file")
    ap.add_argument("-v", "--version", action="store_true")
    ap.add_argument("-d", "--debug", nargs="?", const="", default=None)
    for flag in IGNORED_WITH_VALUE:
        ap.add_argument(flag, dest=f"ignored_{flag[2:].replace('-', '_')}", action="append",
                        nargs="?", const="", help=argparse.SUPPRESS)
    for flag in IGNORED_FLAGS:
        ap.add_argument(flag, dest=f"ignored_{flag[2:].replace('-', '_')}",
                        action="store_true", help=argparse.SUPPRESS)


def headless(args: argparse.Namespace) -> bool:
    return bool(args.print_mode or args.input_format == "stream-json"
                or args.output_format is not None)


def _ignored(args: argparse.Namespace) -> list[str]:
    out = []
    for flag in (*IGNORED_WITH_VALUE, *IGNORED_FLAGS):
        if getattr(args, f"ignored_{flag[2:].replace('-', '_')}", None):
            out.append(flag)
    return out


def permission_arguments(args: argparse.Namespace) -> dict[str, Any]:
    """The permissions plugin's arguments the flags give."""
    out: dict[str, Any] = {}
    allow = [r for value in args.allowed_tools for r in _rules(value)]
    deny = [r for value in args.disallowed_tools for r in _rules(value)]
    if allow:
        out["allow"] = allow
    if deny:
        out["deny"] = deny
        bare = [r for r in deny if "(" not in r]
        if bare:
            out["disallowed_tools"] = bare
    if args.tools is not None:
        names = [r for value in args.tools for r in _rules(value)]
        if names != ["default"]:
            out["allowed_tools"] = [n for n in names if n]
    if args.add_dir:
        out["additional_directories"] = [str(Path(d).expanduser().resolve())
                                         for d in args.add_dir]
    return out


def _rules(value: str) -> list[str]:
    """``"Bash(git *) Edit,Read"`` -> its rules (spaces inside parentheses kept)."""
    out: list[str] = []
    current, depth = "", 0
    for ch in value:
        if ch in " ," and depth == 0:
            if current:
                out.append(current)
            current = ""
            continue
        depth += ch == "("
        depth -= ch == ")"
        current += ch
    if current:
        out.append(current)
    return out


def _prompt_text(args: argparse.Namespace) -> str | None:
    if args.prompt is not None:
        return str(args.prompt)
    if args.input_format == "stream-json" or sys.stdin.isatty():
        return None
    return sys.stdin.read()


async def _stdin_lines(bad: list[str]) -> AsyncIterator[dict[str, Any]]:
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=64 * 1024 * 1024)
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
    while line := await reader.readline():
        text = line.decode("utf-8", errors="replace").strip()
        if not text:
            continue
        try:
            value = json.loads(text)
        except ValueError as exc:
            bad.append(f"not JSON: {exc}")
            continue
        if isinstance(value, dict):
            yield value


async def run(app: Any, s: Settings, args: argparse.Namespace, *, cwd: str) -> int:
    """Run headless on ``app`` (a ``LocalApp``); returns the exit code."""
    from noeta.core.types import Message, Text
    from noeta.host.stream_json import HeadlessOptions, StreamJson, drive, find_latest_task

    for flag in _ignored(args):
        print(f"noeta: {flag} is accepted for compatibility and ignored", file=sys.stderr)
    output = args.output_format or "text"
    if args.input_format == "stream-json" and output != "stream-json":
        print("noeta: --input-format=stream-json requires --output-format=stream-json",
              file=sys.stderr)
        return 1
    if output == "stream-json" and not args.verbose:
        print("noeta: --output-format=stream-json requires --verbose", file=sys.stderr)
        return 1
    schema = json.loads(args.json_schema) if args.json_schema else None
    mode = args.permission_mode
    if args.dangerously_skip_permissions:
        mode = "bypassPermissions"
    async with app:
        for notice in app.notices:
            print(f"noeta: {notice}", file=sys.stderr)
        resume = args.resume or None
        if args.continue_last and not resume:
            resume = await find_latest_task(app.client, cwd, args.agent)
            if resume is None:
                print("noeta: no task to continue in this directory", file=sys.stderr)
                return 1
        options = HeadlessOptions(
            agent=args.agent, model=s.model, cwd=cwd, permission_mode=mode,
            max_turns=args.max_turns, max_budget_usd=args.max_budget_usd,
            output_schema=schema, include_partial_messages=args.include_partial_messages,
            replay_user_messages=args.replay_user_messages,
            ask_client=args.permission_prompt_tool == "stdio"
            and args.permission_prompts == "host",
            resume=resume, fork=bool(args.fork), task_id=args.new_task_id,
            config={"effort": s.effort} if s.effort else {})
        if args.permission_prompt_tool not in (None, "stdio"):
            print(f"noeta: --permission-prompt-tool {args.permission_prompt_tool}: only "
                  "\"stdio\" is supported; permission prompts are refused", file=sys.stderr)

        async def emit(message: dict[str, Any]) -> None:
            if output == "stream-json":
                sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
                sys.stdout.flush()

        conversation = StreamJson(app, options, emit)
        bad: list[str] = []
        if args.input_format == "stream-json":
            result = await drive(conversation, lines=_stdin_lines(bad),
                                 on_bad_line=lambda e: print(f"noeta: {e}", file=sys.stderr))
        else:
            text = _prompt_text(args)
            if not text or not text.strip():
                print("noeta: no prompt (pass one, or pipe it on stdin)", file=sys.stderr)
                return 1
            prompt = Message(role="user", content=[Text(text=text)], origin="human")
            result = await drive(conversation, prompt=prompt)
        for problem in bad:
            print(f"noeta: {problem}", file=sys.stderr)
    if result is None:
        return 0 if args.input_format == "stream-json" else 1
    if output == "json":
        print(json.dumps(result, ensure_ascii=False))
    elif output == "text":
        if result.get("is_error"):
            print("; ".join(result.get("errors") or []) or result.get("result") or "error",
                  file=sys.stderr)
        else:
            print(result.get("result", ""))
    return 1 if result.get("is_error") else 0
