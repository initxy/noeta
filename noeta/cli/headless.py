"""``noeta -p``: Claude Code's headless mode on stdin and stdout, with its
flags (``noeta.host.stream_json`` speaks the protocol).

    noeta -p "fix the test"                        print the answer
    noeta -p --output-format json "…"              print the result object
    noeta -p --output-format stream-json --verbose "…"
    noeta --input-format stream-json --output-format stream-json --verbose
                                                   what Claude Code's Agent SDK runs

``--settings``, ``--setting-sources``, ``--mcp-config`` (with
``--strict-mcp-config``), ``--agents`` and ``--plugin-dir`` work as in Claude
Code, on noeta's own places. Flags Claude Code has and noeta does not use (``--betas`` …)
are accepted and ignored, so an SDK's command line runs unchanged; each
ignored one is named on stderr.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import stat
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import yaml

from noeta.agents.agent_files import AgentFile, AgentFileError, parse_agent_file
from noeta.cli.config import Settings
from noeta.toolkit.hook_callbacks import HookCallbacks
from noeta.toolkit.local_env import local_timezone

#: Accepted for compatibility, not applied (value-taking, then flags).
IGNORED_WITH_VALUE = ("--fallback-model",
                      "--betas", "--max-thinking-tokens", "--thinking", "--thinking-display",
                      "--plugin-url", "--task-budget", "--debug-file",
                      "--name", "--autocompact", "--file", "--system-prompt-snapshot")
IGNORED_FLAGS = ("--include-hook-events", "--no-session-persistence",
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
                    help='"stdio": the client answers permission prompts (can_use_tool); '
                         "an MCP tool name (mcp__<server>__<tool>): that tool answers them")
    ap.add_argument("--permission-prompts", choices=("host", "none"), default="host")
    ap.add_argument("--dangerously-skip-permissions", action="store_true")
    ap.add_argument("-c", "--continue", dest="continue_last", action="store_true",
                    help="continue the newest task started in this directory")
    ap.add_argument("--fork-session", dest="fork", action="store_true",
                    help="with --resume/--continue: go on in a new task")
    ap.add_argument("--session-id", dest="new_task_id", help="the id the new task gets")
    ap.add_argument("--agent", default=None,
                    help="the agent to run (default: the settings' agent, else main)")
    ap.add_argument("--system-prompt-file")
    ap.add_argument("--append-system-prompt-file")
    ap.add_argument("--settings", dest="settings_flag",
                    help="a settings file or JSON string, above the project's settings")
    ap.add_argument("--setting-sources", dest="setting_sources",
                    help='the settings files read: "user,project,local" (managed always)')
    ap.add_argument("--mcp-config", dest="mcp_config", nargs="+", action="extend", default=[],
                    help="MCP servers: JSON files or strings holding mcpServers")
    ap.add_argument("--strict-mcp-config", action="store_true",
                    help="use only the --mcp-config servers")
    ap.add_argument("--agents", dest="agents_json",
                    help="sub-agents as a JSON object: name -> {description, prompt, ...}")
    ap.add_argument("--plugin-dir", dest="plugin_dirs", action="append", default=[],
                    metavar="PATH", help="load a plugin file or directory for this run only")
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


class FlagError(ValueError):
    """A flag whose value cannot be used."""


def _json_or_file(value: str, flag: str) -> Any:
    """A flag value that is a JSON string or a file holding JSON."""
    text = value.strip()
    if not text.startswith("{"):
        try:
            text = Path(value).expanduser().read_text("utf-8")
        except OSError as exc:
            raise FlagError(f"{flag}: cannot read {value}: {exc}") from None
    try:
        return json.loads(text)
    except ValueError as exc:
        raise FlagError(f"{flag}: not JSON: {exc}") from None


def json_schema(value: str) -> dict[str, Any]:
    """``--json-schema``: the schema, a JSON object whose root is an object
    type (it becomes the ``StructuredOutput`` tool's input schema)."""
    from noeta.toolkit.structured_output import answer_schema_problem

    try:
        schema = json.loads(value)
    except ValueError as exc:
        raise FlagError(f"--json-schema is not a valid JSON Schema: not JSON: {exc}") from None
    problem = answer_schema_problem(schema)
    if problem is not None:
        raise FlagError(f"--json-schema is not a valid JSON Schema: {problem}")
    return dict(schema)


def setting_sources(args: argparse.Namespace) -> tuple[str, ...] | None:
    """``--setting-sources``: the settings layers read (``None``: all; the
    empty value Claude Code's SDK passes: none but the managed one)."""
    from noeta.cli.settings_files import SETTING_SOURCES

    if args.setting_sources is None:
        return None
    names = tuple(n.strip() for n in args.setting_sources.split(",") if n.strip())
    unknown = [n for n in names if n not in SETTING_SOURCES]
    if unknown:
        raise FlagError(f"--setting-sources: unknown source {', '.join(unknown)} "
                        f"(valid: {', '.join(SETTING_SOURCES)})")
    return names


def settings_layer(args: argparse.Namespace, scratch: Path) -> Path | None:
    """``--settings``: the file, or a JSON string written to a file in
    ``scratch`` (the hooks and MCP plugins read settings by file)."""
    value = args.settings_flag
    if not value:
        return None
    data = _json_or_file(value, "--settings")
    if not isinstance(data, dict):
        raise FlagError("--settings: must be a JSON object")
    if not value.strip().startswith("{"):
        return Path(value).expanduser().resolve()
    text = json.dumps(data, sort_keys=True)
    scratch.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = scratch / f"settings-{hashlib.sha256(text.encode()).hexdigest()[:16]}.json"
    path.write_text(text, "utf-8")
    path.chmod(0o600)
    return path


def hooks_arguments(args: argparse.Namespace) -> dict[str, Any]:
    """The hooks plugin's arguments: where the client's ``initialize``
    hooks go (``callbacks``, handed to :func:`run` too), and the MCP tool
    ``--permission-prompt-tool`` names."""
    out: dict[str, Any] = {"callbacks": HookCallbacks()}
    tool = args.permission_prompt_tool
    if tool not in (None, "stdio") and args.permission_prompts == "host":
        if not str(tool).startswith("mcp__") or "__" not in str(tool)[5:]:
            raise FlagError(f"--permission-prompt-tool {tool}: must be \"stdio\" or an MCP "
                            "tool name (mcp__<server>__<tool>)")
        out["permission_prompt_tool"] = tool
    return out


def mcp_arguments(args: argparse.Namespace) -> dict[str, Any] | None:
    """The MCP plugin's arguments ``--mcp-config`` gives: each value's
    ``mcpServers`` merged (a later one wins a name); with
    ``--strict-mcp-config`` only those, no settings files or project
    servers."""
    if not args.mcp_config and not args.strict_mcp_config:
        return None
    servers: dict[str, Any] = {}
    for value in args.mcp_config:
        data = _json_or_file(value, "--mcp-config")
        table = data.get("mcpServers") if isinstance(data, dict) else None
        if not isinstance(table, dict):
            raise FlagError(f"--mcp-config: {value[:60]}: needs an 'mcpServers' object")
        servers.update(table)
    out: dict[str, Any] = {"servers": servers}
    if args.strict_mcp_config:
        out.update(settings_files=[], project=False)
    return out


def agent_files(args: argparse.Namespace) -> list[AgentFile]:
    """``--agents``: Claude Code's JSON object of sub-agents (name ->
    ``description``, ``prompt``, and the agent-file fields), read as agent
    files."""
    if not args.agents_json:
        return []
    data = _json_or_file(args.agents_json, "--agents")
    if not isinstance(data, dict):
        raise FlagError("--agents: must be a JSON object of agents by name")
    out = []
    for name, spec in data.items():
        if not isinstance(spec, dict) or not isinstance(spec.get("prompt"), str):
            raise FlagError(f"--agents: {name}: needs a 'prompt' string")
        front = {k: v for k, v in spec.items() if k != "prompt"}
        front["name"] = name
        text = f"---\n{yaml.safe_dump(front, sort_keys=False)}---\n{spec['prompt']}"
        try:
            out.append(parse_agent_file(text, source=f"--agents {name}"))
        except AgentFileError as exc:
            raise FlagError(str(exc)) from None
    return out


def _prompt_text(args: argparse.Namespace) -> str | None:
    if args.prompt is not None:
        return str(args.prompt)
    if args.input_format == "stream-json" or sys.stdin.isatty():
        return None
    return sys.stdin.read()


async def _raw_stdin_lines() -> AsyncIterator[bytes]:
    mode = os.fstat(sys.stdin.fileno()).st_mode
    if not (stat.S_ISFIFO(mode) or stat.S_ISSOCK(mode) or stat.S_ISCHR(mode)):
        # A regular file (``< in.jsonl``): the event loop's pipe transport
        # refuses it, and reading it never blocks for long.
        while line := await asyncio.to_thread(sys.stdin.buffer.readline):
            yield line
        return
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=64 * 1024 * 1024)
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
    while line := await reader.readline():
        yield line


