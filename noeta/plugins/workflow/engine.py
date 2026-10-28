"""Running a Workflow script: an embedded V8 (the ``mini-racer`` package)
evaluates ``harness.js``, which replays the script against the agent results
known so far.

The model writes JavaScript (Claude Code's Workflow API), so a JavaScript
engine runs it, in process and without a system Node.js. Every replay and
every check gets a fresh V8 context that nothing else shares: no module
loader, process, network or file API exists there, code generation from
strings is off, and the clock and randomness throw. A replay has a time limit
and a memory limit; going over either ends it with an error the model reads.
If the engine cannot load on this platform, the Workflow tool says so and
runs nothing.

A replay is a pure function of (script, args, results): ``agent()`` answers
a call it has a result for and leaves the others waiting, so a run moves
forward one batch of agents at a time and resumes after any restart from
the results recorded in the task's log.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from functools import cache
from importlib import resources
from types import ModuleType
from typing import Any

__all__ = ["Engine", "EngineMissing", "Replay"]

#: Seconds one replay (or check) may run before it is stopped.
REPLAY_TIMEOUT_SECONDS = 30.0
#: Bytes of V8 heap one replay may use before it is stopped.
MAX_MEMORY_BYTES = 256 * 1024 * 1024
#: Items one parallel() / pipeline() call accepts.
MAX_ITEMS = 4096
#: Process-wide V8 flags: no eval() or Function() from strings in any context.
V8_FLAGS = ("--disallow-code-generation-from-strings",)


class EngineMissing(Exception):
    """The embedded JavaScript engine cannot load on this worker."""


@dataclass
class Replay:
    """What one replay reached: ``done`` (``value``), ``error`` (``error``) or
    ``blocked`` (``pending`` agent calls), with the script's log lines."""

    status: str
    value: Any = None
    error: str = ""
    pending: list[dict[str, Any]] = field(default_factory=list)
    logs: list[str] = field(default_factory=list)


@cache
def _harness() -> str:
    return resources.files("noeta.plugins.workflow").joinpath("harness.js").read_text("utf-8")


@cache
def _mini_racer() -> ModuleType:
    """The loaded ``py_mini_racer`` module; raises ``EngineMissing``."""
    try:
        import py_mini_racer
    except ImportError as exc:
        raise EngineMissing(f"the mini-racer package is not installed ({exc})") from exc
    try:
        py_mini_racer.init_mini_racer(flags=(*py_mini_racer.DEFAULT_V8_FLAGS, *V8_FLAGS),
                                      ignore_duplicate_init=True)
    except Exception as exc:  # LibNotFoundError, OSError from a missing native library
        raise EngineMissing(f"mini-racer's V8 library does not load here ({exc})") from exc
    return py_mini_racer


class _Stopped(Exception):
    """A replay hit its time or memory limit."""


_JS_PREFIX = re.compile(r"^<anonymous>:\d+: ")


def _js_error(exc: Exception) -> str:
    """The first line of a V8 exception, without its position prefix."""
    first = (str(exc).strip().splitlines() or [""])[0]
    return _JS_PREFIX.sub("", first) or type(exc).__name__


def _settle(ctx: Any) -> None:
    """After a stopped evaluation, let V8 deliver the stopped task's late
    callback before the context closes (it would land on a closed loop and
    print a warning). A script spinning in a microtask is not stopped by a
    time limit, only by closing the context, which follows at once."""
    try:
        ctx.eval("0", timeout_sec=0.25)
    except _mini_racer().JSEvalException:  # still spinning: closing the context ends it
        return


