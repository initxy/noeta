"""Running a Workflow script: Node.js executes ``harness.js``, which replays
the script against the agent results known so far.

The model writes JavaScript (Claude Code's Workflow API), so a JavaScript
engine runs it. No dependency-free engine embeds in Python, so the worker's
``node`` (18 or newer) runs it as a short-lived process: the script gets a
fresh V8 context holding only the workflow API, code generation from strings
is off for the whole process, and Node's permission model (where the
version has it) denies files, child processes and workers. Without
``node`` the Workflow tool says so and runs nothing.

A replay is a pure function of (script, args, results): ``agent()`` answers
a call it has a result for and leaves the others waiting, so a run moves
forward one batch of agents at a time and resumes after any restart from
the results recorded in the task's log.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from dataclasses import dataclass, field
from functools import cache
from importlib import resources
from typing import Any

__all__ = ["NODE_ENV", "Engine", "EngineMissing", "Replay", "node_command"]

#: Overrides the ``node`` binary (a path).
NODE_ENV = "NOETA_NODE"
#: Seconds one replay may take before it is killed.
REPLAY_TIMEOUT_SECONDS = 60.0
#: Items one parallel() / pipeline() call accepts.
MAX_ITEMS = 4096
MIN_NODE_MAJOR = 18


class EngineMissing(Exception):
    """No usable Node.js on this worker."""


@dataclass
class Replay:
    """What one replay reached: ``done`` (``value``), ``error`` (``error``) or
    ``blocked`` (``pending`` agent calls), with the script's log lines."""

    status: str
    value: Any = None
    error: str = ""
    pending: list[dict[str, Any]] = field(default_factory=list)
    logs: list[str] = field(default_factory=list)


def _harness() -> str:
    return str(resources.files("noeta.plugins.workflow").joinpath("harness.js"))


@cache
def _node_major(node: str) -> int:
    import subprocess

    try:
        out = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=10,
                             check=False).stdout.strip()
        return int(out.lstrip("v").split(".")[0])
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0


def node_command() -> list[str]:
    """The command that runs the harness; raises ``EngineMissing``."""
    node = os.environ.get(NODE_ENV) or shutil.which("node")
    if not node:
        raise EngineMissing("node was not found on PATH")
    major = _node_major(node)
    if major < MIN_NODE_MAJOR:
        raise EngineMissing(f"{node} is not Node.js {MIN_NODE_MAJOR} or newer")
    harness = _harness()
    cmd = [node, "--disallow-code-generation-from-strings"]
    if major >= 23:
        cmd += ["--permission", f"--allow-fs-read={harness}"]
    elif major >= 20:
        cmd += ["--experimental-permission", f"--allow-fs-read={harness}", "--no-warnings"]
    return [*cmd, harness]


class Engine:
    """Runs the harness. ``command`` replaces the node command (tests)."""

    def __init__(self, command: list[str] | None = None,
                 timeout: float = REPLAY_TIMEOUT_SECONDS) -> None:
        self._command = command
        self.timeout = timeout

    def command(self) -> list[str]:
        return list(self._command) if self._command else node_command()

    async def _call(self, payload: dict[str, Any]) -> dict[str, Any]:
        proc = await asyncio.create_subprocess_exec(
            *self.command(), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE)
        try:
            out, err = await asyncio.wait_for(proc.communicate(json.dumps(payload).encode()),
                                              self.timeout)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return {"status": "error",
                    "error": f"the script did not finish a replay within {self.timeout:.0f} s"}
        try:
            result: dict[str, Any] = json.loads(out or b"{}")
        except ValueError:
            detail = err.decode(errors="replace").strip()[-500:]
            return {"status": "error", "error": f"the script engine failed: {detail}"}
        return result

    async def check(self, script: str) -> dict[str, Any]:
        """``{"meta": {...}}`` for a valid script, else ``{"error": ...}``."""
        return await self._call({"mode": "check", "script": script})

    async def replay(self, script: str, args: Any, results: dict[str, Any],
                     failed: list[str], spent: int = 0) -> Replay:
        out = await self._call({"mode": "run", "script": script, "args": args,
                                "results": results, "failed": failed, "spent": spent,
                                "maxItems": MAX_ITEMS})
        if "error" in out and "status" not in out:
            return Replay(status="error", error=str(out["error"]))
        return Replay(status=str(out.get("status") or "error"), value=out.get("value"),
                      error=str(out.get("error") or ""), pending=list(out.get("pending") or []),
                      logs=[str(x) for x in out.get("logs") or []])
