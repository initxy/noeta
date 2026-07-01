"""D-C3: friendly alias (opus/sonnet/haiku) → real model-id resolution in the
driver, recorded into ModelBound and read back consistently by the resolver.

The allowlist check stays on the *alias* (so out-of-allowlist selectors still
raise before any durable write); resolution to the real id happens just before
the bind, so ModelBound / resolver._bound_model_for / req.model all carry the
real id and the pricing key never drifts.
"""

from __future__ import annotations

from tests._sdk_session import official_registry as official_agent_registry
from pathlib import Path

import pytest

from noeta.execution.driver import InteractionDriver, ModelSelectorError
from noeta.core.fold import fold
from noeta.providers.catalog import resolve_alias
from noeta.protocols.messages import LLMResponse, TextBlock, Usage
from noeta.protocols.values import Principal
from noeta.storage.memory import (
    InMemoryContentStore,
    InMemoryDispatcher,
    InMemoryEventLog,
)
from noeta.testing.fake_llm import FakeLLMProvider
from noeta.client import SdkHost
from noeta.execution.driver import multi_turn_policy_wrapper
from noeta.tools.fs import FsWriteMode, ShellMode


def _end_turn(text: str = "done") -> LLMResponse:
    return LLMResponse(
        stop_reason="end_turn",
        content=[TextBlock(text=text)],
        usage=Usage(uncached=1, output=1),
        raw={"id": "end-" + text},
    )


def _host(workspace: Path, *, responses, model: str = "gpt-test"):
    dispatcher = InMemoryDispatcher()
    event_log = InMemoryEventLog(lease_validator=dispatcher)
    content_store = InMemoryContentStore()
    host = SdkHost(
        event_log=event_log,
        content_store=content_store,
        dispatcher=dispatcher,
        provider=FakeLLMProvider(responses=responses),
        model=model,
        workspace_dir=workspace,
        write_mode=FsWriteMode.APPLY,
        shell_mode=ShellMode.ALLOWLIST,
        policy_wrapper=multi_turn_policy_wrapper,
    
        registry=official_agent_registry(),
        aliases={"default": "main"},
        require_approval_tools=())
    return host, dispatcher, event_log


def test_start_resolves_alias_to_real_model_id_in_model_bound(
    tmp_path: Path,
) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    host, _, event_log = _host(ws, responses=[_end_turn("hi")])
    driver = InteractionDriver(
        host,
        principal=Principal(identity="bob", allowed_models=frozenset({"opus"})),
    )
    out = driver.start(goal="hello", agent="main", model_selector="opus")

    bound = [e for e in event_log.read(out.task_id) if e.type == "ModelBound"]
    assert len(bound) == 1
    # The friendly alias 'opus' is resolved to its real id before binding.
    assert bound[0].payload.model == resolve_alias("opus") == "claude-opus-4-8"


def test_bound_model_matches_resolver_lookup(tmp_path: Path) -> None:
    """ModelBound.model and resolver._bound_model_for read the SAME real id —
    the (agent, model) cache key does not drift."""
    ws = tmp_path / "ws"
    ws.mkdir()
    host, _, event_log = _host(ws, responses=[_end_turn("hi")])
    driver = InteractionDriver(
        host,
        principal=Principal(identity="bob", allowed_models=frozenset({"haiku"})),
    )
    out = driver.start(goal="hello", agent="main", model_selector="haiku")

    folded = fold(event_log, host.content_store, out.task_id)
    assert folded.governance.model_binding == "claude-haiku-4-5"
    assert host._bound_model_for(folded) == "claude-haiku-4-5"


def test_selector_outside_allowlist_still_raises_before_resolution(
    tmp_path: Path,
) -> None:
    """Allowlist semantics must not regress: an out-of-allowlist selector
    raises with the ORIGINAL alias, leaving no durable write — resolution
    never happens."""
    ws = tmp_path / "ws"
    ws.mkdir()
    host, _, event_log = _host(ws, responses=[_end_turn()])
    driver = InteractionDriver(
        host, principal=Principal(identity="bob", allowed_models=frozenset({"sonnet"}))
    )
    with pytest.raises(ModelSelectorError) as exc:
        driver.start(goal="x", agent="main", model_selector="opus")
    assert exc.value.selector == "opus"  # original alias, not resolved
    assert getattr(event_log, "_streams", {}) == {}


def test_no_selector_binds_host_default_unresolved(tmp_path: Path) -> None:
    """R4: with no selector the host-fixed default is bound as-is (not run
    through alias resolution as a caller value)."""
    ws = tmp_path / "ws"
    ws.mkdir()
    host, _, event_log = _host(ws, responses=[_end_turn()], model="gpt-test")
    driver = InteractionDriver(host)  # ⊤ local principal, no selector
    out = driver.start(goal="hello", agent="main")
    bound = [e for e in event_log.read(out.task_id) if e.type == "ModelBound"]
    assert len(bound) == 1
    assert bound[0].payload.model == "gpt-test"
