"""What a host allows a tenant, enforced by the workers: at most
``max_running`` of its tasks run at once (the rest wait, other tenants go
on), a model call past its monthly budget fails the turn with
``budget_spent``, and its tasks call its own provider."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from noeta.backends.stores.sqlite import SqliteStore
from noeta.core import records
from noeta.core.agent import Agent
from noeta.core.client import Client
from noeta.core.errors import Conflict
from noeta.core.events import ExtensionRecorded, ModelCalled, ModelReplied
from noeta.core.runner import Runtime
from noeta.core.tenants import Tenant, period
from noeta.core.types import Message, ModelRequest, ModelResponse, Text, Usage
from noeta.testing import ScriptedProvider, reply


class Gate:
    """Answers once released; counts how many calls are open at once."""

    family = "scripted"

    def __init__(self) -> None:
        self.open: dict[str, int] = {}
        self.most: dict[str, int] = {}
        self.go = asyncio.Event()

    async def complete(self, request: ModelRequest, on_delta: Any = None) -> ModelResponse:
        who = request.messages[0].text
        self.open[who] = self.open.get(who, 0) + 1
        self.most[who] = max(self.most.get(who, 0), self.open[who])
        await self.go.wait()
        self.open[who] -= 1
        return ModelResponse(message=Message(role="assistant", content=[Text(text="done")]),
                             stop_reason="end_turn", usage=Usage(output=1))


async def test_a_tenant_runs_no_more_than_its_limit(tmp_path: Path) -> None:
    store = SqliteStore(str(tmp_path / "t.db"))
    gate = Gate()
    rt = Runtime(store=store, agents={"a": Agent(name="a", model="m")},
                 provider_for=lambda _m: gate, env_for=lambda _s: None,  # type: ignore[arg-type,return-value]
                 poll_interval=0.02, tenants={"acme": Tenant(max_running=2)})
    host = Client(store)
    acme = [await host.scoped("acme").create("a", "acme") for _ in range(5)]
    beta = [await host.scoped("beta").create("a", "beta") for _ in range(3)]
    stop = asyncio.Event()
    serving = asyncio.create_task(rt.serve(stop))
    for _ in range(200):
        if gate.open.get("acme") == 2 and gate.open.get("beta") == 3:
            break
        await asyncio.sleep(0.01)
    assert gate.open == {"acme": 2, "beta": 3}  # beta is not held up behind acme
    assert len(await store.tasks(tenant="acme", status="ready")) == 3
    gate.go.set()
    for tid in acme + beta:
        await host.wait(tid, timeout=10)
    assert gate.most == {"acme": 2, "beta": 3}
    stop.set()
    await serving
    await store.close()


async def _no_sleep(_: float) -> None:
    return None


async def test_a_spent_budget_fails_the_turn_with_a_clear_reason(tmp_path: Path) -> None:
    store = SqliteStore(str(tmp_path / "t.db"))
    provider = ScriptedProvider([reply("one", usage=Usage(uncached=10, output=5)),
                                 reply("two", usage=Usage(uncached=10, output=5)),
                                 reply("never")])
    rt = Runtime(store=store, agents={"a": Agent(name="a", model="m")},
                 provider_for=lambda _m: provider, env_for=lambda _s: None,  # type: ignore[arg-type,return-value]
                 sleep=_no_sleep, tenants={"acme": Tenant(budget_tokens=20)})
    client = Client(store).scoped("acme")
    tid = await client.create("a", "first")
    await rt.run_until_idle()
    await client.send(tid, "second")
    await rt.run_until_idle()
    assert await store.tenant_usage("acme", period()) == (30, 0.0)
    await client.send(tid, "third")
    await rt.run_until_idle()
    log = [r.event for r in await store.read(tid)]
    assert sum(isinstance(e, ModelCalled) for e in log) == 2
    [failed] = [records.decode(e) for e in log if isinstance(e, ExtensionRecorded)
                and isinstance(records.decode(e), records.TurnFailed)]
    assert isinstance(failed, records.TurnFailed) and failed.reason == "budget_spent"
    assert "tenant 'acme' has used its token budget" in failed.detail
    assert "30 of 20 tokens" in failed.detail
    # Another tenant (no budget) is not touched, and nothing counts for it.
    other = await Client(store).create("a", "hello")
    provider.add(reply("fine"))
    await rt.run_until_idle()
    assert (await store.get(other)).status == "waiting"  # type: ignore[union-attr]
    assert await store.tenant_usage("default", period()) == (0, 0.0)
    await store.close()


async def test_a_spent_budget_refuses_compact(tmp_path: Path) -> None:
    """No model call past the budget, not ``/compact``'s summary either."""
    from noeta.plugins.compaction import Compaction
    from noeta.plugins.compaction.extension import command_text

    store = SqliteStore(str(tmp_path / "t.db"))
    provider = ScriptedProvider([reply("one", usage=Usage(uncached=30, output=5)),
                                 reply("<summary>s</summary>")])
    rt = Runtime(store=store, agents={"a": Agent(name="a", model="m",
                                                 extensions=[Compaction()])},
                 provider_for=lambda _m: provider, env_for=lambda _s: None,  # type: ignore[arg-type,return-value]
                 sleep=_no_sleep, tenants={"acme": Tenant(budget_tokens=20)})
    client = Client(store).scoped("acme")
    tid = await client.create("a", "first")
    await rt.run_until_idle()
    await client.send(tid, command_text(""))
    await rt.run_until_idle()
    assert len(provider.requests) == 1
    log = [records.decode(e) for e in (r.event for r in await store.read(tid))
           if isinstance(e, ExtensionRecorded)]
    assert any(isinstance(e, records.TurnFailed) and e.reason == "budget_spent" for e in log)
    await store.close()


