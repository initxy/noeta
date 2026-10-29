"""Run one scenario trial on the default agents and build its ``Trace``.

A trial is set up as a host sets up a task: plugin settings by name (rules,
hooks, MCP servers), the permission mode in the task's config, approvals and
questions answered through ``Client.answer`` from ``noeta.host.pending``, and
later turns sent with ``Client.send``.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from evals import live_env as _live_env
from evals.core import EvalConfig, Reply, Request, Scenario, Trace, TrialDirs
from noeta.agents import default_agents
from noeta.backends.execenvs.local import LocalExecEnv
from noeta.backends.providers import provider_for
from noeta.backends.stores.sqlite import SqliteStore
from noeta.core.agent import Agent
from noeta.core.client import Client
from noeta.core.events import InputTaken, ModelReplied, Say, TaskEnded, ToolFinished
from noeta.core.provider import Provider
from noeta.core.runner import Runtime
from noeta.host.pending import Pending, pending_tree
from noeta.plugins.mcp import default_pool
from noeta.plugins.memory import memory_dir_for
from noeta.toolkit.permission_modes import MODE_SIGNAL

_STEP_BUDGET = 60
#: Usable history window for the compaction scenarios.
_SMALL_USABLE_WINDOW = 24_000
_POLL_S = 0.2


def isolate_home() -> Path:
    """Point ``NOETA_HOME`` at a fresh directory for this process, so plan
    files and anything else noeta keeps in its home never touch the user's."""
    home = Path(tempfile.mkdtemp(prefix="noeta-eval-home-"))
    os.environ["NOETA_HOME"] = str(home)
    return home


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
        await client.answer(p.task_id, p.request_id, data=reply.data)
    elif reply.decision == "deny":
        await client.answer(p.task_id, p.request_id, "deny",
                            data={"message": reply.message} if reply.message else None)
    else:
        await client.answer(p.task_id, p.request_id, "allow")


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
        while True:
            row = await self.client.get(self.tid)
            if row.status == "ended":
                return
            if row.status != "waiting":
                await asyncio.sleep(_POLL_S)
                continue
            found = await pending_tree(self.client, self.tid)
            if found:
                if not await self._answer(found):
                    return
                await asyncio.sleep(_POLL_S)
                continue
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


async def _drive(cfg: EvalConfig, agents: list[Agent], sc: Scenario, ws: Path, root: Path,
                 ) -> tuple[list[Any], str | None, str, _Person]:
    store = SqliteStore(str(root / "tasks.db"))
    provider = _provider(cfg.provider)
    agent = agents[0]
    rt = Runtime(store=store, agents={a.name: a for a in agents}, provider_for=lambda _m: provider,
                 env_for=lambda _s: LocalExecEnv(str(ws)), spill_root=str(root / "spill"))
    client = Client(store)
    config = {"cwd": str(ws), "model": cfg.model, "permission_mode": sc.permission_mode}
    if sc.switch_mode is None:
        tid = await client.create(agent.name, sc.goal, config=config)
    else:
        tid = await client.create(agent.name, config=config)
        await client.signal(tid, MODE_SIGNAL, {"mode": sc.switch_mode})
        await client.send(tid, sc.goal)
    person = _Person(client, tid, sc, ws)
    error: str | None = None
    # Serve like a worker does (sub-agents run concurrently) until the main
    # task ends, or waits on the person with nothing more to say.
    stop = asyncio.Event()
    serving = asyncio.create_task(rt.serve(stop))
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
    plugins: dict[str, Any] = {"memory": {"directory": str(dirs.memory) + "/"}}
    if sc.small_window:
        from noeta.core.models import model_limits
        from noeta.plugins.compaction import Compaction

        _, out = model_limits(cfg.model)
        # The usable window (window - min(out, 20k) - 13k) is _SMALL_USABLE_WINDOW.
        plugins["compaction"] = Compaction(
            auto_compact_window=_SMALL_USABLE_WINDOW + min(out, 20_000) + 13_000)
    if sc.plugins is not None:
        for name, value in sc.plugins(dirs).items():
            prev = plugins.get(name)
            plugins[name] = {**prev, **value} if isinstance(prev, dict) \
                and isinstance(value, dict) else value
    return plugins


def run_trial(cfg: EvalConfig, sc: Scenario) -> tuple[Trace, dict[str, Any], Path]:
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
    usage = {"requests": 0, "input_tokens": 0, "cache_read_tokens": 0,
             "cache_write_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}
    for ev in events:
        if isinstance(ev, InputTaken) and any(
                isinstance(i.body, Say) and i.body.message.from_person for i in ev.items):
            turn += 1
        elif isinstance(ev, ModelReplied):
            msg, u = ev.response.message, ev.response.usage
            if msg.tool_uses:
                batches.append([_Call(c.id, c.name, dict(c.input)) for c in msg.tool_uses])
                batch_turns.append(max(turn, 0))
            if msg.text.strip():
                texts.append(msg.text)
            usage["requests"] += 1
            usage["input_tokens"] += u.input
            usage["cache_read_tokens"] += u.cache_read
            usage["cache_write_tokens"] += u.cache_write
            usage["output_tokens"] += u.output
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
        asked=person.asked, batch_turns=batch_turns, memory_dir=memory,
    )
    return trace, usage, root


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
