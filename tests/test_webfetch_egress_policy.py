"""``WebFetch`` egress policy: which hosts a fetch may reach without asking.

``WebFetch`` is ``risk_level="low"``, so the static approval set never gates
it, and the URL is entirely the model's. The fence is therefore a per-call one,
the same shape ``Bash`` uses for a command outside its allowlist: a host in
``HostConfig.webfetch_allowed_hosts`` runs silently, every other host routes
through human approval under a gating permission mode.

It gates by **host**, never by address. An agent holding ``Bash`` reaches any
loopback or intranet target with one ``curl``, so refusing addresses here would
protect nothing; a host that needs an egress boundary enforces it at the
network or the sandbox. The one thing refused outright is a scheme
``WebFetch`` does not fetch — ``file:`` is refused because fetching one is not
what this tool does, not because of where it points.

Nothing here touches the network: the E2E cases patch
``HttpFetchTransport.fetch``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

import pytest

from noeta.agent.registry import AgentRegistry
from noeta.agent.spec import AgentSpec, BudgetSpec, ComponentRef, ToolRef
from noeta.builtins.governance.impl.permission import PermissionGuard
from noeta.builtins.web.impl import HttpFetchTransport, WebFetchTool
from noeta.client import Client, Options
from noeta.client.host import SdkHost, _make_webfetch_approval_predicate
from noeta.client.host_config import HostConfig
from noeta.client.webfetch_policy import (
    host_in_allowlist,
    normalize_allowed_hosts,
    unsupported_scheme_refusal,
    url_host,
)
from noeta.core.engine import Engine
from noeta.protocols.messages import (
    LLMResponse,
    TextBlock,
    ToolUseBlock,
    Usage,
)
from noeta.protocols.tool import ToolContext, ToolResult
from noeta.storage.memory import (
    InMemoryContentStore,
    InMemoryDispatcher,
    InMemoryEventLog,
)
from noeta.testing.fake_llm import FakeLLMProvider


_PAGE = "<html><head><title>Cats</title></head><body><h1>Hi</h1></body></html>"
_PROMPT = "You are a test agent. Do what the user asks."


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


@dataclass
class FakeFetchTransport:
    """url → page; records every call so "never reached" is provable."""

    page: str = _PAGE
    calls: list[str] = field(default_factory=list)

    def fetch(self, url: str) -> str:
        self.calls.append(url)
        return self.page


def _ctx() -> ToolContext:
    return ToolContext(artifact_store=InMemoryContentStore())


def _args(url: str) -> dict[str, Any]:
    return {"url": url, "prompt": "What is this page about?"}


# ---------------------------------------------------------------------------
# The URL's real host
# ---------------------------------------------------------------------------


def test_userinfo_cannot_disguise_the_real_host() -> None:
    # ``allowed.com`` here is userinfo, not a host: the fetch goes to evil.com,
    # and the gate has to judge it as evil.com.
    assert url_host("https://allowed.com@evil.com/x") == "evil.com"


def test_host_normalisation_folds_the_spellings_of_one_host() -> None:
    # Case, the trailing root dot, percent-escapes and Unicode vs punycode all
    # fold, so an operator lists a host once and it matches however the model
    # spells it.
    assert url_host("https://EXAMPLE.COM./a") == "example.com"
    assert url_host("https://%65xample.com/a") == "example.com"
    assert url_host("https://пример.рф/a") == "xn--e1afmkfd.xn--p1ai"
    assert url_host("not a url") is None


def test_a_host_idna_cannot_encode_is_not_crashed_on() -> None:
    # An over-long label is not a valid name, so it fails the allowlist and the
    # call asks a human — but it must not take the gate down on the way.
    host = "a" * 70 + ".example.com"
    assert url_host(f"https://{host}/x") == host


# ---------------------------------------------------------------------------
# The operator's host allowlist
# ---------------------------------------------------------------------------


def test_allowlist_normalizes_case_trailing_dot_and_idna() -> None:
    assert normalize_allowed_hosts(
        ["Example.COM.", "*.Intra.Example.com", "пример.рф"]
    ) == ("example.com", "*.intra.example.com", "xn--e1afmkfd.xn--p1ai")


def test_allowlist_matches_whole_hosts_only() -> None:
    allowed = normalize_allowed_hosts(["allowed.com", "*.wild.com"])
    assert host_in_allowlist("allowed.com", allowed) is True
    # A near miss that a substring match would wave through.
    assert host_in_allowlist("notallowed.com", allowed) is False
    assert host_in_allowlist("allowed.com.evil.test", allowed) is False
    # The wildcard covers subdomains at any depth, but not the apex.
    assert host_in_allowlist("a.wild.com", allowed) is True
    assert host_in_allowlist("a.b.wild.com", allowed) is True
    assert host_in_allowlist("wild.com", allowed) is False
    assert host_in_allowlist("evil-wild.com", allowed) is False
    assert host_in_allowlist(None, allowed) is False


def test_allowlist_matches_the_punycode_spelling_of_a_unicode_host() -> None:
    allowed = normalize_allowed_hosts(["пример.рф"])
    assert host_in_allowlist(url_host("https://пример.рф/a"), allowed) is True


@pytest.mark.parametrize(
    "entry",
    [
        "",
        "   ",
        "https://example.com",
        "example.com/path",
        "user@example.com",
        "example.com:8443",
        "example.com?q=1",
        "*",
        "*.",
        "ev*il.com",
        "a.*.com",
        5,
    ],
)
def test_malformed_allowlist_entry_is_rejected_at_config_time(entry: Any) -> None:
    with pytest.raises(ValueError, match="webfetch_allowed_hosts"):
        normalize_allowed_hosts([entry])
    with pytest.raises(ValueError, match="webfetch_allowed_hosts"):
        HostConfig(webfetch_allowed_hosts=(entry,))


def test_wellformed_allowlist_passes_host_config() -> None:
    HostConfig(webfetch_allowed_hosts=("example.com", "*.intra.example.com"))


# ---------------------------------------------------------------------------
# The scheme check — the one outright refusal, and it refuses no host
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url", ["file:///etc/passwd", "gopher://example.com/", "ftp://x.com/a"]
)
def test_a_scheme_webfetch_does_not_fetch_is_refused(url: str) -> None:
    # httpx would refuse these itself, but the container transport hands the
    # URL to curl, which would read a container file and call it a page.
    assert "http(s)" in (unsupported_scheme_refusal(url) or "")


@pytest.mark.parametrize("url", ["https://x/", "http://10.0.0.1/wiki", "x.com/a"])
def test_every_other_url_passes_the_scheme_check(url: str) -> None:
    # Including a scheme-less one: the transport reports "no scheme" far more
    # usefully than this could.
    assert unsupported_scheme_refusal(url) is None


def test_the_scheme_refusal_is_an_ordinary_failed_tool_result() -> None:
    transport = FakeFetchTransport()
    result: ToolResult = WebFetchTool(transport=transport).invoke(
        _args("file:///etc/passwd"), _ctx()
    )
    assert isinstance(result, ToolResult)
    assert result.success is False
    assert "http(s)" in result.summary
    assert result.artifacts == []
    assert transport.calls == []


def test_the_tool_fetches_any_host_it_is_pointed_at() -> None:
    # No address is fenced: an agent holding Bash reaches the same target with
    # one curl, so a refusal here would protect nothing.
    transport = FakeFetchTransport()
    for url in ("https://10.0.0.1/wiki", "https://example.com/p"):
        assert WebFetchTool(transport=transport).invoke(_args(url), _ctx()).success
    assert transport.calls == ["https://10.0.0.1/wiki", "https://example.com/p"]


# ---------------------------------------------------------------------------
# The approval predicate
# ---------------------------------------------------------------------------


def test_webfetch_predicate_gates_unlisted_hosts_only() -> None:
    gate = _make_webfetch_approval_predicate(
        normalize_allowed_hosts(["allowed.com", "*.wild.com"])
    )
    assert gate("WebFetch", {"url": "https://allowed.com/x"}) is False
    assert gate("WebFetch", {"url": "https://a.wild.com/x"}) is False
    assert gate("WebFetch", {"url": "https://evil.test/x"}) is True
    assert gate("WebFetch", {"url": "https://notallowed.com/x"}) is True
    # The userinfo trick is judged by the real host.
    assert gate("WebFetch", {"url": "https://allowed.com@evil.test/x"}) is True
    # A cross-host redirect comes back to the model to re-issue; that second
    # call is an ordinary WebFetch and meets this same gate, so a redirect
    # cannot smuggle a fetch to an unapproved host.
    assert gate("WebFetch", {"url": "https://moved.evil.test/x"}) is True
    # Malformed input fails closed.
    assert gate("WebFetch", {"url": ""}) is True
    assert gate("WebFetch", {}) is True
    assert gate("WebFetch", {"url": "not a url"}) is True
    # Every other tool is untouched.
    assert gate("Read", {"file_path": "/etc/passwd"}) is False
    assert gate("Bash", {"command": "curl https://evil.test"}) is False


# ---------------------------------------------------------------------------
# SdkHost wiring: the gate composes with Bash's, and bypass builds none
# ---------------------------------------------------------------------------


def _main_spec() -> AgentSpec:
    return AgentSpec(
        name="main",
        instructions="You are the main agent.",
        policy=ComponentRef("react", "1"),
        composer=ComponentRef("three_segment", "v3"),
        tools=(
            ToolRef(name="Bash", risk_level="high", version="1"),
            ToolRef(name="WebFetch", risk_level="low", version="1"),
        ),
        plugins=(),
        default_budget=BudgetSpec(max_iterations=20),
        metadata={},
    )


def _host(tmp_path: Path, **overrides: Any) -> SdkHost:
    registry = AgentRegistry()
    registry.add(_main_spec())
    dispatcher = InMemoryDispatcher()
    kwargs: dict[str, Any] = dict(
        event_log=InMemoryEventLog(lease_validator=dispatcher),
        content_store=InMemoryContentStore(),
        dispatcher=dispatcher,
        provider=FakeLLMProvider(
            responses=[
                LLMResponse(
                    stop_reason="end_turn",
                    content=[TextBlock(text="ok")],
                    usage=Usage(uncached=1, output=1),
                    raw={"id": "r1"},
                )
            ]
        ),
        model="stub-model",
        workspace_dir=tmp_path,
        registry=registry,
        permission_mode="default",
        trust_store=tmp_path / "trust.json",
    )
    kwargs.update(overrides)
    return SdkHost(**kwargs)


def _predicate(host: SdkHost, **kwargs: Any) -> Any:
    engine: Engine = host._build_engine(
        _main_spec(),
        "stub-model",
        delegation_enabled=False,
        allowed_subtask_agents=frozenset(),
        ask_user_question_enabled=False,
        policy_wrapper=None,
        **kwargs,
    )
    guard = engine._hooks._guards[1].guard
    assert isinstance(guard, PermissionGuard)
    return guard._policy.conditional_approval


def test_host_gate_covers_webfetch_alongside_bash(tmp_path: Path) -> None:
    host = _host(tmp_path, webfetch_allowed_hosts=("docs.example.com",))
    gate = _predicate(host)
    # One slot, both gates.
    assert gate("WebFetch", {"url": "https://docs.example.com/a"}) is False
    assert gate("WebFetch", {"url": "https://evil.test/a"}) is True
    assert gate("Bash", {"command": "git status"}) is False
    assert gate("Bash", {"command": "bash -c 'x'"}) is True


def test_host_gate_covers_webfetch_under_accept_edits(tmp_path: Path) -> None:
    # acceptEdits only exempts the two edit-class tools; it is still a gating
    # mode, so an unlisted fetch asks.
    host = _host(tmp_path, webfetch_allowed_hosts=("docs.example.com",))
    gate = _predicate(host, permission_mode="acceptEdits")
    assert gate("WebFetch", {"url": "https://evil.test/a"}) is True
    assert gate("WebFetch", {"url": "https://docs.example.com/a"}) is False


def test_empty_allowlist_gates_every_fetch(tmp_path: Path) -> None:
    gate = _predicate(_host(tmp_path))
    assert gate("WebFetch", {"url": "https://docs.example.com/a"}) is True


def test_bypass_permissions_builds_no_gate(tmp_path: Path) -> None:
    host = _host(tmp_path, webfetch_allowed_hosts=("docs.example.com",))
    assert _predicate(host, permission_mode="bypassPermissions") is None


def test_host_rejects_a_malformed_allowlist(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="webfetch_allowed_hosts"):
        _host(tmp_path, webfetch_allowed_hosts=("https://x.com",))


# ---------------------------------------------------------------------------
# E2E through Client: request → approve / deny, and the listed short-circuit
# ---------------------------------------------------------------------------


@pytest.fixture()
def offline_fetch(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Stand in for the wire, keeping the real gate in place.

    Only ``HttpFetchTransport.fetch`` is replaced, so every decision about the
    call is the production one — the URL simply returns a page instead of
    opening a socket.
    """
    fetched: list[str] = []

    def _fake_fetch(self: HttpFetchTransport, url: str) -> str:
        fetched.append(url)
        return _PAGE

    monkeypatch.setattr(HttpFetchTransport, "fetch", _fake_fetch)
    return fetched


