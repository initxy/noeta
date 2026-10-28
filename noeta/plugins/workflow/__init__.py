"""``Workflow``: Claude Code's script orchestration of sub-agents.

The model writes a JavaScript script against the workflow API (``agent()``,
``parallel()``, ``pipeline()``, ``phase()``, ``log()``, ``args``); the
call returns a run id at once and the run goes on in the background. Each
``agent()`` is a child task. The script is replayed by ``engine.py``
against the results recorded so far: a replay that blocks on agents
without a result starts them (up to the concurrency cap) and the next
child's end replays again, until the script returns. Only the results live
in the task's log, so a run survives restarts and moves between workers;
its end arrives as a ``<task-notification>``.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

from noeta.core import models
from noeta.core.events import ChildEnded, InputTaken, Recorded, Say
from noeta.core.extension import Extension, TaskView
from noeta.core.tool import Tool, ToolContext, ToolOutput, text_output
from noeta.core.types import Message, user
from noeta.plugins.workflow.engine import Engine, EngineMissing, Replay
from noeta.toolkit.notices import NOTICE_GUARD, escape
from noeta.toolkit.tools import SCHEMA_DIALECT, load_description

__all__ = ["Orchestration", "WorkflowTool"]

NAME = "workflow"
TOOL = "Workflow"
RUN_ID = re.compile(r"^wf_[a-z0-9-]{6,}$")
MAX_SCRIPT_CHARS = 524_288
#: Agents one run may start over its life (a runaway-loop backstop).
MAX_AGENTS = 1000
#: The agent type agent() starts when the script names none.
DEFAULT_AGENT = "general-purpose"
#: Parent config a workflow agent copies, as a sub-agent does.
INHERIT = ("cwd", "model", "effort", "permission_mode")

RETURN_NOTE = ("\n\nYou are a step of a workflow script: your final reply is returned to the "
               "script as data, not shown to a person. End with exactly the result asked for.")
SCHEMA_NOTE = ("\n\nYou are a step of a workflow script: your final reply is parsed as JSON "
               "matching this schema and returned to the script:\n{schema}")
NO_ENGINE = ("Workflow needs Node.js {major} or newer on the worker that runs this task "
             "({reason}); nothing was started. Use the Agent tool instead, or install Node.js "
             "(or set NOETA_NODE to its path).")
STARTED = ('Workflow "{name}" started in the background as {run_id}: {launched} agent(s) '
           "running. A <task-notification> arrives when it finishes; do not wait for it or "
           "guess its result.{saved}")
SAVED = (" The script is saved at {path}: to change the workflow, edit that file and call "
         "Workflow with scriptPath (add resumeFromRunId to reuse finished agents).")


def default_max_running() -> int:
    """Agents one run keeps running at once: min(16, CPUs - 2), at least 1."""
    return max(1, min(16, (os.cpu_count() or 4) - 2))


class Sink(Protocol):
    """Where a replay's records and notices go: the task's view, or the
    Workflow call's context while the call runs."""

    post: Callable[..., Awaitable[None]] | None

    def notify(self, text: str, *, kind: str | None = None, data: Any = None) -> None: ...

    def record(self, extension: str, kind: str, data: Any = None, *,
               message: Message | None = None) -> None: ...


def _st(state: Any) -> dict[str, Any]:
    return {"runs": {}, **(state or {})}


def _validate(value: Any, schema: Mapping[str, Any]) -> bool:
    """A light check of a parsed schema-typed answer: an object holding the
    required properties."""
    if not isinstance(value, dict):
        return False
    return all(k in value for k in schema.get("required") or [])


