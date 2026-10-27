"""Run one scenario trial on the default agents and build its ``Trace``."""

from __future__ import annotations

import asyncio
import tempfile
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from evals import live_env as _live_env
from evals.core import TRIAL_TIMEOUT_S, EvalConfig, Trace
from noeta.agent import Agent
from noeta.client import Client
from noeta.events import ModelReplied, TaskEnded, ToolFinished
from noeta.execenv.local import LocalExecEnv
from noeta.presets import default_agents
from noeta.providers import provider_for
from noeta.providers.base import Provider
from noeta.runner import Runtime
from noeta.store.sqlite import SqliteStore

_MAX_STEPS_PER_TURN = 60
#: Usable history window for the compaction scenario.
_SMALL_USABLE_WINDOW = 24_000


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


PROVIDERS: dict[str, Callable[[], Any]] = {
    n: (lambda n=n: _provider(n)) for n in ("anthropic", "openai_compat", "responses")
}


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


async def _drive(cfg: EvalConfig, agents: list[Agent], goal: str, ws: Path, root: Path,
                 timeout_s: float) -> tuple[list[Any], str | None, str]:
    store = SqliteStore(str(root / "tasks.db"))
    provider = _provider(cfg.provider)
    agent = agents[0]
    rt = Runtime(store=store, agents={a.name: a for a in agents}, provider_for=lambda _m: provider,
                 env_for=lambda _s: LocalExecEnv(str(ws)), spill_root=str(root / "spill"))
    client = Client(store)
    tid = await client.create(agent.name, goal, config={
        "cwd": str(ws), "model": cfg.model, "permission_mode": "bypassPermissions"})
    error: str | None = None
    # Serve like a worker does (sub-agents run concurrently) until the main
    # task waits or ends.
    stop = asyncio.Event()
    serving = asyncio.create_task(rt.serve(stop))
    try:
        await asyncio.wait_for(_settled(client, tid), timeout_s)
    except TimeoutError:
        error = f"timeout after {timeout_s:.0f}s"
    finally:
        stop.set()
        await serving
    events = [r.event for r in await client.events(tid)]
    row = await client.get(tid)
    await store.close()
    status = {"waiting": "completed", "ended": row.end_status or "ended"}.get(row.status,
                                                                            row.status)
    return events, error, status


async def _settled(client: Client, tid: str) -> None:
    """Until the main task ends or waits on a person (not on its children)."""
    while True:
        row = await client.get(tid)
        if row.status == "ended" or (row.status == "waiting" and row.waiting_on != "children"):
            return
        await asyncio.sleep(0.2)


def run_trial(
    cfg: EvalConfig,
    goal: str,
    setup: Callable[[Path], None],
    *,
    small_window: bool = False,
    timeout_s: float = TRIAL_TIMEOUT_S,
) -> tuple[Trace, dict[str, Any], Path]:
    root = Path(tempfile.mkdtemp(prefix="noeta-eval-next-"))
    ws = root / "ws"
    ws.mkdir()
    setup(ws)
    kw: dict[str, Any] = {"one_shot": True, "max_steps_per_turn": _MAX_STEPS_PER_TURN}
    if cfg.system_prompt is not None:
        kw["system_prompt"] = cfg.system_prompt
    if small_window:
        from noeta.ext.compaction import Compaction
        from noeta.models import model_limits

        _, out = model_limits(cfg.model)
        # The usable window (window - min(out, 20k) - 13k) is _SMALL_USABLE_WINDOW.
        kw["compaction"] = Compaction(
            auto_compact_window=_SMALL_USABLE_WINDOW + min(out, 20_000) + 13_000)
    agents = default_agents(cfg.model, memory_root=str(root / "memory"), **kw)
    events, error, status = _run(_drive(cfg, agents, goal, ws, root, timeout_s))

    batches: list[list[Any]] = []
    results: dict[str, Any] = {}
    texts: list[str] = []
    usage = {"requests": 0, "input_tokens": 0, "cache_read_tokens": 0,
             "cache_write_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}
    for ev in events:
        if isinstance(ev, ModelReplied):
            msg, u = ev.response.message, ev.response.usage
            if msg.tool_uses:
                batches.append([_Call(c.id, c.name, dict(c.input)) for c in msg.tool_uses])
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
        from noeta.types import ModelRequest, SystemPart, user

        goal = f"Question: {question}\n\n<excerpt>\n{text}\n</excerpt>"
        request = ModelRequest(model=self._cfg.model, system=[SystemPart(text=self._SYSTEM)],
                               messages=[user(goal)])
        resp = _run(_provider(self._cfg.provider).complete(request))
        with self._lock:
            self.input_tokens += resp.usage.input
            self.output_tokens += resp.usage.output
            self.requests += 1
        return resp.message.text.strip().upper().startswith("YES")
