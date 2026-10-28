"""The ``max_tokens`` a request sends when its agent sets none: the host's
``Runtime.output_ceiling``, by default the model catalog's."""

from __future__ import annotations

from pathlib import Path

from noeta.backends.stores.sqlite import SqliteStore
from noeta.core.agent import Agent
from noeta.core.client import Client
from noeta.core.runner import Runtime
from noeta.core.runner.runtime import catalog_output_ceiling
from noeta.testing import ScriptedProvider, reply


async def _no_sleep(_: float) -> None:
    return None


async def test_the_host_sets_the_output_ceiling(tmp_path: Path) -> None:
    store = SqliteStore(str(tmp_path / "t.db"))
    try:
        provider = ScriptedProvider([reply("a"), reply("b")])
        agents = {"a": Agent(name="a", model="claude-opus-5-5"),
                  "b": Agent(name="b", model="claude-opus-5-5", max_tokens=99)}
        asked: list[str] = []

        def ceiling(model: str) -> int:
            asked.append(model)
            return 1234

        rt = Runtime(store=store, agents=agents, provider_for=lambda _m: provider,
                     env_for=lambda _s: None, output_ceiling=ceiling,  # type: ignore[arg-type,return-value]
                     sleep=_no_sleep)
        client = Client(store)
        await client.create("a", "go")
        await client.create("b", "go")
        await rt.run_until_idle()
        assert sorted(r.max_tokens or 0 for r in provider.requests) == [99, 1234]
        assert asked == ["claude-opus-5-5"]
        assert catalog_output_ceiling("claude-opus-5-5") > 16_384
    finally:
        await store.close()