class Orchestration(Extension):
    """Offers ``Workflow``. Constructor: ``engine`` (tests), ``default_agent``
    (the type agent() runs; default ``general-purpose``, else the task's
    own agent), ``max_running`` per run."""

    name = NAME

    def __init__(self, *, engine: Engine | None = None, default_agent: str = DEFAULT_AGENT,
                 max_running: int | None = None) -> None:
        self.engine = engine or Engine()
        self.default_agent = default_agent
        self.max_running = max_running or default_max_running()

    # ------------------------------------------------------------ state

    def reduce(self, state: Any, kind: str, data: Any) -> Any:
        st = _st(state)
        runs = dict(st["runs"])
        rid = data.get("run_id") if isinstance(data, dict) else None
        run = dict(runs.get(rid) or {})
        if kind == "run":
            run = {**data, "status": "running", "agents": {}, "results": dict(data.get("results")
                                                                               or {}),
                   "failed": [], "logs": 0, "spent": 0}
        elif kind == "agent" and run:
            run["agents"] = {**run["agents"], data["key"]: {
                k: data.get(k) for k in ("child_id", "label", "phase", "schema")}}
        elif kind == "result" and run:
            run["results"] = {**run["results"], data["key"]: data.get("value")}
            run["spent"] = int(run.get("spent") or 0) + int(data.get("tokens") or 0)
        elif kind == "agent_failed" and run:
            run["failed"] = [*run["failed"], data["key"]]
        elif kind == "logged" and run:
            run["logs"] = int(data["n"])
        elif kind == "ended" and run:
            run["status"] = data["status"]
        else:
            return st
        runs[rid] = run
        return {**st, "runs": runs}

    def runs(self, view: TaskView) -> dict[str, dict[str, Any]]:
        return dict(_st(view.ext_state(self.name))["runs"])

    # ------------------------------------------------------------ surface

    def tools(self, view: TaskView) -> Sequence[Tool]:
        return (WorkflowTool(self, view),)

    # ------------------------------------------------------------ driving

    def _agent_type(self, view: TaskView, wanted: Any) -> str:
        if isinstance(wanted, str) and wanted in view.agents:
            return wanted
        return self.default_agent if self.default_agent in view.agents else view.state.agent

    def _child(self, view: TaskView, call: Mapping[str, Any]) -> tuple[str, str, dict[str, Any]]:
        """The agent, prompt and config of one agent() call."""
        opts = call.get("options") or {}
        config = {k: view.state.config[k] for k in INHERIT if k in view.state.config}
        if isinstance(opts.get("model"), str):
            config["model"] = models.resolve_alias(opts["model"])
        if isinstance(opts.get("effort"), str):
            config["effort"] = opts["effort"]
        prompt = str(call["prompt"])
        schema = opts.get("schema")
        if isinstance(schema, dict):
            config["output_schema"] = schema
            prompt += SCHEMA_NOTE.format(schema=json.dumps(schema, sort_keys=True))
        else:
            prompt += RETURN_NOTE
        return self._agent_type(view, opts.get("agentType")), prompt, config

    async def drive(self, sink: Sink, view: TaskView, run: dict[str, Any], spawn: Any) -> int:
        """Replay ``run`` and act on where it got: start the agents it waits
        for (``spawn(agent, prompt, config, key)`` returns a child id), or
        record its end and notify. Returns how many agents it started."""
        rid = run["run_id"]
        try:
            replay = await self.engine.replay(run["script"], run.get("args"), run["results"],
                                              list(run["failed"]), int(run.get("spent") or 0))
        except EngineMissing as exc:
            replay = Replay(status="error", error=f"the script engine is gone: {exc}")
        for line in replay.logs[int(run.get("logs") or 0):]:
            sink.notify(f"[{run['name']}] {line}", kind="workflow",
                        data={"run_id": rid, "log": line})
        if len(replay.logs) > int(run.get("logs") or 0):
            sink.record(self.name, "logged", {"run_id": rid, "n": len(replay.logs)})
        if replay.status == "blocked":
            agents = run["agents"]
            live = [k for k, a in agents.items() if k not in run["results"]
                    and k not in run["failed"]]
            started = 0
            for call in replay.pending:
                key = call["key"]
                if key in agents:
                    continue
                if len(agents) + started >= MAX_AGENTS:
                    return await self._end(sink, run, "failed", error=(
                        f"the run reached {MAX_AGENTS} agents"))
                if len(live) + started >= self.max_running:
                    break
                agent, prompt, config = self._child(view, call)
                child = spawn(agent, prompt, config, f"{rid}-{len(agents) + started + 1}")
                sink.record(self.name, "agent", {
                    "run_id": rid, "key": key, "child_id": child, "label": call.get("label"),
                    "phase": call.get("phase"), "schema": (call.get("options") or {}).get(
                        "schema")})
                sink.notify(f"[{run['name']}] started {call.get('label') or 'agent'}",
                            kind="workflow", data={"run_id": rid, "agent": child,
                                                   "label": call.get("label"),
                                                   "phase": call.get("phase")})
                started += 1
            if not live and not started:
                return await self._end(sink, run, "failed",
                                       error="the script waits on agents that never started")
            return started
        if replay.status == "done":
            return await self._end(sink, run, "completed", value=replay.value)
        return await self._end(sink, run, "failed", error=replay.error or "the script failed")

    async def _end(self, sink: Sink, run: dict[str, Any], status: str, *,
                   value: Any = None, error: str = "") -> int:
        rid = run["run_id"]
        sink.record(self.name, "ended", {"run_id": rid, "status": status, "error": error})
        agents = len(run["agents"])
        if status == "completed":
            summary = f'Workflow "{run["name"]}" completed'
            result = json.dumps(value, ensure_ascii=False, indent=2)
        else:
            summary = f'Workflow "{run["name"]}" failed: {error}'
            result = error
        duration = int(max(0.0, time.time() - float(run.get("started_at") or time.time())) * 1000)
        text = (f"{NOTICE_GUARD}\n\n<task-notification>\n<task-id>{rid}</task-id>\n"
                f"<tool-use-id>{run.get('call_id', '')}</tool-use-id>\n<status>{status}</status>\n"
                f"<summary>{escape(summary)}</summary>\n<result>{escape(result)}\n</result>\n"
                f"<usage><subagent_tokens>{int(run.get('spent') or 0)}</subagent_tokens>"
                f"<agents>{agents}</agents><duration_ms>{duration}</duration_ms></usage>\n"
                "</task-notification>")
        sink.notify(summary, kind="workflow", data={"run_id": rid, "status": status})
        if sink.post is not None:
            await sink.post(Say(message=user(text, origin="system"), when="now"),
                            dedupe_key=f"workflow:end:{rid}")
        else:
            sink.record(self.name, "notice", {"run_id": rid},
                        message=user(text, origin="system"))
        return 0

    def _take(self, view: TaskView, run: dict[str, Any], key: str, ended: ChildEnded) -> None:
        """Record one agent's end in ``run`` (and in the log)."""
        rid = run["run_id"]
        tokens = ended.last_usage.output if ended.last_usage is not None else 0
        schema = (run["agents"].get(key) or {}).get("schema")
        value: Any = ended.answer
        ok = ended.status == "done"
        if ok and isinstance(schema, dict):
            try:
                value = json.loads(ended.answer)
            except ValueError:
                ok = False
            else:
                ok = _validate(value, schema)
        if ok:
            view.record(self.name, "result", {"run_id": rid, "key": key, "value": value,
                                              "tokens": tokens})
            run["results"] = {**run["results"], key: value}
            run["spent"] = int(run.get("spent") or 0) + tokens
        else:
            view.record(self.name, "agent_failed", {"run_id": rid, "key": key})
            run["failed"] = [*run["failed"], key]

    def _spawner(self, view: TaskView) -> Any:
        def spawn(agent: str, prompt: str, config: dict[str, Any], key: str) -> str:
            return view.spawn(agent, prompt, config=config, key=key)
        return spawn

    async def on_event(self, view: TaskView, recorded: Recorded) -> None:
        ev = recorded.event
        if not isinstance(ev, InputTaken):
            return
        ended = [i.body for i in ev.items if isinstance(i.body, ChildEnded)]
        if ended:
            await self._settle(view, ended)

    async def on_start(self, view: TaskView) -> None:
        """Runs a crash left between an agent's end and the next replay."""
        missed = [c for c in view.state.children.values() if c is not None]
        await self._settle(view, missed, every_run=True)

    async def _settle(self, view: TaskView, ended: Sequence[ChildEnded], *,
                      every_run: bool = False) -> None:
        runs = {rid: dict(r) for rid, r in self.runs(view).items() if r["status"] == "running"}
        touched: set[str] = set()
        for e in ended:
            for rid, run in runs.items():
                for key, a in run["agents"].items():
                    if a.get("child_id") == e.child_id and key not in run["results"] \
                            and key not in run["failed"]:
                        self._take(view, run, key, e)
                        touched.add(rid)
        if every_run:
            touched |= {rid for rid, run in runs.items()
                        if all(k in run["results"] or k in run["failed"] for k in run["agents"])}
        for rid in sorted(touched):
            await self.drive(view, view, runs[rid], self._spawner(view))