async def test_a_spent_budget_refuses_an_extensions_model_call(tmp_path: Path) -> None:
    from noeta.core.errors import BudgetSpent
    from noeta.core.extension import Extension, TaskView

    refused: list[str] = []

    class Judge(Extension):
        name = "judge"

        async def on_start(self, view: TaskView) -> None:
            assert view.complete is not None
            try:
                await view.complete(ModelRequest(model="m", messages=[]))
            except BudgetSpent as exc:
                refused.append(str(exc))

    store = SqliteStore(str(tmp_path / "t.db"))
    await store.add_tenant_usage("acme", period(), 30, 0.0)
    provider = ScriptedProvider([reply("never")])
    rt = Runtime(store=store, agents={"a": Agent(name="a", model="m", extensions=[Judge()])},
                 provider_for=lambda _m: provider, env_for=lambda _s: None,  # type: ignore[arg-type,return-value]
                 sleep=_no_sleep, tenants={"acme": Tenant(budget_tokens=20)})
    await Client(store).scoped("acme").create("a", "first")
    await rt.run_until_idle()
    assert provider.requests == []
    assert refused and "30 of 20 tokens" in refused[0]
    await store.close()


class LosesTheReply(SqliteStore):
    """The append of a model's reply finds the claim lost."""

    async def append(self, claim: Any, events: Any, **kw: Any) -> Any:
        if any(isinstance(e, ModelReplied) for e in events):
            raise Conflict("the claim was lost")
        return await super().append(claim, events, **kw)


async def test_a_reply_whose_claim_was_lost_is_still_charged(tmp_path: Path) -> None:
    """The call was paid for whether or not its reply is kept."""
    store = LosesTheReply(str(tmp_path / "t.db"))
    provider = ScriptedProvider([reply("one", usage=Usage(uncached=10, output=5))])
    rt = Runtime(store=store, agents={"a": Agent(name="a", model="m")},
                 provider_for=lambda _m: provider, env_for=lambda _s: None,  # type: ignore[arg-type,return-value]
                 sleep=_no_sleep, tenants={"acme": Tenant(budget_tokens=100)})
    await Client(store).scoped("acme").create("a", "first")
    await rt.run_once()
    assert await store.tenant_usage("acme", period()) == (15, 0.0)
    await store.close()


