"""Side requests in Claude Code's shape: identity then prompt, the main
request's default output, the task's effort, no thinking field on Anthropic models."""

from __future__ import annotations

from noeta.core import models
from noeta.core.types import user
from noeta.toolkit.side_requests import side_request


def test_an_anthropic_reasoning_model_gets_claude_codes_shape() -> None:
    r = side_request(model="claude-opus-5-5", identity="You are x.", prompt="Name it.",
                     messages=[user("q")])
    assert [p.text for p in r.system] == ["You are x.", "Name it."]
    assert not any(p.cache for p in r.system) and r.tools == []
    assert (r.thinking, r.effort, r.max_tokens) == (None, "medium", 128_000)
    assert side_request(model="claude-opus-5-5", identity="", messages=[user("q")],
                        effort="high").effort == "high"


def test_a_side_request_sends_the_models_default_output_and_effort() -> None:
    # Claude Code's helper calls carry the main request's max_tokens and
    # effort: the catalog defaults when the task sets neither.
    r = side_request(model="claude-sonnet-4-6", identity="i", messages=[user("q")])
    assert (r.max_tokens, r.effort) == (32_000, "high")


def test_other_models_switch_reasoning_off_and_send_no_effort() -> None:
    haiku = side_request(model="claude-haiku-4-5", identity="i", messages=[user("q")])
    assert (haiku.thinking, haiku.effort) == (None, None)
    for model in ("gpt-4o", "some-gateway-model"):
        r = side_request(model=model, identity="i", messages=[user("q")], effort="high")
        assert (r.thinking, r.effort) == (False, None)
    assert side_request(model="some-gateway-model", identity="", messages=[user("q")]
                        ).max_tokens == models.UNKNOWN_MODEL_MAX_OUTPUT_TOKENS