class Engine:
    """Runs scripts in a fresh embedded V8 context per call. ``timeout``
    (seconds) and ``max_memory`` (bytes) bound each replay."""

    def __init__(self, timeout: float = REPLAY_TIMEOUT_SECONDS,
                 max_memory: int = MAX_MEMORY_BYTES) -> None:
        self.timeout = timeout
        self.max_memory = max_memory

    def _limits(self) -> str:
        return (f"the script was stopped: a replay may run {self.timeout:g} s and use "
                f"{self.max_memory // (1024 * 1024)} MB")

    def _run(self, payload: dict[str, Any]) -> dict[str, Any]:
        """One check or replay, blocking; called off the event loop."""
        mr = _mini_racer()
        deadline = time.monotonic() + self.timeout

        def left() -> float:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _Stopped
            return remaining

        with mr.MiniRacer() as ctx:
            ctx.set_hard_memory_limit(self.max_memory)
            try:
                harness = ctx.eval(_harness(), timeout_sec=left())
                cut = json.loads(harness["split"](payload["script"], timeout_sec=left()))
                if "error" in cut:
                    return {"error": cut["error"]}
                if payload["mode"] == "check":
                    return self._meta(ctx, cut["literal"], left)
                harness["run"](json.dumps(payload), timeout_sec=left())
                # From here the script's code runs: only evaluations, never a
                # function call (see harness.js).
                try:
                    ctx.eval(f"__workflow_watch((async () => {{\n{cut['body']}\n}})())",
                             timeout_sec=left())
                except mr.JSParseException as exc:
                    return {"status": "error", "error": _js_error(exc), "logs": []}
                # The script's awaits run as microtasks after that evaluation;
                # this one waits for them, so it carries the time limit.
                out: dict[str, Any] = json.loads(ctx.eval("__workflow_finish()",
                                                          timeout_sec=left()))
                return out
            except (_Stopped, mr.JSTimeoutException, mr.JSOOMException):
                _settle(ctx)
                return {"status": "error", "error": self._limits()}
            except (mr.JSEvalException, ValueError) as exc:  # a broken replay
                if ctx.was_hard_memory_limit_reached():
                    return {"status": "error", "error": self._limits()}
                return {"status": "error", "error": f"the script engine failed: {_js_error(exc)}"}

    def _meta(self, ctx: Any, literal: str, left: Any) -> dict[str, Any]:
        """Evaluate the meta literal with every global deleted first, so a
        variable or a call in it throws; ``stringify`` is held only here."""
        mr = _mini_racer()
        stringify = ctx.eval("JSON.stringify", timeout_sec=left())
        ctx.eval("(() => { const g = globalThis; for (const k of Object.getOwnPropertyNames(g)) "
                 "{ try { delete g[k]; } catch (e) {} } })()", timeout_sec=left())
        try:
            value = ctx.eval(f"({literal})", timeout_sec=left())
            meta = json.loads(stringify(value, timeout_sec=left()) or "null")
        except (mr.JSTimeoutException, mr.JSOOMException):
            raise
        except mr.JSEvalException as exc:
            return {"error": f"meta must be a pure literal: {_js_error(exc)}"}
        if not isinstance(meta, dict):
            return {"error": "meta must be an object"}
        if not isinstance(meta.get("name"), str) or not meta["name"]:
            return {"error": "meta.name is required (a string)"}
        if not isinstance(meta.get("description"), str) or not meta["description"]:
            return {"error": "meta.description is required (a string)"}
        phases = meta.get("phases")
        if phases is not None and (not isinstance(phases, list) or any(
                not isinstance(p, dict) or not isinstance(p.get("title"), str) for p in phases)):
            return {"error": "meta.phases must be a list of {title, detail?}"}
        return {"meta": meta}

    async def _call(self, payload: dict[str, Any]) -> dict[str, Any]:
        _mini_racer()  # raises EngineMissing before any work
        return await asyncio.to_thread(self._run, payload)

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
        pending = [p for p in out.get("pending") or []
                   if isinstance(p, dict) and isinstance(p.get("key"), str)
                   and isinstance(p.get("prompt"), str)]
        return Replay(status=str(out.get("status") or "error"), value=out.get("value"),
                      error=str(out.get("error") or ""), pending=pending,
                      logs=[str(x) for x in out.get("logs") or []])
