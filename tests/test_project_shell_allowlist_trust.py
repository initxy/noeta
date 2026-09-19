"""The workspace's ``.noeta/shell-allowlist.json`` is gated on workspace trust.

The file is repository content: a cloned repo can carry one, and every rule in
it exempts a program from per-call approval. So the host loads it only when the
HOST-side workspace path is recorded in the plugin trust store (the same store
that gates workspace plugin directories and the workspace skill tiers).
Operator rules (``SdkHost.shell_allowlist``) are host configuration and stay
trusted; ``bypassPermissions`` never built a predicate at all and still does not.
"""

from __future__ import annotations

import json
import warnings
from pathlib import Path
from typing import Any, Optional

import pytest

from noeta.agent.registry import AgentRegistry
from noeta.agent.spec import AgentSpec, BudgetSpec, ComponentRef, ToolRef
from noeta.builtins.governance.impl.permission import PermissionGuard
from noeta.client.host import SdkHost, UntrustedProjectShellAllowlistWarning
from noeta.client.plugins import grant_trust
from noeta.core.engine import Engine
from noeta.protocols.messages import LLMResponse, TextBlock, Usage
from noeta.storage.memory import (
    InMemoryContentStore,
    InMemoryDispatcher,
    InMemoryEventLog,
)
from noeta.testing.fake_llm import FakeLLMProvider


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


def _stub_provider() -> FakeLLMProvider:
    return FakeLLMProvider(
        responses=[
            LLMResponse(
                stop_reason="end_turn",
                content=[TextBlock(text="ok")],
                usage=Usage(uncached=1, output=1),
                raw={"id": "r1"},
            )
        ]
    )


def _main_spec() -> AgentSpec:
    return AgentSpec(
        name="main",
        instructions="You are the main agent.",
        policy=ComponentRef("react", "1"),
        composer=ComponentRef("three_segment", "v3"),
        tools=(ToolRef(name="Bash", risk_level="high", version="1"),),
        plugins=(),
        default_budget=BudgetSpec(max_iterations=20),
        metadata={},
    )


def _workspace(tmp_path: Path, rules: Optional[list[dict[str, Any]]]) -> Path:
    ws = tmp_path / "repo"
    ws.mkdir(exist_ok=True)
    if rules is not None:
        (ws / ".noeta").mkdir(exist_ok=True)
        (ws / ".noeta" / "shell-allowlist.json").write_text(
            json.dumps(rules), encoding="utf-8"
        )
    return ws


def _host(ws: Path, tmp_path: Path, **overrides: Any) -> SdkHost:
    registry = AgentRegistry()
    registry.add(_main_spec())
    dispatcher = InMemoryDispatcher()
    kwargs: dict[str, Any] = dict(
        event_log=InMemoryEventLog(lease_validator=dispatcher),
        content_store=InMemoryContentStore(),
        dispatcher=dispatcher,
        provider=_stub_provider(),
        model="stub-model",
        workspace_dir=ws,
        registry=registry,
        permission_mode="default",
        # Hermetic: never read the operating user's ~/.noeta/trust.json.
        trust_store=tmp_path / "trust.json",
    )
    kwargs.update(overrides)
    return SdkHost(**kwargs)


def _build(host: SdkHost, **kwargs: Any) -> Engine:
    return host._build_engine(
        _main_spec(),
        "stub-model",
        delegation_enabled=False,
        allowed_subtask_agents=frozenset(),
        ask_user_question_enabled=False,
        policy_wrapper=None,
        **kwargs,
    )


def _predicate(engine: Engine) -> Any:
    perm_guard = engine._hooks._guards[1].guard
    assert isinstance(perm_guard, PermissionGuard)
    return perm_guard._policy.conditional_approval


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


def test_untrusted_project_rules_ignored_and_warned_once(tmp_path: Path) -> None:
    """An untrusted workspace contributes no rules, and warns exactly once."""
    ws = _workspace(tmp_path, [{"program": "bash"}])
    host = _host(ws, tmp_path)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        first = _predicate(_build(host))
        second = _predicate(_build(host))

    # The planted rule bought nothing: the command still needs a human.
    assert first("Bash", {"command": "bash -c 'x'"}) is True
    assert second("Bash", {"command": "bash -c 'x'"}) is True
    # ... while the curated base table is untouched.
    assert first("Bash", {"command": "git status"}) is False

    untrusted = [
        w for w in caught
        if isinstance(w.message, UntrustedProjectShellAllowlistWarning)
    ]
    assert len(untrusted) == 1, [str(w.message) for w in caught]
    warned = untrusted[0].message
    assert isinstance(warned, UntrustedProjectShellAllowlistWarning)
    assert warned.workspace_dir == ws
    assert warned.path == ws / ".noeta" / "shell-allowlist.json"
    assert str(warned.path) in str(warned)
    assert "grant_trust" in str(warned)


