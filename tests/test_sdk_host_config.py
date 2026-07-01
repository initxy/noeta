"""Stage A acceptance — the noeta.sdk ``HostConfig`` host-level wiring surface (D3).

D3: host-level wiring (durable storage + the preview/MCP runtime injections)
goes through HostConfig, NOT Options — so it never touches the agent identity.
``HostConfig()`` reproduces the historical in-memory, no-preview, no-MCP path.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from noeta.sdk import Client, HostConfig, Options
from noeta.storage.memory import (
    InMemoryContentStore,
    InMemoryDispatcher,
    InMemoryEventLog,
)
from noeta.testing.fake_llm import FakeLLMProvider
from noeta.protocols.messages import LLMResponse, TextBlock, Usage


def _finishing_provider() -> FakeLLMProvider:
    return FakeLLMProvider(
        responses=[
            LLMResponse(
                stop_reason="end_turn",
                content=[TextBlock(text="ok")],
                usage=Usage(uncached=1, output=1),
            )
        ]
    )


def _options() -> Options:
    return Options(
        system_prompt="finish",
        name="main",
        allowed_tools=(),
        permission_mode="bypassPermissions",
    )


def test_empty_host_config_is_inert() -> None:
    hc = HostConfig()
    assert hc.storage_triple() is None
    assert hc.app_gateway is None
    assert hc.mcp_server_resolver is None
    assert hc.workflow_allowed is False


def test_storage_triple_is_all_or_none() -> None:
    disp = InMemoryDispatcher()
    log = InMemoryEventLog(lease_validator=disp)
    # Partial triple is a hard error: the three are constructed together.
    with pytest.raises(ValueError):
        HostConfig(event_log=log, dispatcher=disp).storage_triple()


def test_injected_storage_triple_is_used(tmp_path: Path) -> None:
    # Build an external in-memory triple and drive a turn through it; the events
    # must land in OUR log (proving the Client used the injected storage, not a
    # freshly-built internal one).
    disp = InMemoryDispatcher()
    log = InMemoryEventLog(lease_validator=disp)
    store = InMemoryContentStore()
    client = Client(
        _options(),
        provider=_finishing_provider(),
        workspace_dir=tmp_path,
        host_config=HostConfig(
            event_log=log, content_store=store, dispatcher=disp
        ),
    )
    try:
        outcome = client.start(goal="hi")
        # The injected log, read directly, sees this task's stream.
        injected = list(log.read(outcome.task_id))
        assert injected, "injected event_log holds no events"
        assert injected == client.events(outcome.task_id)
        assert client._host.event_log is log
        assert client._host.content_store is store
    finally:
        client.shutdown()


def test_host_injections_reach_the_host(tmp_path: Path) -> None:
    # app_gateway / mcp_server_resolver / workflow flag are host-level (not
    # Options): they reach the SdkHost verbatim.
    sentinel_gateway = object()

    def resolver(alias: str):  # noqa: ANN202 — test stub
        return None

    client = Client(
        _options(),
        provider=_finishing_provider(),
        workspace_dir=tmp_path,
        host_config=HostConfig(
            app_gateway=sentinel_gateway,  # type: ignore[arg-type]
            mcp_server_resolver=resolver,
            workflow_allowed=True,
        ),
    )
    try:
        assert client._host.app_gateway is sentinel_gateway
        assert client._host.mcp_server_resolver is resolver
        assert client._host.workflow_allowed is True
    finally:
        client.shutdown()


def test_host_config_excluded_from_agent_identity(tmp_path: Path) -> None:
    # Two clients differing only in HostConfig compile the SAME AgentSpec
    # identity (HostConfig is wiring, never identity).
    plain = Client(
        _options(), provider=_finishing_provider(), workspace_dir=tmp_path
    )
    wired = Client(
        _options(),
        provider=_finishing_provider(),
        workspace_dir=tmp_path,
        host_config=HostConfig(workflow_allowed=True),
    )
    try:
        plain_spec = plain.registry.resolve(plain.main_agent_name)
        wired_spec = wired.registry.resolve(wired.main_agent_name)
        assert plain_spec == wired_spec
    finally:
        plain.shutdown()
        wired.shutdown()
