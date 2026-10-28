"""Permissions with a sandboxed shell: a confined command needs no approval
(``autoAllowBashIfSandboxed``), leaving the sandbox
(``dangerouslyDisableSandbox``) is always asked about."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from noeta.backends.execenvs.local import LocalExecEnv
from noeta.backends.stores.sqlite import SqliteStore
from noeta.core.client import Client
from noeta.core.runner import Runtime
from noeta.plugins.permissions import Permissions
from noeta.testing import ScriptedProvider, reply
from noeta.toolkit.sandbox import SandboxSettings
from tests.plugins.permissions.helpers import Fake, agent, events


@dataclass
class FakeSandbox:
    settings: SandboxSettings
    reason: str | None = None

    def unavailable(self) -> str | None:
        return self.reason

    async def prepare(self, argv: Sequence[str], *, cwd: str,
                      writable: Sequence[str] = ()) -> Any:
        raise AssertionError("the fake Bash does not run commands")


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SqliteStore]:
    s = SqliteStore(str(tmp_path / "t.db"))
    yield s
    await s.close()


async def _no_sleep(_: float) -> None:
    return None


async def run(store: SqliteStore, tmp_path: Path, perms: Permissions,
              calls: list[dict[str, Any]], **settings: Any) -> tuple[Fake, list[Any]]:
    bash = Fake("Bash")
    env = LocalExecEnv(str(tmp_path))
    env.sandbox = FakeSandbox(SandboxSettings(enabled=True, **settings))  # type: ignore[assignment]
    provider = ScriptedProvider([reply("", *[("Bash", c) for c in calls]), reply("ok")])
    ag = agent([perms], [bash])
    rt = Runtime(store=store, agents={ag.name: ag}, provider_for=lambda _m: provider,
                 env_for=lambda _s: env, sleep=_no_sleep)
    client = Client(store)
    tid = await client.create("a", "go", config={"cwd": str(tmp_path)})
    await rt.run_until_idle()
    return bash, await events(client, tid)


async def test_sandboxed_commands_need_no_approval(store: SqliteStore, tmp_path: Path) -> None:
    bash, _ = await run(store, tmp_path, Permissions(mode="default"),
                        [{"command": "npm install"}, {"command": "rm -rf build"}])
    assert len(bash.runs) == 2


async def test_auto_allow_can_be_switched_off(store: SqliteStore, tmp_path: Path) -> None:
    bash, _ = await run(store, tmp_path, Permissions(mode="default"),
                        [{"command": "npm install"}], auto_allow_bash=False)
    assert bash.runs == []


async def test_leaving_the_sandbox_is_asked(store: SqliteStore, tmp_path: Path) -> None:
    bash, evs = await run(store, tmp_path, Permissions(mode="default", allow=["Bash"]),
                          [{"command": "ls", "dangerouslyDisableSandbox": True}])
    assert bash.runs == []
    assert any("outside the sandbox" in str(getattr(e, "detail", "")) for e in evs)


async def test_excluded_and_broken_sandboxes_fall_back_to_the_rules(
        store: SqliteStore, tmp_path: Path) -> None:
    bash, _ = await run(store, tmp_path, Permissions(mode="default"),
                        [{"command": "docker ps"}], excluded_commands=("docker *",))
    assert bash.runs == []
    bash, _ = await run(store, tmp_path, Permissions(mode="default", allow=["Bash(docker *)"]),
                        [{"command": "docker ps", "dangerouslyDisableSandbox": True}],
                        excluded_commands=("docker *",))
    assert len(bash.runs) == 1


async def test_leaving_is_allowed_in_bypass_mode(store: SqliteStore, tmp_path: Path) -> None:
    bash, _ = await run(store, tmp_path, Permissions(mode="bypassPermissions"),
                        [{"command": "ls", "dangerouslyDisableSandbox": True}])
    assert len(bash.runs) == 1