async def test_a_tenants_tasks_call_its_own_provider(tmp_path: Path) -> None:
    store = SqliteStore(str(tmp_path / "t.db"))
    host_provider, own = ScriptedProvider([reply("host")]), ScriptedProvider([reply("own")])
    rt = Runtime(store=store, agents={"a": Agent(name="a", model="m")},
                 provider_for=lambda _m: host_provider, env_for=lambda _s: None,  # type: ignore[arg-type,return-value]
                 sleep=_no_sleep, tenants={"acme": Tenant(provider_for=lambda _m: own)})
    await Client(store).scoped("acme").create("a", "hi")
    await Client(store).create("a", "hi")
    await rt.run_until_idle()
    assert len(own.requests) == 1 and len(host_provider.requests) == 1
    await store.close()


def test_tenant_directories_are_read_with_their_secrets(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from noeta.cli import config, home_dir, tenants

    monkeypatch.setattr(home_dir, "HOME", tmp_path)
    work = tmp_path / "acme-work"
    work.mkdir()
    (tmp_path / "tenants" / "acme").mkdir(parents=True)
    (tmp_path / "tenants" / "acme" / "config.toml").write_text(
        f'workspace = "{work}"\nqueues = ["acme", "gpu"]\nmax_running = 3\nbudget_usd = 5\n'
        'provider = "openai_compat"\nbase_url = "http://llm.example/v1"\n'
        'api_key = "{env:ACME_KEY}"\n')
    (tmp_path / "tenants" / "empty").mkdir()  # no config.toml: not a tenant
    monkeypatch.setenv("ACME_KEY", "sk-acme")
    found = tenants.load_tenants(config.Settings(model="m", api_key="server-key"))
    assert list(found) == ["acme"]
    acme = found["acme"]
    assert acme.workspace == str(work.resolve()) and acme.queues == ("acme", "gpu")
    assert acme.allowed.max_running == 3 and acme.allowed.budget_usd == 5.0
    assert acme.allowed.provider_for is not None
    made: Any = acme.allowed.provider_for("m")
    assert made._client.headers["authorization"] == "Bearer sk-acme"  # never the server's
    assert str(made._client.base_url).rstrip("/") == "http://llm.example/v1"
    (tmp_path / "tenants" / "acme" / "config.toml").write_text("queues = []\n")
    with pytest.raises(ValueError, match="queues"):
        tenants.load_tenants(config.Settings(model="m"))
    (tmp_path / "tenants" / "acme" / "config.toml").write_text("max_runing = 3\n")
    with pytest.raises(ValueError, match="unknown setting"):
        tenants.load_tenants(config.Settings(model="m"))


def test_a_tenants_own_endpoint_never_borrows_the_servers_key_or_headers(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from noeta.cli import config, home_dir, tenants

    monkeypatch.setattr(home_dir, "HOME", tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "server-env-key")
    monkeypatch.setenv("OPENAI_API_KEY", "server-env-key")
    work = tmp_path / "acme-work"
    work.mkdir()
    folder = tmp_path / "tenants" / "acme"
    folder.mkdir(parents=True)
    server = config.Settings(model="m", api_key="server-key",
                             extra_headers={"x-gateway-auth": "server-secret"})
    for own in ('base_url = "https://llm.acme.example"\n',
                'provider = "openai_compat"\nbase_url = "http://llm.acme.example/v1"\n',
                'base_url = "https://llm.acme.example"\napi_key = ""\n'):
        (folder / "config.toml").write_text(f'workspace = "{work}"\n{own}')
        with pytest.raises(ValueError, match="api_key"):
            tenants.load_tenants(server)
    (folder / "config.toml").write_text(
        f'workspace = "{work}"\nbase_url = "https://llm.acme.example"\napi_key = "sk-acme"\n'
        '[extra_headers]\nx-acme = "mine"\n')
    found = tenants.load_tenants(server)["acme"].allowed.provider_for
    assert found is not None
    made: Any = found("m")
    assert made._client.headers["x-api-key"] == "sk-acme"
    assert made._client.headers["x-acme"] == "mine"
    assert "x-gateway-auth" not in made._client.headers
    # Its own key on the server's endpoint: still none of the server's headers.
    (folder / "config.toml").write_text(f'workspace = "{work}"\napi_key = "sk-acme"\n')
    found = tenants.load_tenants(server)["acme"].allowed.provider_for
    assert found is not None
    made = found("m")
    assert made._client.headers["x-api-key"] == "sk-acme"
    assert "x-gateway-auth" not in made._client.headers
