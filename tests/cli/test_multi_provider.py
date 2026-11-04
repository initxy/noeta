"""Several providers through ``from_settings``, as two workers would run
them: the route a task records when its model is chosen is the route every
worker takes, whatever its own listings say, and the limits it records are
known before the first request."""

from __future__ import annotations

import shutil
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest

from noeta.backends.providers import listing
from noeta.backends.providers.listing import ListedModel
from noeta.cli import config, home_dir
from noeta.cli.config import from_settings, load_settings
from noeta.core import registry
from noeta.core.events import ModelCalled
from noeta.core.loop.state import CONFIG_SIGNAL, MODEL_ROUTE
from noeta.testing import ScriptedProvider, reply


def fresh(name: str) -> str:
    return f"{name}-{uuid.uuid4().hex[:8]}"


class Worker:
    """One process's view: its home (config, listing cache), its listings
    and the scripted adapters ``from_settings`` builds there."""

    def __init__(self, home: Path, monkeypatch: pytest.MonkeyPatch,
                 listings: dict[str, Any]) -> None:
        self.home, self.monkeypatch, self.listings = home, monkeypatch, listings
        self.adapters: dict[str, ScriptedProvider] = {}

    def fetch(self, family: str, base_url: str | None, *a: Any, **kw: Any) -> Any:
        found = self.listings[str(base_url)]
        if isinstance(found, Exception):
            raise found
        return found

    def adapter(self, family: str, *, base_url: str | None = None, **kw: Any) -> Any:
        return self.adapters.setdefault(str(base_url), ScriptedProvider([reply("ok")] * 20))

    def app(self) -> Any:
        self.monkeypatch.setattr(home_dir, "HOME", self.home)
        self.monkeypatch.setattr(listing, "fetch_listing", self.fetch)
        self.monkeypatch.setattr(registry, "provider", self.adapter)
        return from_settings(load_settings(), cwd=str(self.home), own_tasks_only=True)

    def models(self, base_url: str) -> list[str]:
        provider = self.adapters.get(base_url)
        return [r.model for r in provider.requests] if provider else []


@pytest.fixture
def homes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    monkeypatch.setattr(config, "managed_dir", lambda: tmp_path / "managed")
    for var in ("NOETA_MODEL", "NOETA_PROVIDER", "NOETA_STORE", "NOETA_BASE_URL"):
        monkeypatch.delenv(var, raising=False)
    store = tmp_path / "shared.db"
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    (a / "config.toml").write_text(
        'model = "claude-opus-5-5"\nprovider = "anthropic"\napi_key = "k"\n'
        'base_url = "https://claude"\n'
        f'store = "sqlite:///{store}"\n'
        '[providers.gw]\nfamily = "openai_compat"\nbase_url = "https://gw/v1"\n'
        'api_key = "g"\n')
    shutil.copytree(a, b)
    return a, b


async def test_a_resumed_task_routes_by_its_record_not_the_listing(
        homes: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    only_gw = fresh("only-gw")
    a = Worker(homes[0], monkeypatch, {"https://claude": [],
                                       "https://gw/v1": [ListedModel(only_gw)]})
    app = a.app()
    async with app:
        tid = await app.client.create("main", "hi", config={"model": only_gw})
        await app.client.wait(tid, timeout=20)
        route = (await app.client.events(tid))[0].event.config[MODEL_ROUTE]  # type: ignore[attr-defined]
    assert only_gw in a.models("https://gw/v1") and not a.models("https://claude")
    assert route == {"model": only_gw, "provider": "gw"}

    # Worker B: its listings cannot be read, its cache is its own.
    down = httpx.ConnectError("down")
    b = Worker(homes[1], monkeypatch, {"https://claude": down, "https://gw/v1": down})
    app = b.app()
    async with app:
        await app.adopt(tid)
        await app.client.send(tid, "again")
        await app.client.wait(tid, timeout=20)
    assert only_gw in b.models("https://gw/v1") and not b.models("https://claude")


async def test_the_recorded_limits_size_the_first_request_on_any_worker(
        homes: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    big = fresh("gw-big")
    b = Worker(homes[1], monkeypatch, {"https://claude": httpx.ConnectError("down"),
                                       "https://gw/v1": httpx.ConnectError("down")})
    app = b.app()
    route = {"model": big, "provider": "gw", "context_window": 300_000,
             "max_output_tokens": 12_345}
    async with app:
        # Recorded where the model was chosen, another machine; unknown here.
        tid = await app.client.create("main", "hi",
                                      config={"model": big, MODEL_ROUTE: route})
        await app.client.wait(tid, timeout=20)
    sent = [r for r in b.adapters["https://gw/v1"].requests if r.model == big]
    assert sent and sent[0].max_tokens == 12_345


async def test_a_switch_records_the_route_and_an_unknown_provider_fails_clearly(
        homes: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    on_gw = fresh("on-gw")
    a = Worker(homes[0], monkeypatch, {"https://claude": [],
                                       "https://gw/v1": [ListedModel(on_gw, 64_000)]})
    app = a.app()
    async with app:
        tid = await app.client.create("main", "hi")
        await app.client.wait(tid, timeout=20)
        await app.client.signal(tid, CONFIG_SIGNAL, {"model": on_gw})
        await app.client.send(tid, "switched")
        await app.client.wait(tid, timeout=20)
        assert a.models("https://claude")[0] == "claude-opus-5-5"
        assert on_gw in a.models("https://gw/v1")
        await app.client.signal(tid, CONFIG_SIGNAL, {
            "model": "x", MODEL_ROUTE: {"model": "x", "provider": "gone"}})
        await app.client.send(tid, "and now")
        await app.client.wait(tid, timeout=20)
        events = [r.event for r in await app.client.events(tid)]
    called = [e.model for e in events if isinstance(e, ModelCalled)]
    assert called[:1] == ["claude-opus-5-5"] and on_gw in called
    assert any("provider 'gone', which is not configured here" in str(e) for e in events)


async def test_starting_reads_no_listing(homes: tuple[Path, Path],
                                         monkeypatch: pytest.MonkeyPatch) -> None:
    a = Worker(homes[0], monkeypatch, {})  # any read would be a KeyError
    app = a.app()
    assert app.runtime.agents
    await app.store.close()
