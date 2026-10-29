"""The sign-ins in progress: finished ones are let go once they are no
longer handed out again."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from noeta.plugins.mcp import sign_ins
from noeta.plugins.mcp.config import McpError


class _FakeSignIn:
    closed = 0

    def __init__(self, server: Any, store: Any, *, http: Any = None) -> None:
        self.server = server

    async def start(self) -> str:
        return f"https://auth.example/{self.server.alias}"

    async def finish(self) -> None:
        raise McpError("refused")

    async def close(self) -> None:
        _FakeSignIn.closed += 1


class _Pool:
    http = None

    def key(self, server: Any, scope: str | None) -> tuple[object, ...]:
        return (scope, server.alias)

    async def forget(self, server: Any, scope: str | None) -> None:
        return None


async def test_finished_sign_ins_are_let_go(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sign_ins, "SignIn", _FakeSignIn)
    monkeypatch.setattr(sign_ins, "store_for", lambda server, scope: None)
    now = [1000.0]
    monkeypatch.setattr(sign_ins.time, "monotonic", lambda: now[0])
    pool = _Pool()
    runs = sign_ins.SignIns(lambda: pool)  # type: ignore[arg-type,return-value]
    tool = SimpleNamespace(name="mcp__a__authenticate")
    for n in range(3):
        await runs.start(tool, SimpleNamespace(alias=f"s{n}"), None)  # type: ignore[arg-type]
    await asyncio.sleep(0)  # each sign-in fails
    assert len(runs._runs) == 3
    now[0] += sign_ins.SIGN_IN_REUSE_S + 1
    await runs.start(tool, SimpleNamespace(alias="new"), None)  # type: ignore[arg-type]
    assert list(runs._runs) == [(None, "new")]
    assert _FakeSignIn.closed == 3
