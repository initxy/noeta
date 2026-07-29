"""Contract test for the reference host (``examples/reference-host/host.py``).

This is the split spec's Phase-1 contract test (``docs/implementation-specs/
2026-07-26-sdk-only-repo-split.md``, decision D5), updated for the manifest
plugin mechanism (``docs/implementation-specs/2026-07-28-sdk-extensibility-
redesign.md``). It stands in for the real product after the agent moves to its
own repo. It boots the reference host — written against the ``noeta.sdk`` public
surface **only** — against an offline fake provider and asserts the host's
load-bearing claims:

1. **Streaming works.** A ``StreamingProvider`` + the host's ``delta_sink`` push
   ephemeral deltas while the turn is in flight; they reach the sink.
2. **Durable storage works.** The sqlite triple lands a real file on disk and
   the turn's events are persisted through it.
3. **Manifest plugins are wired.** The loaded :class:`~noeta.sdk.PluginSet`
   contributes process-wide governance guards / an observer (D6), and a
   per-agent ``tool_result_transform`` (``redaction``) is activated and applied
   before recording — the secret never lands in the durable ledger or content
   store (acceptance 10).

The host itself never imports a runtime internal; this test lives in the SDK's
own suite, so it may reach ``noeta.testing`` for the fake provider — that reach
is exactly what the split repo forbids the *product* and is fine here.
"""

from __future__ import annotations

import importlib.util
import io
import sys
from pathlib import Path

import pytest

from noeta.protocols.messages import LLMResponse, StreamDelta, TextBlock, Usage
from noeta.sdk import Options, ToolContext, ToolResult, ToolUseBlock, tool
from noeta.testing.fake_llm import FakeLLMProvider, FakeStreamingLLMProvider


_HOST_PATH = (
    Path(__file__).resolve().parents[1]
    / "examples"
    / "reference-host"
    / "host.py"
)
_REDACTION_PLUGIN = (
    Path(__file__).resolve().parents[1]
    / "examples"
    / "plugins"
    / "redaction"
    / "plugin.py"
)


def _load_host_module():
    spec = importlib.util.spec_from_file_location("_reference_host", _HOST_PATH)
    assert spec is not None and spec.loader is not None, _HOST_PATH
    mod = importlib.util.module_from_spec(spec)
    # Register before exec: the host defines a ``@dataclass`` under
    # ``from __future__ import annotations``, whose field-type resolution looks
    # the module up in ``sys.modules`` at class-creation time.
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _streaming_provider() -> FakeStreamingLLMProvider:
    """One end-of-turn answer, streamed as two text deltas that concatenate
    back to the final content (the invariant a real adapter guarantees)."""
    return FakeStreamingLLMProvider(
        responses=[
            LLMResponse(
                stop_reason="end_turn",
                content=[TextBlock(text="hi there")],
                usage=Usage(uncached=1, output=1),
            )
        ],
        deltas=[
            [
                StreamDelta(kind="text", text="hi ", index=0),
                StreamDelta(kind="text", text="there", index=0),
            ]
        ],
    )


@pytest.fixture()
def host_module():
    return _load_host_module()


def test_reference_host_module_documents_capability(host_module) -> None:
    """Guard: the reference host is a documented SDK example with a builder."""
    doc = host_module.__doc__ or ""
    assert "Demonstrated SDK capability" in doc
    assert hasattr(host_module, "build_reference_host")


