"""Run one scenario trial on the default agents and build its ``Trace``.

A trial is set up as a host sets up a task: plugin settings by name (rules,
hooks, MCP servers), the permission mode in the task's config, approvals and
questions answered through ``Client.answer`` from ``noeta.host.pending``, and
later turns sent with ``Client.send``.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import tempfile
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from evals import live_env as _live_env
from evals.core import EvalConfig, Reply, Request, Scenario, Trace, TrialDirs, in_repo, snapshot
from noeta.agents import default_agents
from noeta.backends.execenvs.local import LocalExecEnv
from noeta.backends.providers import provider_for
from noeta.backends.stores.sqlite import SqliteStore
from noeta.core import records
from noeta.core.agent import Agent
from noeta.core.client import Client
from noeta.core.events import (
    InputTaken,
    ModelReplied,
    Say,
    TaskEnded,
    ToolFinished,
)
from noeta.core.execenv import RunResult
from noeta.core.provider import Provider
from noeta.core.questions import detail_questions
from noeta.core.runner import Runtime
from noeta.core.types import Image, Message, Text
from noeta.host.pending import Pending, pending_tree
from noeta.plugins.mcp import default_pool
from noeta.plugins.memory import memory_dir_for
from noeta.toolkit.permission_modes import MODE_SIGNAL, current_mode

_STEP_BUDGET = 60
_POLL_S = 0.2
#: Polls a wait on the person may show nothing to answer before the trial
#: is called broken (a wait the host cannot see would spin to the timeout).
_STUCK_POLLS = 150

_home_lock = threading.Lock()
_home: Path | None = None
_venv: Path | None = None


def isolate_home() -> Path:
    """Point ``NOETA_HOME`` at a fresh directory for this process (once), so
    what noeta keeps in its home never touches the user's. It is process-wide
    (noeta reads the variable when it needs the home), so concurrent trials
    share it; each trial's plan files and memory have their own directories
    under its root."""
    global _home
    with _home_lock:
        if _home is None:
            _home = Path(tempfile.mkdtemp(prefix="noeta-eval-home-"))
            os.environ["NOETA_HOME"] = str(_home)
        return _home


def trial_venv() -> Path:
    """A throwaway virtualenv with pytest (once per process, under the eval
    home): the Python the model's commands find, so nothing they run comes
    from the repo's own development environment."""
    global _venv
    home = isolate_home()
    with _home_lock:
        if _venv is None:
            venv = home / "venv"
            subprocess.run(["uv", "venv", "--quiet", "--python", "3.12", str(venv)],
                           check=True, capture_output=True, env=_outside_repo_env())
            subprocess.run(["uv", "pip", "install", "--quiet", "--python",
                            str(venv / "bin" / "python"), "pytest"],
                           check=True, capture_output=True, env=_outside_repo_env())
            _venv = venv
        return _venv


def _outside_repo_path() -> str:
    """``PATH`` without the repo's directories (``uv run`` puts its .venv first)."""
    return os.pathsep.join(d for d in os.environ.get("PATH", "").split(os.pathsep)
                           if d and not in_repo(d))


def _outside_repo_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in ("VIRTUAL_ENV", "PYTHONPATH")}
    env["PATH"] = _outside_repo_path()
    return env


class _TrialEnv(LocalExecEnv):
    """The local exec env with the trial's own ``HOME`` (and XDG
    directories), and the throwaway virtualenv in place of the repo's on
    ``PATH``: commands the model runs never touch the user's dotfiles, and
    find nothing that leads back to the repo (where the eval's answers are)."""

    def __init__(self, cwd: str, home: Path) -> None:
        super().__init__(cwd)
        venv = trial_venv()
        self._home_env = {"HOME": str(home), "XDG_CONFIG_HOME": str(home / ".config"),
                          "XDG_CACHE_HOME": str(home / ".cache"),
                          "XDG_DATA_HOME": str(home / ".local" / "share"),
                          "VIRTUAL_ENV": str(venv), "PYTHONPATH": "",
                          "PATH": os.pathsep.join([str(venv / "bin"), _outside_repo_path()])}

    async def run(self, argv: Any, *, cwd: str | None = None,
                  env: Mapping[str, str] | None = None, stdin: bytes | None = None,
                  timeout: float | None = None, output_cap: int | None = None) -> RunResult:
        return await super().run(argv, cwd=cwd, env={**self._home_env, **(env or {})},
                                 stdin=stdin, timeout=timeout, output_cap=output_cap)


