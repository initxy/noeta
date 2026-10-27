"""A worker process for the kill -9 test: serves tasks from a sqlite file
(``worker.py PATH JOURNAL``) or a Postgres schema (``worker.py DSN JOURNAL
SCHEMA``) until killed. Every tool run is appended to a journal file first, so the test
can count runs across processes that died mid-call."""

from __future__ import annotations

import asyncio
import os
import random
import sys
from typing import Any

from noeta.agent import Agent
from noeta.runner import Runtime
from noeta.store.sqlite import SqliteStore
from noeta.tools.base import ToolContext, ToolOutput, text_output
from noeta.types import Message, ModelRequest, ModelResponse, Text, ToolResult, ToolUse, Usage

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
        from noeta.store.postgres import PostgresStore

        store = PostgresStore(db, schema=schema)
    agent = Agent(name="a", model="m", tools=[Effect(journal)])
    rt = Runtime(store=store, agents={"a": agent}, provider_for=lambda _m: ByProgress(),
                 env_for=lambda _s: None, lease_seconds=0.5, poll_interval=0.05)  # type: ignore[arg-type,return-value]
    await rt.serve(asyncio.Event())


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None))
