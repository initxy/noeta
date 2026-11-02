"""``Workflow``: Claude Code's script orchestration of sub-agents.

The model writes a JavaScript script against the workflow API (``agent()``,
``parallel()``, ``pipeline()``, ``phase()``, ``log()``, ``args``); the
call returns a run id at once and the run goes on in the background. Each
``agent()`` is a child task. The script is replayed by ``engine.py``
against the results recorded so far: a replay that blocks on agents
without a result starts them (up to the concurrency cap) and the next
child's end replays again, until the script returns. Only the results live
in the task's log, so a run survives restarts and moves between workers;
its end arrives as a ``<task-notification>``. ``TaskStop`` stops a run
(``noeta.toolkit.background_tasks.STOP_RUN``): it ends as ``killed``, its
running agents are cancelled, and its notice says it was stopped.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

from noeta.core import models
from noeta.core.events import Cancel, ChildEnded, External, InputTaken, Recorded, Say
from noeta.core.extension import Extension, Reminder, TaskView
from noeta.core.tool import Tool
from noeta.core.types import Message, user
from noeta.plugins.workflow.engine import Engine, EngineMissing, Replay
from noeta.plugins.workflow.opt_in import opt_in_reminders, reduce_opt_in
from noeta.plugins.workflow.tool import WorkflowTool, _ToolView
from noeta.toolkit import notices
from noeta.toolkit.background_tasks import STOP_RUN
from noeta.toolkit.json_schema import schema_errors
from noeta.toolkit.notices import NOTICE_GUARD, escape
from noeta.toolkit.structured_output import SCHEMA_KEY, structured_answer

__all__ = ["Orchestration", "WorkflowTool", "_ToolView"]

NAME = "workflow"
#: Agents one run may start over its life (a runaway-loop backstop).
MAX_AGENTS = 1000
#: The agent type agent() starts when the script names none.
DEFAULT_AGENT = "general-purpose"
#: Parent config a workflow agent copies, as a sub-agent does.
INHERIT = ("cwd", "model", "effort", "permission_mode")

RETURN_NOTE = ("\n\nYou are a step of a workflow script: your final reply is returned to the "
               "script as data, not shown to a person. End with exactly the result asked for.")
SCHEMA_NOTE = ("\n\nYou are a step of a workflow script: return your result by calling the "
               "StructuredOutput tool once, in the shape its input schema gives. The script "
               "reads only that call, never your text; if the call is refused, correct the "
               "shape and call it again.")
#: How the schema note began before agents answered through
#: ``StructuredOutput``: a child it went to answers in JSON text.
JSON_TEXT_NOTE = "You are a step of a workflow script: your final reply is parsed as JSON"


def _resume_call(run: Mapping[str, Any]) -> str:
    """The ``Workflow`` call that resumes ``run`` from its saved script
    (``""`` when the script was not saved)."""
    path = run.get("script_path")
    if not path:
        return ""
    args = run.get("args")
    extra = "" if args is None else f", args: {json.dumps(args, ensure_ascii=False)}"
    return f"Workflow({{scriptPath: '{path}', resumeFromRunId: '{run['run_id']}'{extra}}})"


def _failure(run: Mapping[str, Any], data: Mapping[str, Any]) -> str:
    """One failed agent of ``run``: its label (or key) and why."""
    agent = (run.get("agents") or {}).get(data.get("key")) or {}
    return f"{agent.get('label') or data.get('key')}: {data.get('error') or 'failed'}"


def default_max_running() -> int:
    """Agents one run keeps running at once: CPUs - 2, between 2 and 16."""
    return min(16, max(2, (os.cpu_count() or 4) - 2))


class Sink(Protocol):
    """Where a replay's records and notices go: the task's view, or the
    Workflow call's context while the call runs."""

    post: Callable[..., Awaitable[None]] | None

    def notify(self, text: str, *, kind: str | None = None, data: Any = None) -> None: ...

    def record(self, extension: str, kind: str, data: Any = None, *,
               message: Message | None = None) -> None: ...


def _st(state: Any) -> dict[str, Any]:
    return {"runs": {}, **(state or {})}


def _asked_for_json_text(log: Sequence[Recorded]) -> bool:
    """Whether a child's first message carries the old JSON-text note."""
    for rec in log:
        if isinstance(rec.event, InputTaken):
            for item in rec.event.items:
                if isinstance(item.body, Say):
                    return JSON_TEXT_NOTE in item.body.message.text
    return False


