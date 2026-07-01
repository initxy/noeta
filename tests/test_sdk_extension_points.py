"""T3 acceptance — the SDK ``Options`` extension surface + lockdown/seam.

Proves that the six pluggable extension points (Tool / LLMProvider / Policy /
Guard / Observer / ContentKindSpec) mount through ``Options`` and take effect,
that ``allowed_tools`` is replacement-style, that ``mcp_servers`` (in-process
SDK MCP) contribute their tools, and that the locked-down internals (Engine /
Dispatcher / whole-composer) have no public replacement entry.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from noeta.agent.spec import ComponentRef
from noeta.context.composer import RenderedSkills
from noeta.context.content_channel import ContentKindSpec
from noeta.protocols.decisions import FinishDecision, ToolCall, ToolCallsDecision
from noeta.protocols.hooks import ProposedToolCall, VerdictResult
from noeta.protocols.tool import ToolContext, ToolResult
from noeta.sdk import (
    Client,
    Options,
    compile_options,
    create_sdk_mcp_server,
    tool,
)
from noeta.testing.fake_llm import FakeLLMProvider


_ECHO_SCHEMA = {
    "type": "object",
    "properties": {"text": {"type": "string"}},
    "required": ["text"],
    "additionalProperties": False,
}


@tool(name="echo", version="1", risk_level="low", input_schema=_ECHO_SCHEMA)
def echo_tool(arguments: dict, ctx: ToolContext) -> ToolResult:
    """Echo back ``arguments['text']`` (a custom Tool extension)."""
    return ToolResult(success=True, output=str(arguments.get("text", "")))


class _RecordingGuard:
    """A custom Guard that records every tool-call check and always allows."""

    name = "recorder"
    priority = 100

    def __init__(self) -> None:
        self.checked: list[str] = []

    def check(self, action, ctx) -> VerdictResult:
        if isinstance(action, ProposedToolCall):
            self.checked.append(action.call.tool_name)
        return VerdictResult.allow()


class _ToolThenFinishPolicy:
    """A custom decision Policy: call ``echo`` once, then finish.

    Fully replaces ReAct — never touches the LLM. Stateful across the two
    ``decide`` calls of one turn (fresh instance per engine build).
    """

    def __init__(self) -> None:
        self._step = 0

    def decide(self, ctx, view):
        self._step += 1
        if self._step == 1:
            return ToolCallsDecision(
                calls=[
                    ToolCall(
                        tool_name="echo",
                        arguments={"text": "hello-from-custom-policy"},
                        call_id="c1",
                    )
                ]
            )
        return FinishDecision(answer="done")


class _CustomPolicyProvider:
    """``Options.policy`` shape: ``(llm) -> Policy`` carrying a ``.ref``."""

    @property
    def ref(self) -> ComponentRef:
        return ComponentRef("tool-then-finish", "1")

    def __call__(self, llm) -> _ToolThenFinishPolicy:
        return _ToolThenFinishPolicy()


def _empty_renderer(names: list[str]) -> RenderedSkills:
    """A custom content channel that renders nothing until activated.

    The composer calls every registered kind's renderer each compose with
    that kind's active names (empty here — the channel is mounted but never
    activated), so it must return a valid empty render.
    """
    return RenderedSkills(messages=[], selected_skills=[])


# ---------------------------------------------------------------------------
# compile_options: replacement tools, custom-policy identity, mcp tools
# ---------------------------------------------------------------------------


def test_allowed_tools_is_replacement_style() -> None:
    main, _ = compile_options(
        Options(system_prompt="x", allowed_tools=("read", "grep"))
    )
    assert {r.name for r in main.tools} == {"read", "grep"}


def test_custom_policy_sets_agent_identity_ref() -> None:
    main, _ = compile_options(
        Options(system_prompt="x", policy=_CustomPolicyProvider())
    )
    assert main.policy == ComponentRef("tool-then-finish", "1")


def test_default_policy_ref_is_react() -> None:
    main, _ = compile_options(Options(system_prompt="x"))
    assert main.policy == ComponentRef("react", "1")


def test_bad_custom_policy_without_ref_raises() -> None:
    with pytest.raises(TypeError):
        compile_options(Options(system_prompt="x", policy=object()))


def test_mcp_server_tools_enter_identity() -> None:
    server = create_sdk_mcp_server("toolbox", tools=[echo_tool])
    main, _ = compile_options(
        Options(
            system_prompt="x",
            allowed_tools=("read",),
            mcp_servers=(server,),
        )
    )
    # Replacement base ("read") PLUS the in-process server's tool ("echo").
    assert {r.name for r in main.tools} == {"read", "echo"}


# ---------------------------------------------------------------------------
# The headline acceptance: all six extension points on ONE Options, end-to-end
# ---------------------------------------------------------------------------


def test_all_six_extensions_mounted_on_one_options(tmp_path: Path) -> None:
    guard = _RecordingGuard()
    observed: list[str] = []
    channel = ContentKindSpec(kind="demo", renderer=_empty_renderer)
    provider = FakeLLMProvider(responses=[])  # mounted; custom policy bypasses it

    options = Options(
        system_prompt="you are a custom-policy agent",
        name="ext",
        allowed_tools=(echo_tool,),          # custom Tool
        policy=_CustomPolicyProvider(),       # custom Policy
        guards=(guard,),                      # custom Guard
        observers=(lambda env: observed.append(env.type),),  # custom Observer
        content_channels=(channel,),          # custom ContentKindSpec
        permission_mode="bypassPermissions",
    )

    client = Client(
        options,
        provider=provider,                    # custom LLMProvider
        workspace_dir=tmp_path,
        multi_turn=False,
    )
    try:
        # Identity reflects the swapped policy brain.
        assert client.main_agent_name == "ext"
        assert client._host.extra_content_kinds == (channel,)  # channel mounted

        outcome = client.start(goal="echo something")
        events = client.events(outcome.task_id)
    finally:
        client.shutdown()

    types = [e.type for e in events]
    # Tool extension ran (the custom policy drove a real tool call)...
    assert "ToolCallStarted" in types
    # ...the custom Guard was consulted for that call...
    assert "echo" in guard.checked
    # ...the custom Observer saw the post-commit stream...
    assert observed and "TaskCreated" in observed
    # ...and the custom Policy reached its terminal answer (not ReAct).
    assert any(e.type == "TaskCompleted" for e in events)


# ---------------------------------------------------------------------------
# Lockdown / seam: Engine, Dispatcher, whole-composer have no public entry
# ---------------------------------------------------------------------------


def test_facade_exposes_no_engine_or_composer_replacement() -> None:
    import noeta.sdk as sdk

    forbidden = {
        "Engine",
        "Dispatcher",
        "Worker",
        "ThreeSegmentComposer",
        "ContextComposer",
        "Composer",
    }
    assert forbidden.isdisjoint(set(sdk.__all__))
    for name in forbidden:
        assert not hasattr(sdk, name), f"noeta.sdk should not expose {name}"


def test_options_has_no_engine_or_composer_field() -> None:
    import dataclasses

    field_names = {f.name for f in dataclasses.fields(Options)}
    # The ONLY composer-extension seam is content_channels; there is no field
    # to replace the engine, dispatcher, or the composer as a whole.
    assert "content_channels" in field_names
    for forbidden in ("engine", "dispatcher", "composer", "worker"):
        assert forbidden not in field_names
