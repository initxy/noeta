"""Contract test for the reference host (``examples/reference-host/host.py``).

This is the split spec's Phase-1 contract test (``docs/implementation-specs/
2026-07-26-sdk-only-repo-split.md``, decision D5): it stands in for the real
product after the agent moves to its own repo. It boots the reference host —
which is written against the ``noeta.sdk`` public surface **only** — against an
offline fake streaming provider, drives one turn end-to-end with a
plugin-contributed guard active, and asserts the two load-bearing host claims:

1. **Streaming works.** A ``StreamingProvider`` + the host's ``delta_sink``
   push ephemeral deltas while the turn is in flight; they reach the sink.
2. **Durable storage works.** The sqlite triple lands a real file on disk and
   the turn's events are persisted through it.

The host itself never imports a runtime internal; this test lives in the SDK's
own suite, so it may reach ``noeta.testing`` for the fake provider (the same
double every runtime streaming test runs on) — that reach is exactly what the
split repo forbids the *product* and is fine here.
"""

from __future__ import annotations

import importlib.util
import io
import sys
from pathlib import Path

import pytest

from noeta.protocols.messages import LLMResponse, StreamDelta, TextBlock, Usage
from noeta.testing.fake_llm import FakeStreamingLLMProvider


_HOST_PATH = (
    Path(__file__).resolve().parents[1]
    / "examples"
    / "reference-host"
    / "host.py"
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


def test_reference_host_streams_and_persists_with_plugin_guard(
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
        plugin_config=host_module.default_plugin_config(workspace),
        delta_out=delta_out,
    )
    try:
        # A plugin-contributed guard is active before the turn runs: the
        # protected-paths plugin folded its Guard into the compiled recipe.
        guard_names = {getattr(g, "name", None) for g in host.options.guards}
        assert "protected_paths" in guard_names, guard_names
        # And the git-checkpoint plugin folded its Observer in (host plane).
        observer_names = {
            getattr(o, "name", None) for o in host.options.observers
        }
        assert "git_checkpoint" in observer_names, observer_names

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