def _provider(name: str) -> Provider:
    base = (_live_env.live_base_url() or "").rstrip("/")
    key = _live_env.live_api_key() or ""
    if name == "anthropic":
        return provider_for("anthropic", base_url=base, api_key=key,
                            default_max_tokens=_live_env.live_max_tokens())
    if name == "openai_compat":
        return provider_for("openai_compat", base_url=f"{base}/v1", api_key=key)
    return provider_for("openai_responses", base_url=f"{base}/v1/responses", api_key=key,
                        extra_headers={"Authorization": f"Bearer {key}"})


@dataclass
class _Call:
    call_id: str
    tool_name: str
    arguments: dict[str, Any]


@dataclass
class _Result:
    call_id: str
    success: bool
    text: str


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _request(p: Pending) -> Request:
    return Request(kind=p.kind, call_id=p.request_id, tool=p.tool, input=dict(p.input),
                   prompt=p.prompt)


async def _reply(client: Client, p: Pending, reply: Reply) -> None:
    if p.kind == "question":
        data = reply.data
        if isinstance(data, str):  # free text for every question asked
            data = {q["id"]: {"other": data} for q in detail_questions(p.detail)}
        await client.answer(p.task_id, p.request_id, data=data)
    elif reply.decision == "deny":
        await client.answer(p.task_id, p.request_id, "deny",
                            data={"message": reply.message} if reply.message else None)
    elif reply.decision == "allow":
        await client.answer(p.task_id, p.request_id, "allow")
    else:
        raise ValueError(f"an approval of {p.tool} needs decision allow or deny")


class _Person:
    """Plays the person for one trial: answers what the scenario's answerer
    answers, sends the follow-up turns, and says when the trial is over."""

    def __init__(self, client: Client, tid: str, sc: Scenario, ws: Path) -> None:
        self.client, self.tid, self.sc, self.ws = client, tid, sc, ws
        self.asked: list[tuple[Request, Reply | None]] = []
        self._answered: set[str] = set()
        self._turns = list(sc.follow_ups)

    async def _wait_past(self, seq: int) -> None:
        """Until the task has moved on from ``seq`` (the runner took what was
        posted), so the same wait is not read twice."""
        while (await self.client.get(self.tid)).last_seq <= seq:
            await asyncio.sleep(_POLL_S)

    async def _answer(self, found: list[Pending]) -> bool:
        """Answer the new requests; ``False`` when one is left unanswered."""
        new = [p for p in found if p.request_id not in self._answered]
        for p in new:
            req = _request(p)
            reply = self.sc.answer(req) if self.sc.answer is not None else None
            self.asked.append((req, reply))
            self._answered.add(p.request_id)
            if reply is None:
                return False
            await _reply(self.client, p, reply)
        return True

    async def run(self) -> None:
        stuck = 0
        while True:
            row = await self.client.get(self.tid)
            if row.status == "ended":
                return
            if row.status != "waiting":
                await asyncio.sleep(_POLL_S)
                continue
            found = await pending_tree(self.client, self.tid)
            if found:
                stuck = 0
                if not await self._answer(found):
                    return
                await asyncio.sleep(_POLL_S)
                continue
            if row.waiting_on in ("approval", "question"):
                stuck += 1
                if stuck > _STUCK_POLLS:
                    raise RuntimeError(f"task waits on {row.waiting_on} but nothing is pending")
            if row.waiting_on in ("children", "approval", "question"):
                await asyncio.sleep(_POLL_S)
                continue
            if not self._turns:
                return
            change, text = self._turns.pop(0)
            if change is not None:
                change(self.ws)
            await self.client.send(self.tid, text)
            await self._wait_past(row.last_seq)


