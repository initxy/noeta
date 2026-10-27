"""A model that answers summarize calls with a note and main calls from a
script, a tool with a sized output, and a check that every recorded model
call is rebuilt byte for byte from the log (what a restarted runner sends)."""

from __future__ import annotations

import itertools
from collections.abc import Callable, Sequence
from typing import Any

from noeta.core.agent import Agent
from noeta.core.client import Client
from noeta.core.events import ModelCalled
from noeta.core.extension import TaskView
from noeta.core.loop.request import build_request, request_hash
from noeta.core.loop.state import fold
from noeta.core.provider import OnDelta
from noeta.core.runner import Runtime
from noeta.core.runner.model import _output_ceiling
from noeta.core.store.sqlite import SqliteStore
from noeta.core.tool import Tool, ToolContext, ToolOutput, text_output
from noeta.core.types import Message, ModelRequest, ModelResponse, Text, ToolUse, Usage
from noeta.plugins.compaction.estimate import messages_tokens, prefix_tokens
from noeta.plugins.compaction.summary import SUMMARIZE_PROMPT

NOTE = (
    "## 1. Primary Request & Intent\nProbe things.\n\n"
    "## 5. All user messages\n- go\n\n"
    "## 8. Current Work\nProbing."
)

_ids = itertools.count(1)


def real_tokens(request: ModelRequest) -> int:
    """What a provider would report: the estimate itself (density 1)."""
    return prefix_tokens(request.system, request.tools) + messages_tokens(request.messages)


def call(name: str = "Probe", **input: Any) -> Callable[[ModelRequest], ModelResponse]:
    def step(request: ModelRequest) -> ModelResponse:
        use = ToolUse(id=f"c{next(_ids)}", name=name, input=dict(input))
        return ModelResponse(message=Message(role="assistant", content=[use]),
                             stop_reason="tool_use",
                             usage=Usage(uncached=real_tokens(request), output=5))
    return step


def answer(text: str) -> Callable[[ModelRequest], ModelResponse]:
    def step(request: ModelRequest) -> ModelResponse:
        return ModelResponse(message=Message(role="assistant", content=[Text(text=text)]),
                             stop_reason="end_turn",
                             usage=Usage(uncached=real_tokens(request), output=5))
    return step


def note_reply(text: str = NOTE) -> ModelResponse:
    return ModelResponse(message=Message(role="assistant", content=[Text(text=text)]),
                         stop_reason="end_turn", usage=Usage(uncached=100, output=50))


Step = ModelResponse | Exception | Callable[[ModelRequest], ModelResponse]


def is_summarize(request: ModelRequest) -> bool:
    return bool(request.messages) and request.messages[-1].text == SUMMARIZE_PROMPT


class Model:
    """Main calls take the next scripted step; summarize calls take the next
    ``summaries`` step, or a plain note."""

    family = "scripted"

    def __init__(self, main: Sequence[Step] = (), summaries: Sequence[Step] = ()) -> None:
        self.main = list(main)
        self.summary_steps = list(summaries)
        self.requests: list[ModelRequest] = []
        self.summarize_requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest,
                       on_delta: OnDelta | None = None) -> ModelResponse:
        if is_summarize(request):
            self.summarize_requests.append(request)
            step: Step = self.summary_steps.pop(0) if self.summary_steps else note_reply()
        else:
            self.requests.append(request)
            if not self.main:
                raise AssertionError("script ran out")
            step = self.main.pop(0)
        if isinstance(step, Exception):
            raise step
        return step(request) if callable(step) else step


class Probe:
    name = "Probe"
    description = "Look."
    input_schema = {"type": "object"}
    read_only = True
    clearable = True

    def __init__(self, size: int = 2_000) -> None:
        self.size = size

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        return text_output(f"{ctx.call_id}:" + "x" * self.size)


async def _no_sleep(_: float) -> None:
    return None


def runtime(store: SqliteStore, agent: Agent, model: Model,
            env_for: Callable[[Any], Any] | None = None) -> Runtime:
    return Runtime(store=store, agents={agent.name: agent}, provider_for=lambda _m: model,  # type: ignore[arg-type,return-value]
                   env_for=env_for or (lambda _s: None), sleep=_no_sleep)


async def run(store: SqliteStore, agent: Agent, model: Model, goal: str = "go",
              env_for: Callable[[Any], Any] | None = None) -> tuple[Client, str]:
    client = Client(store)
    tid = await client.create(agent.name, goal)
    await runtime(store, agent, model, env_for).run_until_idle()
    return client, tid


async def _no_compaction(_request: ModelRequest) -> ModelResponse:
    raise AssertionError("a restarted runner would compact again")


async def rebuilt(store: SqliteStore, agent: Agent, tid: str) -> list[tuple[str, str]]:
    """For every ``ModelCalled``: (hash recorded live, hash of the request a
    runner rebuilds from the log up to that point)."""
    recs = await store.read(tid)
    exts = agent.all_extensions()
    reducers = {e.name: e.reduce for e in exts}
    initial = {e.name: e.initial_state() for e in exts}
    out: list[tuple[str, str]] = []
    for k, rec in enumerate(recs):
        if not isinstance(rec.event, ModelCalled):
            continue
        state = fold(recs[:k], reducers, initial)
        view = TaskView(state=state, complete=_no_compaction, env=None)
        tools: list[Tool] = []
        for e in exts:
            for t in e.tools(view):
                if t.name not in {x.name for x in tools}:
                    tools.append(t)
        request = build_request(
            state, model=agent.model, system_prompt=agent.system_prompt,
            parts=[p for e in exts for p in e.context(view)], tools=tools,
            effort=agent.effort, max_tokens=agent.max_tokens or _output_ceiling(agent.model),
            output_schema=agent.output_schema)
        for e in exts:
            request = await e.before_model(view, request)
        assert view.pending == [], view.pending
        out.append((rec.event.request_hash, request_hash(request)))
    return out