def _tooluse(call_id: str, url: str) -> LLMResponse:
    return LLMResponse(
        stop_reason="tool_use",
        content=[
            ToolUseBlock(
                call_id=call_id,
                tool_name="WebFetch",
                arguments={"url": url, "prompt": "what is this?"},
            )
        ],
        usage=Usage(uncached=1, output=1),
        raw={"id": call_id},
    )


def _end(text: str = "done") -> LLMResponse:
    return LLMResponse(
        stop_reason="end_turn",
        content=[TextBlock(text=text)],
        usage=Usage(uncached=1, output=1),
        raw={"id": "end"},
    )


def _client(
    tmp_path: Path,
    url: str,
    *,
    permission_mode: str = "default",
    allowed_hosts: Sequence[str] = (),
    can_use_tool: Any = None,
) -> Client:
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    return Client(
        Options(
            system_prompt=_PROMPT,
            allowed_tools=("WebFetch",),
            permission_mode=permission_mode,
            can_use_tool=can_use_tool,
        ),
        provider=FakeLLMProvider(responses=[_tooluse("f1", url), _end()]),
        workspace_dir=ws,
        model="stub-model",
        multi_turn=False,
        host_config=HostConfig(webfetch_allowed_hosts=tuple(allowed_hosts)),
    )