async def _start(client: Client, agent: str, sc: Scenario, config: dict[str, Any]) -> str:
    """Create the task and send the goal; a mode switch is made first and
    is in effect before the goal is sent, as when a person toggles the mode
    and then types (taken in one batch with the goal, the plan-mode note
    would skip the first request)."""
    goal: str | Message = sc.goal
    if sc.images:
        goal = Message(role="user", origin="human", content=[
            *(Image(media_type="image/png", data=d) for d in sc.images), Text(text=sc.goal)])
    if sc.switch_mode is None:
        return await client.create(agent, goal, config=config)
    tid = await client.create(agent, config=config)
    await client.signal(tid, MODE_SIGNAL, {"mode": sc.switch_mode})

    async def switched() -> None:
        while current_mode(await client.events(tid)) != sc.switch_mode:
            await asyncio.sleep(_POLL_S)

    await asyncio.wait_for(switched(), 60)
    await client.send(tid, goal)
    return tid


async def _drive(cfg: EvalConfig, agents: list[Agent], sc: Scenario, ws: Path, root: Path,
                 ) -> tuple[list[Any], str | None, str, _Person]:
    store = SqliteStore(str(root / "tasks.db"))
    provider = _provider(cfg.provider)
    agent = agents[0]
    rt = Runtime(store=store, agents={a.name: a for a in agents}, provider_for=lambda _m: provider,
                 env_for=lambda _s: _TrialEnv(str(ws), root / "home"),
                 spill_root=str(root / "spill"))
    client = Client(store)
    config = {"cwd": str(ws), "model": cfg.model, "permission_mode": sc.permission_mode}
    error: str | None = None
    # Serve like a worker does (sub-agents run concurrently) until the main
    # task ends, or waits on the person with nothing more to say.
    stop = asyncio.Event()
    serving = asyncio.create_task(rt.serve(stop))
    try:
        tid = await _start(client, agent.name, sc, config)
        person = _Person(client, tid, sc, ws)
        try:
            await asyncio.wait_for(person.run(), sc.timeout_s)
        except TimeoutError:
            error = f"timeout after {sc.timeout_s:.0f}s"
    finally:
        stop.set()
        await serving
        await default_pool().close()
    events = [r.event for r in await client.events(tid)]
    row = await client.get(tid)
    await store.close()
    status = {"waiting": "completed", "ended": row.end_status or "ended"}.get(row.status,
                                                                            row.status)
    return events, error, status, person


def _plugins(cfg: EvalConfig, sc: Scenario, dirs: TrialDirs) -> dict[str, Any]:
    # Plan files go to the trial's own directory: the permissions plugin
    # names it, and the file tools may write there (they otherwise allow
    # only the home's plans directory, which concurrent trials share).
    plans = str(dirs.root / "plans")
    plugins: dict[str, Any] = {"memory": {"directory": str(dirs.memory) + "/"},
                               "permissions": {"plans_dir": plans},
                               "fs": {"write_roots": [plans]}}
    if sc.small_window:
        from noeta.core.models import model_limits
        from noeta.plugins.compaction import Compaction

        _, out = model_limits(cfg.model)
        # The usable window (window - min(out, 20k) - 13k) is the scenario's.
        plugins["compaction"] = Compaction(
            auto_compact_window=sc.usable_window + min(out, 20_000) + 13_000)
    if sc.plugins is not None:
        for name, value in sc.plugins(dirs).items():
            prev = plugins.get(name)
            plugins[name] = {**prev, **value} if isinstance(prev, dict) \
                and isinstance(value, dict) else value
    return plugins


