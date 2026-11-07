"""A worker process for the kill -9 test: serves tasks from a sqlite file
(``worker.py PATH JOURNAL``) or a Postgres schema (``worker.py DSN JOURNAL
SCHEMA``) until killed. Every tool run is appended to a journal file first, so the test
can count runs across processes that died mid-call. With ``NOETA_KILL9_STALL``
naming a file, the first tool run to remove it writes its pid to
``<file>.pid`` and stalls there until killed: a kill that surely lands while
a call is in flight."""

from __future__ import annotations

import asyncio
import os
import random
import sys
from typing import Any

from noeta.backends.stores.sqlite import SqliteStore
from noeta.core.agent import Agent
from noeta.core.runner import Runtime
from noeta.core.tool import ToolContext, ToolOutput, text_output
from noeta.core.types import Message, ModelRequest, ModelResponse, Text, ToolResult, ToolUse, Usage

STEPS = 3


class Effect:
    name = "Effect"
    description = "Do a side effect."
    input_schema = {"type": "object"}
    read_only = False
    clearable = False

    def __init__(self, journal: str) -> None:
        self.journal = journal

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        line = f"{ctx.task_id} {input['key']}\n"
        fd = os.open(self.journal, os.O_WRONLY | os.O_APPEND | os.O_CREAT)
        try:
            os.write(fd, line.encode())
            os.fsync(fd)
        finally:
            os.close(fd)
        stall = os.environ.get("NOETA_KILL9_STALL")
        if stall:
            try:
                os.remove(stall)
            except FileNotFoundError:
                pass
            else:
                with open(stall + ".tmp", "w") as f:
                    f.write(str(os.getpid()))
                os.rename(stall + ".tmp", stall + ".pid")
                await asyncio.Event().wait()
        await asyncio.sleep(random.uniform(0.01, 0.1))
        return text_output(f"did {input['key']}")


class ByProgress:
    family = "scripted"

    async def complete(self, request: ModelRequest, on_delta: Any | None = None) -> ModelResponse:
        await asyncio.sleep(random.uniform(0, 0.01))
        done = sum(1 for m in request.messages for b in m.content if isinstance(b, ToolResult))
        if done < STEPS:
            key = str(done)
            msg = Message(role="assistant", content=[ToolUse(id=f"c{key}", name="Effect",
                                                             input={"key": key})])
            return ModelResponse(message=msg, stop_reason="tool_use", usage=Usage(output=1))
        return ModelResponse(message=Message(role="assistant", content=[Text(text="finished")]),
                             stop_reason="end_turn", usage=Usage(output=1))


async def main(db: str, journal: str, schema: str | None) -> None:
    store: Any
    if schema is None:
        store = SqliteStore(db)
    else:
        from noeta.backends.stores.postgres import PostgresStore

        store = PostgresStore(db, schema=schema)
    agent = Agent(name="a", model="m", tools=[Effect(journal)])
    # SIGKILLs every 0.05-0.25 s can hit one task four times with no step
    # between, which the restart limit would end (tests/core/test_restart_limit.py
    # covers it); this test is about recovery, so no limit.
    rt = Runtime(store=store, agents={"a": agent}, provider_for=lambda _m: ByProgress(),
                 env_for=lambda _s: None, lease_seconds=0.5, poll_interval=0.05,
                 restart_limit=0)  # type: ignore[arg-type,return-value]
    await rt.serve(asyncio.Event())


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None))