def _types(client: Client, task_id: str) -> list[str]:
    return [e.type for e in client.events(task_id)]


def test_unlisted_host_suspends_then_approve_runs_the_fetch(
    tmp_path: Path, offline_fetch: list[str]
) -> None:
    client = _client(tmp_path, "https://docs.example.com/guide")
    try:
        outcome = client.start(goal="read the guide")
        assert outcome.status == "suspended"
        assert outcome.wake_handle == "approval-f1"
        assert "ToolCallApprovalRequested" in _types(client, outcome.task_id)
        assert offline_fetch == []  # nothing was fetched before the human said so

        after = client.approve(outcome.task_id, call_id="f1")
        assert after.status == "terminal"
        types = _types(client, outcome.task_id)
        assert "ToolCallApprovalResolved" in types
        assert "ToolResultRecorded" in types
        assert offline_fetch == ["https://docs.example.com/guide"]
    finally:
        client.shutdown()


def test_unlisted_host_denied_returns_the_denial_and_never_fetches(
    tmp_path: Path, offline_fetch: list[str]
) -> None:
    client = _client(tmp_path, "https://docs.example.com/guide")
    try:
        outcome = client.start(goal="read the guide")
        assert outcome.status == "suspended"
        client.deny(outcome.task_id, call_id="f1", reason="not that host")
        types = _types(client, outcome.task_id)
        assert "ToolCallApprovalResolved" in types
        assert "ToolResultRecorded" not in types
        assert offline_fetch == []
    finally:
        client.shutdown()