async def _stdin_lines(bad: list[str]) -> AsyncIterator[dict[str, Any]]:
    async for line in _raw_stdin_lines():
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


async def run(app: Any, s: Settings, args: argparse.Namespace, *, cwd: str,
              hook_callbacks: HookCallbacks | None = None) -> int:
    """Run headless on ``app`` (a ``LocalApp``; ``hook_callbacks``: those
    its hooks plugin was given); returns the exit code."""
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
    schema = None
    if args.json_schema:
        try:
            schema = json_schema(args.json_schema)
        except FlagError as exc:
            print(f"noeta: {exc}", file=sys.stderr)
            return 1
    mode = args.permission_mode
    if args.dangerously_skip_permissions:
        mode = "bypassPermissions"
    agent = args.agent or getattr(app, "main_agent", "main")
    async with app:  # app.notices were logged to stderr when the app was made
        resume = args.resume or None
        if args.continue_last and not resume:
            resume = await find_latest_task(app.client, cwd, agent)
            if resume is None:
                print("noeta: no task to continue in this directory", file=sys.stderr)
                return 1
        options = HeadlessOptions(
            agent=agent, model=s.model, cwd=cwd, permission_mode=mode,
            max_turns=args.max_turns, max_budget_usd=args.max_budget_usd,
            output_schema=schema, include_partial_messages=args.include_partial_messages,
            replay_user_messages=args.replay_user_messages,
            ask_client=args.permission_prompt_tool == "stdio"
            and args.permission_prompts == "host",
            resume=resume, fork=bool(args.fork), task_id=args.new_task_id,
            hook_callbacks=hook_callbacks,
            config={**({"effort": s.effort} if s.effort else {}),
                    **({"timezone": zone} if (zone := local_timezone()) else {})})

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
