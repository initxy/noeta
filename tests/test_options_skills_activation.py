"""``Options.skills`` → pre-loop activation (bugfix regression).

``compile_options`` always turned ``Options.skills`` into
``AgentSpec.skills`` (a tuple of ``ComponentRef``s, covered by
``test_client_options.py::test_skills_become_component_refs_with_default_version``),
but nothing downstream ever *read* the compiled spec's ``skills`` field —
declaring ``Options(skills=["x"])`` was pure identity decoration with zero
behavior change. Real skill activation flowed only through the driver-level
pre-loop ``activations`` selector (a slash-command mechanism ``Options``
never fed) and the model-driven ``skill`` control tool
(``Capabilities.skill_invocation``).

This module proves the fix: a declared skill now rides the SAME pre-loop
``TaskStatePatch(activate_skills=...)`` channel the ``activations`` selector
uses, so it is active/available from the model's very first request — not
merely recorded in state after the fact.
"""

from __future__ import annotations

from pathlib import Path

from noeta.client import Client, Options
from noeta.core.fold import fold
from noeta.protocols.messages import LLMResponse, Message, TextBlock, Usage
from noeta.testing.fake_llm import FakeLLMProvider

from tests._skill_fixtures import write_skill


def _end_turn(req=None) -> LLMResponse:  # noqa: ANN001 — responder shape
    return LLMResponse(
        stop_reason="end_turn",
        content=[TextBlock(text="done")],
        usage=Usage(uncached=1, output=1),
        raw={"id": "end"},
    )


def _client(
    tmp_path: Path, *, skills: tuple[str, ...] = ()
) -> tuple[Client, FakeLLMProvider]:
    provider = FakeLLMProvider(responder=_end_turn)
    client = Client(
        Options(system_prompt="test agent", name="main", skills=skills),
        provider=provider,
        workspace_dir=tmp_path,
        model="gpt-test",
    )
    return client, provider


def _rendered_text(*messages: Message) -> str:
    return "\n".join(
        block.text
        for message in messages
        for block in message.content
        if isinstance(block, TextBlock)
    )


def test_declared_skill_is_rendered_in_the_first_request(tmp_path: Path) -> None:
    """``Options(skills=["greet"])`` must materialize the skill body in the
    VERY FIRST ``LLMRequest`` the model sees — proving the declared skill is
    active/available from the first step, not just recorded in durable state
    after the model already ran without it."""
    write_skill(tmp_path, "greet", description="say hi")
    client, provider = _client(tmp_path, skills=("greet",))
    try:
        client.start(goal="hi")
        assert len(provider.received_requests) >= 1
        first_request = provider.received_requests[0]
        text = _rendered_text(first_request.system, *first_request.messages)
        assert "Activated skill: greet" in text
        assert "Body of the greet skill." in text
    finally:
        client.shutdown()


def test_declared_skill_is_recorded_in_durable_active_skills(tmp_path: Path) -> None:
    """The declared activation is durable — a resumed/refolded task reproduces
    the same active set (the same channel a slash-command ``activations``
    selector already rides)."""
    write_skill(tmp_path, "greet", description="say hi")
    client, _ = _client(tmp_path, skills=("greet",))
    try:
        out = client.start(goal="hi")
        folded = fold(
            client._host.event_log, client._host.content_store, out.task_id
        )
        assert "greet" in folded.state.active_skills
        assert "greet" in folded.state.active_content.get("skill", ())
    finally:
        client.shutdown()


def test_no_declared_skills_stays_byte_identical_to_before(tmp_path: Path) -> None:
    """A bare ``Options`` (no ``skills``) activates nothing — the default,
    overwhelmingly common path must be unaffected by the fix."""
    client, _ = _client(tmp_path)
    try:
        out = client.start(goal="hi")
        folded = fold(
            client._host.event_log, client._host.content_store, out.task_id
        )
        assert folded.state.active_skills == []
        assert "skill" not in folded.state.active_content
    finally:
        client.shutdown()


def test_declared_skill_dedupes_with_an_explicit_slash_activation(
    tmp_path: Path,
) -> None:
    """A skill that is BOTH declared on ``Options.skills`` AND separately
    activated via the driver's ``activations`` selector (the slash-command
    channel) activates exactly once — no duplicate entries, no duplicate
    provenance event."""
    write_skill(tmp_path, "greet", description="say hi")
    client, _ = _client(tmp_path, skills=("greet",))
    try:
        # Reach directly under the driver seam (mirrors ``tests/_sdk_session.py``'s
        # direct-``InteractionDriver`` pattern) to also pass an explicit
        # ``activations`` selector for the SAME skill name declared on Options —
        # ``activations`` has no public ``Client`` surface (it's the driver-level
        # slash-command channel), so this is the only way to exercise the merge.
        outcome = client._driver.start(  # noqa: SLF001 — exercising the merge point
            goal="hi",
            agent="main",
            activations=("greet",),
        )
        folded = fold(
            client._host.event_log, client._host.content_store, outcome.task_id
        )
        assert folded.state.active_skills == ["greet"]
    finally:
        client.shutdown()