def test_listed_host_is_never_gated(
    tmp_path: Path, offline_fetch: list[str]
) -> None:
    client = _client(
        tmp_path,
        "https://docs.example.com/guide",
        allowed_hosts=("*.example.com",),
    )
    try:
        outcome = client.start(goal="read the guide")
        assert outcome.status == "terminal"
        types = _types(client, outcome.task_id)
        assert "ToolCallApprovalRequested" not in types
        assert "ToolResultRecorded" in types
        assert offline_fetch == ["https://docs.example.com/guide"]
    finally:
        client.shutdown()


def test_can_use_tool_drains_the_webfetch_gate_like_any_other(
    tmp_path: Path, offline_fetch: list[str]
) -> None:
    seen: list[str] = []

    def _resolver(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        seen.append(tool_name)
        return {"behavior": "allow"}

    client = _client(
        tmp_path, "https://docs.example.com/guide", can_use_tool=_resolver
    )
    try:
        outcome = client.start(goal="read the guide")
        assert outcome.status == "terminal"
        assert seen == ["WebFetch"]
        assert "ToolResultRecorded" in _types(client, outcome.task_id)
        assert offline_fetch == ["https://docs.example.com/guide"]
    finally:
        client.shutdown()


def test_an_unlisted_intranet_host_is_the_same_question(
    tmp_path: Path, offline_fetch: list[str]
) -> None:
    # The gate is by host, not by address: an intranet host is gated when it is
    # unlisted and fetched when it is listed, exactly like a public one.
    client = _client(tmp_path, "https://wiki.internal/page")
    try:
        outcome = client.start(goal="read the internal wiki")
        assert outcome.status == "suspended"
        assert "ToolCallApprovalRequested" in _types(client, outcome.task_id)
        assert client.approve(outcome.task_id, call_id="f1").status == "terminal"
        assert offline_fetch == ["https://wiki.internal/page"]
    finally:
        client.shutdown()


def test_a_listed_intranet_host_is_fetched_without_approval(
    tmp_path: Path, offline_fetch: list[str]
) -> None:
    client = _client(
        tmp_path, "https://wiki.internal/page", allowed_hosts=("wiki.internal",)
    )
    try:
        outcome = client.start(goal="read it")
        assert outcome.status == "terminal"
        assert "ToolCallApprovalRequested" not in _types(client, outcome.task_id)
        assert offline_fetch == ["https://wiki.internal/page"]
    finally:
        client.shutdown()