def test_trusted_project_rules_are_honored(tmp_path: Path) -> None:
    """A grant on the HOST-side workspace path restores today's behavior."""
    ws = _workspace(tmp_path, [{"program": "bash"}])
    store = tmp_path / "trust.json"
    grant_trust(ws, store)
    host = _host(ws, tmp_path)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        pred = _predicate(_build(host))

    assert pred("Bash", {"command": "bash -c 'x'"}) is False
    assert not [
        w for w in caught
        if isinstance(w.message, UntrustedProjectShellAllowlistWarning)
    ]


def test_no_project_file_no_warning(tmp_path: Path) -> None:
    """Nothing to ignore ⇒ nothing to warn about."""
    ws = _workspace(tmp_path, None)
    host = _host(ws, tmp_path)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        pred = _predicate(_build(host))

    assert pred("Bash", {"command": "git status"}) is False
    assert pred("Bash", {"command": "bash -c 'x'"}) is True
    assert not [
        w for w in caught
        if isinstance(w.message, UntrustedProjectShellAllowlistWarning)
    ]


def test_operator_rules_survive_an_untrusted_workspace(tmp_path: Path) -> None:
    """``SdkHost.shell_allowlist`` is host config — the gate never touches it."""
    ws = _workspace(tmp_path, [{"program": "bash"}])
    host = _host(
        ws, tmp_path, shell_allowlist=({"program": "npm", "subcommand": "run"},)
    )

    with warnings.catch_warnings(record=True):
        warnings.simplefilter("always")
        pred = _predicate(_build(host))
    assert pred("Bash", {"command": "npm run build"}) is False
    assert pred("Bash", {"command": "bash -c 'x'"}) is True


def test_bypass_permissions_unchanged(tmp_path: Path) -> None:
    """bypassPermissions builds no predicate and reads no project file."""
    ws = _workspace(tmp_path, [{"program": "bash"}])
    host = _host(ws, tmp_path)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        engine = _build(host, permission_mode="bypassPermissions")

    assert _predicate(engine) is None
    assert not [
        w for w in caught
        if isinstance(w.message, UntrustedProjectShellAllowlistWarning)
    ]


def test_open_mode_restores_the_ungated_load(tmp_path: Path) -> None:
    """A host that wants the old posture says so explicitly."""
    ws = _workspace(tmp_path, [{"program": "bash"}])
    host = _host(ws, tmp_path, project_shell_allowlist_trust="open")

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        pred = _predicate(_build(host))

    assert pred("Bash", {"command": "bash -c 'x'"}) is False
    assert not [
        w for w in caught
        if isinstance(w.message, UntrustedProjectShellAllowlistWarning)
    ]


def test_unknown_trust_mode_raises(tmp_path: Path) -> None:
    """A typo on a security knob must not read as "open"."""
    ws = _workspace(tmp_path, None)
    with pytest.raises(ValueError, match="project_shell_allowlist_trust"):
        _host(ws, tmp_path, project_shell_allowlist_trust="Open")


# ---------------------------------------------------------------------------
# Sandbox: the file is read through the container, the SUBJECT is host-side
# ---------------------------------------------------------------------------


class _FakeContainer:
    """Minimal ExecEnv stand-in: only ``read_text`` is exercised here."""

    def __init__(self, files: dict[str, bytes]) -> None:
        self._files = files

    def read_text(self, path: Path, *, encoding: str = "utf-8") -> str:
        try:
            return self._files[str(path)].decode(encoding)
        except KeyError as exc:  # pragma: no cover - mirrors a real miss
            raise FileNotFoundError(str(path)) from exc


def test_sandbox_gate_keys_on_the_host_side_subject(tmp_path: Path) -> None:
    """The container workdir every session shares is never the trust subject."""
    ws = _workspace(tmp_path, None)
    store = tmp_path / "trust.json"
    host = _host(ws, tmp_path)
    container = _FakeContainer(
        {"/workspace/.noeta/shell-allowlist.json": b'[{"program": "bash"}]'}
    )

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        untrusted = host._project_shell_rules(
            Path("/workspace"), ws, exec_env=container
        )
    assert untrusted == ()
    assert len(caught) == 1

    grant_trust(ws, store)
    assert host._project_shell_rules(
        Path("/workspace"), ws, exec_env=container
    ) == ({"program": "bash"},)
