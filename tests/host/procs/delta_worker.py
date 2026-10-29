"""A worker process for the live-output test (``delta_worker.py DSN SCHEMA``):
serves the ``main`` agent from a Postgres schema with a provider that
streams its reply in pieces, until killed."""

from __future__ import annotations

import asyncio
import sys
from typing import Any

from noeta.backends.stores.postgres import PostgresStore
from noeta.core.agent import Agent
from noeta.core.types import Delta, Message, ModelRequest, ModelResponse, Text, Usage
from noeta.host.local import LocalApp

PIECES = ["hello ", "from ", "another ", "machine"]


class Streaming:
    family = "scripted"

    async def complete(self, request: ModelRequest, on_delta: Any | None = None) -> ModelResponse:
        await asyncio.sleep(1.0)  # the watcher is listening by then
        for piece in PIECES:
            if on_delta is not None:
                on_delta(Delta(kind="text", text=piece))
            await asyncio.sleep(0.05)
        return ModelResponse(message=Message(role="assistant", content=[Text(text="".join(PIECES))]),
                             stop_reason="end_turn", usage=Usage(output=4))


async def main(dsn: str, schema: str) -> None:
    app = LocalApp(PostgresStore(dsn, schema=schema), [Agent(name="main", model="m")],
                   Streaming(), env_for=lambda _s: None)  # type: ignore[arg-type,return-value]
    await app.start()
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], sys.argv[2]))