def test_reference_host_streams_and_persists_with_plugins(
    host_module, tmp_path: Path
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    db_path = tmp_path / "state" / "noeta.sqlite"
    delta_out = io.StringIO()
    provider = _streaming_provider()

    host = host_module.build_reference_host(
        provider=provider,
        workspace_dir=workspace,
        db_path=db_path,
        delta_out=delta_out,
    )
    try:
        # The manifest plugins are loaded and wired per the effect-scoping rules
        # (D6): the governance guards / observer are process-wide (not gated on
        # activation), and the redaction transform is activated on the main agent.
        assert set(host.plugins.names()) >= {
            "protected-paths",
            "approval-modes",
            "git-checkpoint",
            "redaction",
        }
        assert "protected_paths" in host.guard_names()
        assert "approval_modes" in host.guard_names()
        assert "git_checkpoint" in host.observer_names()
        assert "redaction" in host.options.plugins  # per-agent activation

        outcome = host.run(goal="say hi")

        # 1. Streaming deltas arrived — the StreamingProvider path was taken and
        #    each delta reached the stdout sink (captured here into a buffer).
        assert provider.streamed_calls == 1, provider.streamed_calls
        assert provider.batch_calls == 0, provider.batch_calls
        assert host.delta_sink.delta_count == 2, host.delta_sink.delta_count
        streamed = delta_out.getvalue()
        assert streamed == "hi there", repr(streamed)

        # 2. Durable storage — the sqlite file exists and the turn's events were
        #    persisted through the injected triple.
        assert db_path.exists(), db_path
        events = host.events(outcome.task_id)
        assert events, "no events persisted to the sqlite event log"
        types = {e.type for e in events}
        assert "TaskCreated" in types, types
        # The streamed answer landed in the durable stream (not just the sink).
        assert "MessagesAppended" in types, types
    finally:
        host.close()

    # The sqlite file survives host teardown (it is the durable record).
    assert db_path.exists(), db_path


# ---------------------------------------------------------------------------
# The redaction tool_result_transform is wired end-to-end (acceptance 10).
# ---------------------------------------------------------------------------


_SECRET = "sk-TOP-SECRET-abcdefgh12345"


@tool(
    name="fetch_token",
    version="1",
    risk_level="low",
    input_schema={"type": "object", "additionalProperties": True},
)
def fetch_token(arguments: dict, ctx: ToolContext) -> ToolResult:
    """A deliberately leaky tool: it returns a secret in output and summary."""
    return ToolResult(
        success=True,
        output={"token": _SECRET},
        summary=f"fetched token {_SECRET}",
    )


def _leaky_provider() -> FakeLLMProvider:
    """Scripted: call the leaky tool once, then finish."""
    return FakeLLMProvider(
        responses=[
            LLMResponse(
                stop_reason="tool_use",
                content=[
                    ToolUseBlock(
                        call_id="ft-1", tool_name="fetch_token", arguments={}
                    )
                ],
                usage=Usage(uncached=1, output=1),
            ),
            LLMResponse(
                stop_reason="end_turn",
                content=[TextBlock(text="done")],
                usage=Usage(uncached=1, output=1),
            ),
        ]
    )


def test_reference_host_redaction_transform_scrubs_the_ledger(
    host_module, tmp_path: Path
) -> None:
    """The activated ``redaction`` transform runs before recording, so the leaky
    tool's secret reaches neither the durable event log nor the content store."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    db_path = tmp_path / "state" / "noeta.sqlite"

    # Load ONLY the redaction plugin (the governance guards would gate the tool
    # call), and give the agent just the leaky tool. permission_mode bypass keeps
    # the built-in permission guard from intervening.
    base = Options(
        system_prompt="Fetch the token when asked.",
        allowed_tools=(fetch_token,),
        permission_mode="bypassPermissions",
    )
    host = host_module.build_reference_host(
        provider=_leaky_provider(),
        workspace_dir=workspace,
        db_path=db_path,
        plugin_modules=[str(_REDACTION_PLUGIN)],
        activate=("redaction",),
        base_options=base,
    )
    try:
        assert host.plugins.names() == ("redaction",)
        assert "redaction" in host.options.plugins

        outcome = host.run(goal="fetch the token")

        events = host.events(outcome.task_id)
        # The tool ran (a ToolResultRecorded event exists) ...
        recorded = [e for e in events if e.type == "ToolResultRecorded"]
        assert recorded, "the leaky tool never ran"
        # ... and no event payload anywhere carries the secret.
        for env in events:
            assert _SECRET not in str(env.payload), env.type
        # The recorded summary was scrubbed to the redaction marker.
        assert any("***REDACTED***" in str(e.payload) for e in recorded)

        # The offloaded tool output blob in the content store is secret-free too
        # (redaction ran before the ToolRuntime recorded the output).
        for e in recorded:
            output_ref = getattr(e.payload, "output_ref", None)
            if output_ref is not None:
                body = host._content_store.get(output_ref)
                assert body is None or _SECRET.encode() not in body
    finally:
        host.close()