class Orchestration(Extension):
    """Offers ``Workflow``. Constructor: ``engine`` (tests), ``default_agent``
    (the type agent() runs; default ``general-purpose``, else the task's
    own agent), ``max_running`` per run, ``home`` (noeta's config directory,
    holding the user's saved workflows; default ``~/.noeta``)."""

    name = NAME

    def __init__(self, *, engine: Engine | None = None, default_agent: str = DEFAULT_AGENT,
                 max_running: int | None = None, home: str | None = None) -> None:
        self.engine = engine or Engine()
        self.default_agent = default_agent
        self.max_running = max_running or default_max_running()
        self.home = Path(home) if home else None

    # ------------------------------------------------------------ state

    def reduce(self, state: Any, kind: str, data: Any) -> Any:
        st = _st(state)
        opted = reduce_opt_in(st, kind, data)
        if opted is not None:
            return opted
        runs = dict(st["runs"])
        rid = data.get("run_id") if isinstance(data, dict) else None
        run = dict(runs.get(rid) or {})
        if kind == "run":
            run = {**data, "status": "running", "agents": {}, "results": dict(data.get("results")
                                                                               or {}),
                   "failed": [], "logs": 0, "spent": 0, "calls": 0, "failures": []}
        elif kind == "agent" and run:
            run["agents"] = {**run["agents"], data["key"]: {
                k: data.get(k) for k in ("child_id", "label", "phase", "schema")}}
        elif kind == "result" and run:
            run["results"] = {**run["results"], data["key"]: data.get("value")}
            run["spent"] = int(run.get("spent") or 0) + int(data.get("tokens") or 0)
            run["calls"] = int(run.get("calls") or 0) + int(data.get("tool_calls") or 0)
        elif kind == "agent_failed" and run:
            run["failed"] = [*run["failed"], data["key"]]
            run["calls"] = int(run.get("calls") or 0) + int(data.get("tool_calls") or 0)
            run["failures"] = [*(run.get("failures") or []), _failure(run, data)]
        elif kind == "logged" and run:
            run["logs"] = int(data["n"])
        elif kind == "ended" and run:
            run["status"] = data["status"]
        else:
            return st
        runs[rid] = run
        return {**st, "runs": runs}

    def continue_state(self, state: Any) -> Any:
        """A continuation keeps finished runs only: a running one's agents
        are the source task's children and report to it, so here it would
        never settle (or be driven twice)."""
        st = _st(state)
        return {**st, "runs": {rid: r for rid, r in st["runs"].items()
                               if r.get("status") != "running"}}

    def runs(self, view: TaskView) -> dict[str, dict[str, Any]]:
        return dict(_st(view.ext_state(self.name))["runs"])

    # ------------------------------------------------------------ surface

    def tools(self, view: TaskView) -> Sequence[Tool]:
        return (WorkflowTool(self, view),)

    async def reminders(self, view: TaskView) -> Sequence[Reminder]:
        """The ``ultracode`` keyword and switch notes (``opt_in``)."""
        return opt_in_reminders(view, _st(view.ext_state(self.name)))

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
            config[SCHEMA_KEY] = schema
            prompt += SCHEMA_NOTE
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
        """Record the run's end and send its notice (Claude Code's meaning):
        the summary; for a failed or stopped run how to resume it, for a
        completed one its result and how to re-run only what changed; the
        agents that failed; the usage."""
        rid = run["run_id"]
        sink.record(self.name, "ended", {"run_id": rid, "status": status, "error": error})
        name = run["name"] or "Dynamic workflow"
        resume = _resume_call(run)
        body = ""
        if status == "completed":
            summary = f'Dynamic workflow "{name}" completed'
            body += f"\n<result>{escape(json.dumps(value, ensure_ascii=False, indent=2))}" \
                    "\n</result>"
            if resume:
                body += ("\n<diagnostics>To change what the script does with the agents' "
                         f"results and run it again: {escape(resume)}; agents whose prompt "
                         "and options are unchanged answer from this run.</diagnostics>")
        else:
            summary = f'Dynamic workflow "{name}" was stopped' if status == "killed" \
                else f'Dynamic workflow "{name}" failed: {error or "unknown error"}'
            if resume:
                body = (f"\n<recovery>To resume (after editing the script if needed): "
                        f"{escape(resume)}</recovery>")
        failures = [str(f) for f in run.get("failures") or []]
        if failures:
            body += f"\n<failures>{escape(chr(10).join(failures))}</failures>"
        duration = int(max(0.0, time.time() - float(run.get("started_at") or time.time())) * 1000)
        body += (f"\n<usage><agent_count>{len(run['agents'])}</agent_count>"
                 f"<subagent_tokens>{int(run.get('spent') or 0)}</subagent_tokens>"
                 f"<tool_uses>{int(run.get('calls') or 0)}</tool_uses>"
                 f"<duration_ms>{duration}</duration_ms></usage>")
        element = notices.task_notification(
            (("task-id", rid), ("tool-use-id", f"{run.get('call_id', '')}"), ("status", status),
             ("summary", escape(summary))), body=body)
        text = f"{NOTICE_GUARD}\n\n{element}"
        sink.notify(summary, kind="workflow", data={"run_id": rid, "status": status})
        if sink.post is not None:
            await sink.post(Say(message=user(text, origin="system"), when="now"),
                            dedupe_key=f"workflow:end:{rid}")
        else:
            sink.record(self.name, "notice", {"run_id": rid},
                        message=user(text, origin="system"))
        return 0

    async def _take(self, view: TaskView, run: dict[str, Any], key: str,
                    ended: ChildEnded) -> None:
        """Record one agent's end in ``run`` (and in the log). A schema-typed
        agent's result is its accepted ``StructuredOutput`` call, read from
        its log (:meth:`_structured`): one that ended with no value matching
        the schema failed."""
        rid = run["run_id"]
        tokens = ended.last_usage.output if ended.last_usage is not None else 0
        schema = (run["agents"].get(key) or {}).get("schema")
        value: Any = ended.answer
        ok = ended.status == "done"
        if ok and isinstance(schema, dict):
            value = await self._structured(view, ended)
            ok = value is not None and not schema_errors(value, schema)
        if ok:
            view.record(self.name, "result", {"run_id": rid, "key": key, "value": value,
                                              "tokens": tokens, "tool_calls": ended.tool_calls})
            run["results"] = {**run["results"], key: value}
            run["spent"] = int(run.get("spent") or 0) + tokens
        else:
            error = ended.error or ("it gave no result matching its schema"
                                    if ended.status == "done" else ended.status)
            data = {"run_id": rid, "key": key, "error": error, "tool_calls": ended.tool_calls}
            view.record(self.name, "agent_failed", data)
            run["failed"] = [*run["failed"], key]
            run["failures"] = [*(run.get("failures") or []), _failure(run, data)]
        run["calls"] = int(run.get("calls") or 0) + ended.tool_calls

    @staticmethod
    async def _structured(view: TaskView, ended: ChildEnded) -> Any:
        """A child's structured answer: its accepted ``StructuredOutput``
        call, read from its log. Its final text as JSON when there is no
        log reader, or when the child was started (by an older noeta) with
        the note asking for JSON text."""
        if view.read_log is not None:
            log = await view.read_log(ended.child_id)
            value = structured_answer(log)
            if value is not None or not _asked_for_json_text(log):
                return value
        try:
            return json.loads(ended.answer)
        except ValueError:
            return None

    def _spawner(self, view: TaskView) -> Any:
        def spawn(agent: str, prompt: str, config: dict[str, Any], key: str) -> str:
            return view.spawn(agent, prompt, config=config, key=key)
        return spawn

    async def on_event(self, view: TaskView, recorded: Recorded) -> None:
        ev = recorded.event
        if not isinstance(ev, InputTaken):
            return
        stopped: set[str] = set()
        for item in ev.items:
            body = item.body
            if isinstance(body, External) and body.name == STOP_RUN \
                    and isinstance(body.data, Mapping):
                stopped |= await self._stop(view, str(body.data.get("run_id") or ""))
        ended = [i.body for i in ev.items if isinstance(i.body, ChildEnded)]
        if ended:
            await self._settle(view, ended, stopped=stopped)

    async def _stop(self, view: TaskView, rid: str) -> set[str]:
        """End a running run as stopped and cancel its running agents (their
        ends then change nothing: the run is no longer running). Returns the
        run stopped, if any."""
        run = self.runs(view).get(rid)
        if run is None or run["status"] != "running":
            return set()
        for key, agent in run["agents"].items():
            child = agent.get("child_id")
            if child and key not in run["results"] and key not in run["failed"] \
                    and view.state.children.get(child) is None:
                view.enqueue(Cancel(reason="its workflow run was stopped"), task_id=child)
        await self._end(view, run, "killed")
        return {rid}

    async def on_start(self, view: TaskView) -> None:
        """Runs a crash left between an agent's end and the next replay."""
        missed = [c for c in view.state.children.values() if c is not None]
        await self._settle(view, missed, every_run=True)

    async def _settle(self, view: TaskView, ended: Sequence[ChildEnded], *,
                      every_run: bool = False, stopped: set[str] | None = None) -> None:
        runs = {rid: dict(r) for rid, r in self.runs(view).items()
                if r["status"] == "running" and rid not in (stopped or ())}
        touched: set[str] = set()
        for e in ended:
            for rid, run in runs.items():
                for key, a in run["agents"].items():
                    if a.get("child_id") == e.child_id and key not in run["results"] \
                            and key not in run["failed"]:
                        await self._take(view, run, key, e)
                        touched.add(rid)
        if every_run:
            touched |= {rid for rid, run in runs.items()
                        if all(k in run["results"] or k in run["failed"] for k in run["agents"])}
        for rid in sorted(touched):
            await self.drive(view, view, runs[rid], self._spawner(view))