def run_trial(cfg: EvalConfig, sc: Scenario) -> tuple[Trace, dict[str, Any], Path]:
    isolate_home()
    root = Path(tempfile.mkdtemp(prefix="noeta-eval-next-"))
    ws = root / "ws"
    ws.mkdir()
    # Where the memory plugin puts a project's memory under a home: the
    # layout a host that sets ``home`` gets.
    memory = Path(memory_dir_for(str(ws), root / "home"))
    memory.mkdir(parents=True)
    dirs = TrialDirs(root=root, ws=ws, memory=memory)
    sc.setup(ws)
    if sc.memory is not None:
        sc.memory(memory)
    before = snapshot(ws)
    kw: dict[str, Any] = {"one_shot": not sc.follow_ups, "step_budget": _STEP_BUDGET}
    if cfg.system_prompt is not None:
        kw["system_prompt"] = cfg.system_prompt
    agents = default_agents(cfg.model, plugins=_plugins(cfg, sc, dirs), **kw)
    events, error, status, person = _run(_drive(cfg, agents, sc, ws, root))

    batches: list[list[Any]] = []
    batch_turns: list[int] = []
    results: dict[str, Any] = {}
    texts: list[str] = []
    turn = -1
    usage = trial_usage(events)
    for ev in events:
        if isinstance(ev, InputTaken) and any(
                isinstance(i.body, Say) and i.body.message.from_person for i in ev.items):
            turn += 1
        elif isinstance(ev, ModelReplied):
            msg = ev.response.message
            if msg.tool_uses:
                batches.append([_Call(c.id, c.name, dict(c.input)) for c in msg.tool_uses])
                batch_turns.append(max(turn, 0))
            if msg.text.strip():
                texts.append(msg.text)
        elif isinstance(ev, ToolFinished):
            r = ev.result
            results[r.call_id] = _Result(r.call_id, not r.is_error, r.text)
        elif isinstance(ev, TaskEnded) and ev.error and error is None:
            error = f"TaskEnded({ev.status}): {ev.error}"
    trace = Trace(
        workspace=ws, status=status, items=events, batches=batches, results=results,
        final_text=texts[-1] if texts else "",
        spawned=sum(len(e.data.get("children", [])) for e in events
                    if type(e).__name__ == "ExtensionRecorded" and e.kind == "spawned"),
        compactions=sum(type(e).__name__ == "Compacted" for e in events), error=error,
        asked=person.asked, batch_turns=batch_turns, turns=turn + 1, memory_dir=memory,
        before=before,
    )
    return trace, usage, root


def trial_usage(events: list[Any]) -> dict[str, Any]:
    """A trial's model calls and tokens: every recorded reply, and every
    reply the kernel dropped (an empty one, or one cut at the output cap
    while it held only reasoning), whose usage rides on its ``continue`` /
    ``turn_done`` entry. ``empty_nudges`` counts the times the model was
    asked to go on after an empty reply."""
    usage: dict[str, Any] = {"requests": 0, "input_tokens": 0, "cache_read_tokens": 0,
                             "cache_write_tokens": 0, "output_tokens": 0, "cost_usd": 0.0,
                             "empty_nudges": 0}
    for ev in events:
        if isinstance(ev, ModelReplied):
            u = ev.response.usage
        else:
            core = records.decode(ev)
            if not isinstance(core, (records.Continue, records.TurnDone)):
                continue
            if isinstance(core, records.Continue) and core.reason == records.EMPTY_REPLY:
                usage["empty_nudges"] += 1
            if core.usage is None:
                continue
            u = core.usage
        usage["requests"] += 1
        usage["input_tokens"] += u.input
        usage["cache_read_tokens"] += u.cache_read
        usage["cache_write_tokens"] += u.cache_write
        usage["output_tokens"] += u.output
    return usage


class Judge:
    _SYSTEM = (
        "You grade one transcript excerpt against one yes/no question. Reply "
        "with exactly YES or NO on the first line, then one short sentence of "
        "reason. Judge only what the text says, not what it should have said."
    )

    def __init__(self, cfg: EvalConfig) -> None:
        self._cfg = cfg
        self._lock = threading.Lock()
        self.input_tokens = 0
        self.output_tokens = 0
        self.requests = 0

    def ask(self, question: str, text: str) -> bool:
        from noeta.core.types import ModelRequest, SystemPart, user

        goal = f"Question: {question}\n\n<excerpt>\n{text}\n</excerpt>"
        request = ModelRequest(model=self._cfg.model, system=[SystemPart(text=self._SYSTEM)],
                               messages=[user(goal)])
        resp = _run(_provider(self._cfg.provider).complete(request))
        with self._lock:
            self.input_tokens += resp.usage.input
            self.output_tokens += resp.usage.output
            self.requests += 1
        return bool(resp.message.text.strip().upper().startswith("YES"))