class WorkflowTool:
    name = TOOL
    description = load_description("noeta.plugins.workflow", "workflow")
    read_only = False
    clearable = False
    input_schema: dict[str, Any] = {
        "$schema": SCHEMA_DIALECT,
        "type": "object",
        "properties": {
            "script": {"type": "string", "maxLength": MAX_SCRIPT_CHARS, "description": (
                "The whole workflow script: it starts with `export const meta = { name, "
                "description, phases }` (a pure literal, nothing computed), then the body "
                "using agent() / parallel() / pipeline() / phase().")},
            "name": {"type": "string", "description": (
                "The name of a saved workflow (in .claude/workflows/ or .noeta/workflows/ as "
                "<name>.js), which supplies the script.")},
            "description": {"type": "string", "description": (
                "Ignored: the description lives in the script's `meta`.")},
            "title": {"type": "string", "description": (
                "Ignored: the title lives in the script's `meta`.")},
            "args": {"description": (
                "Any value; the script reads it as the global `args`, unchanged. Pass lists "
                "and objects as JSON values (`args: [\"a.ts\", \"b.ts\"]`), never as a string "
                "of JSON: a string reaches the script as one string, so `args.map` fails. Use "
                "it to give a saved workflow its input, such as a question or a path.")},
            "scriptPath": {"type": "string", "description": (
                "The path of a script file to run. Every run saves its script and returns the "
                "path; edit that file and pass it here instead of resending the script. Wins "
                "over `script` and `name`.")},
            "resumeFromRunId": {"type": "string", "pattern": "^wf_[a-z0-9-]{6,}$",
                                "description": (
                "An earlier run of this task to resume: agent() calls whose prompt and options "
                "did not change return that run's results, and only edited or new calls run. "
                "The earlier run must have ended.")},
        },
        "additionalProperties": False,
    }

    def __init__(self, ext: Orchestration, view: TaskView) -> None:
        self.ext = ext
        self.view = view

    async def _script(self, input: Mapping[str, Any], ctx: ToolContext) -> str | ToolOutput:
        path = input.get("scriptPath")
        if isinstance(path, str) and path:
            try:
                return (await ctx.env.read_bytes(path)).decode("utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                return text_output(f"{TOOL}: cannot read the script {path}: {exc}", is_error=True)
        name = input.get("name")
        if isinstance(name, str) and name:
            if not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
                return text_output(f"{TOOL}: {name!r} is not a workflow name.", is_error=True)
            cwd = str(self.view.state.config.get("cwd") or ctx.env.cwd)
            for base in (".claude/workflows", ".noeta/workflows"):
                try:
                    return (await ctx.env.read_bytes(f"{cwd}/{base}/{name}.js")).decode("utf-8")
                except (OSError, UnicodeDecodeError):
                    continue
            return text_output(f"{TOOL}: no saved workflow is named {name!r}.", is_error=True)
        script = input.get("script")
        if isinstance(script, str) and script.strip():
            return script
        return text_output(f"{TOOL}: give `script`, `scriptPath` or `name`.", is_error=True)

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        script = await self._script(input, ctx)
        if isinstance(script, ToolOutput):
            return script
        if len(script) > MAX_SCRIPT_CHARS:
            return text_output(f"{TOOL}: the script is over {MAX_SCRIPT_CHARS} characters.",
                               is_error=True)
        if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f​-‏‪-‮⁦-⁩]",
                     script):
            return text_output(f"{TOOL}: the script holds control or invisible characters "
                               "that the approval prompt would hide.", is_error=True)
        runs = self.ext.runs(self.view)
        results: dict[str, Any] = {}
        resume = input.get("resumeFromRunId")
        if resume is not None:
            if not isinstance(resume, str) or not RUN_ID.match(resume) or resume not in runs:
                return text_output(f"{TOOL}: no earlier run {resume!r} in this task.",
                                   is_error=True)
            if runs[resume]["status"] == "running":
                return text_output(f"{TOOL}: run {resume} is still running; wait for it to end "
                                   "before resuming from it.", is_error=True)
            results = dict(runs[resume]["results"])
        try:
            checked = await self.ext.engine.check(script)
        except EngineMissing as exc:
            from noeta.plugins.workflow.engine import MIN_NODE_MAJOR

            return text_output(NO_ENGINE.format(major=MIN_NODE_MAJOR, reason=exc), is_error=True)
        if "error" in checked:
            return text_output(f"{TOOL}: {checked['error']}. The script must be plain "
                               "JavaScript that starts with the meta literal.", is_error=True)
        meta = checked["meta"]
        rid = f"wf_{secrets.token_hex(5)}"
        path = self._save(ctx, rid, script)
        run: dict[str, Any] = {
            "run_id": rid, "name": str(meta["name"]), "description": str(meta["description"]),
            "phases": meta.get("phases") or [], "script": script, "args": input.get("args"),
            "call_id": ctx.call_id, "started_at": time.time(), "results": results,
            "script_path": path}
        ctx.record(NAME, "run", run)
        run = {**run, "status": "running", "agents": {}, "failed": [], "logs": 0, "spent": 0}
        view = _ToolView(ctx)

        def spawn(agent: str, prompt: str, config: dict[str, Any], key: str) -> str:
            assert ctx.spawn is not None
            return ctx.spawn(agent, prompt, config=config, background=True)

        launched = await self.ext.drive(view, self.view, run, spawn)
        ctx.notify(f'workflow "{run["name"]}" started: {meta["description"]}', kind="workflow",
                   data={"run_id": rid, "name": run["name"], "description": run["description"],
                         "phases": run["phases"]})
        saved = SAVED.format(path=path) if path else ""
        return text_output(STARTED.format(name=run["name"], run_id=rid, launched=launched,
                                          saved=saved), run_id=rid, script_path=path)

    @staticmethod
    def _save(ctx: ToolContext, rid: str, script: str) -> str:
        """Keep the script beside the task's other spilled files."""
        if not ctx.spill_dir:
            return ""
        path = Path(ctx.spill_dir) / "workflows" / f"{rid}.js"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(script, encoding="utf-8")
        except OSError:
            return ""
        return str(path)


class _ToolView:
    """What ``Orchestration.drive`` needs while the tool runs: notices and records
    go through the call's context (recorded with its result), the end
    notice through the task's inbox."""

    def __init__(self, ctx: ToolContext) -> None:
        self._ctx = ctx
        self.post: Callable[..., Awaitable[None]] | None = self._post if ctx.post else None

    async def _post(self, body: Any, dedupe_key: str | None = None) -> None:
        assert self._ctx.post is not None
        await self._ctx.post(body)

    def notify(self, text: str, *, kind: str | None = None, data: Any = None) -> None:
        self._ctx.notify(text, kind=kind, data=data)

    def record(self, extension: str, kind: str, data: Any = None, *,
               message: Message | None = None) -> None:
        self._ctx.record(extension, kind, data)
