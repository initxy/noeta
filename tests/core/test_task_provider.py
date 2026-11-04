"""``Runtime.task_provider`` is asked, with the task's folded state, before
a request is sized: a host that registers a model's limits there (a route
the task recorded) has them read by that very request."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from noeta.backends.stores.sqlite import SqliteStore
from noeta.core import models
from noeta.core.agent import Agent
from noeta.host.local import LocalApp
from noeta.testing import ScriptedProvider, reply


async def test_limits_registered_by_task_provider_size_the_same_request(
        tmp_path: Path) -> None:
    model = f"late-{uuid.uuid4().hex[:8]}"
    provider = ScriptedProvider([reply("ok")])
    asked: list[str] = []

    def task_provider(state: Any, wanted: str) -> Any:
        asked.append(wanted)
        if models.find_spec(wanted) is None:
            models.register_models({wanted: models.ModelSpec(
                real_model_id=wanted, context_window=50_000, max_output_tokens=4_321)})
        return provider

    app = LocalApp(SqliteStore(str(tmp_path / "t.db")), [Agent(name="main", model=model)],
                   provider, env_for=lambda _s: None, task_provider=task_provider)
    async with app:
        tid = await app.client.create("main", "hi")
        await app.client.wait(tid, timeout=20)
    assert asked and provider.requests[0].max_tokens == 4_321
