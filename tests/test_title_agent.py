"""The ``title`` internal agent (session-title generation).

The title agent is a host/runtime-side effect, NOT a preset and NOT a
Subtask: it has no tool whitelist and is not user-facing, so it stays out
of ``noeta.presets`` (no extra golden fingerprint — asserted here too) and
is organised like the compaction summarize round-trip (a single
deterministic ``LLMRequest`` through a plain ``LLMProvider``).

Coverage:

* it receives the conversation context and produces a short title;
* the request is deterministic + provider-neutral (the fixed system
  prompt rides ``system``, the conversation rides ``messages``);
* the deterministic post-clean shrinks a messy model response to one
  short single-line title (length cap honoured at a word boundary);
* empty conversation / empty response degrade to ``""`` (caller's
  fallback), with no provider call on an empty conversation;
* the agent is NOT registered as a preset / sub-agent (D8 guard).
"""

from __future__ import annotations

from noeta.execution.title import (
    DEFAULT_TITLE_MAX_CHARS,
    TITLE_SYSTEM_PROMPT,
    build_title_request,
    clean_title,
    generate_title,
)
from noeta.protocols.messages import (
    ImageBlock,
    LLMResponse,
    Message,
    TextBlock,
    Usage,
)
from noeta.protocols.values import ContentRef
from noeta.testing.fake_llm import FakeLLMProvider
from noeta.presets import OFFICIAL_SUBAGENTS, official_specs


def _conversation() -> list[Message]:
    return [
        Message(
            role="user",
            content=[TextBlock(text="The login page redirects to a 404 after sign-in.")],
        ),
        Message(
            role="assistant",
            content=[TextBlock(text="Let me look at the redirect handler.")],
        ),
    ]


def _resp(text: str) -> LLMResponse:
    return LLMResponse(
        stop_reason="end_turn",
        content=[TextBlock(text=text)],
        usage=Usage(uncached=1, output=1),
        raw={"id": "title"},
    )


# ---------------------------------------------------------------------------
# generate_title — receives context, returns a short title
# ---------------------------------------------------------------------------


def test_generate_title_returns_model_title() -> None:
    provider = FakeLLMProvider(responses=[_resp("Fix login redirect 404")])
    title = generate_title(provider, _conversation(), model="stub-model")
    assert title == "Fix login redirect 404"


def test_generate_title_passes_conversation_and_fixed_system() -> None:
    """The agent receives the conversation context; the fixed title
    instruction rides ``system`` (orthogonal to history)."""
    provider = FakeLLMProvider(responses=[_resp("Login redirect bug")])
    convo = _conversation()
    generate_title(provider, convo, model="stub-model")

    req = provider.received_requests[0]
    assert req.model == "stub-model"
    # Conversation is passed through verbatim as the history.
    assert req.messages == convo
    # No tools: the title agent never acts, it only reads + names.
    assert req.tools == []
    # The title instruction lives on ``system``, not inlined into history.
    assert req.system is not None
    assert req.system.role == "system"
    assert req.system.content[0].text == TITLE_SYSTEM_PROMPT


def test_build_title_request_is_deterministic() -> None:
    convo = _conversation()
    a = build_title_request(convo, model="m")
    b = build_title_request(convo, model="m")
    assert a == b


# ---------------------------------------------------------------------------
# clean_title — deterministic shrink to one short single-line title
# ---------------------------------------------------------------------------


def test_clean_title_takes_first_nonempty_line() -> None:
    assert clean_title("\n\nFix the bug\nextra reasoning line") == "Fix the bug"


def test_clean_title_strips_wrapping_quotes_and_marks() -> None:
    assert clean_title('"Quoted Title"') == "Quoted Title"
    assert clean_title("'Single Quoted'") == "Single Quoted"
    assert clean_title("“Smart Quotes”") == "Smart Quotes"
    assert clean_title("# Heading title") == "Heading title"
    assert clean_title("- Bulleted title") == "Bulleted title"


def test_clean_title_truncates_at_word_boundary() -> None:
    long = "Investigate the failing authentication flow across services and tenants"
    out = clean_title(long, max_chars=30)
    assert len(out) <= 30
    # cut at a word boundary, no trailing partial word
    assert not long[len(out) : len(out) + 1].strip() or long.startswith(out)
    assert " " in out and not out.endswith(" ")


def test_clean_title_hard_cuts_single_long_token() -> None:
    out = clean_title("supercalifragilisticexpialidocious", max_chars=10)
    assert out == "supercalif"


def test_clean_title_empty_response_is_empty() -> None:
    assert clean_title("") == ""
    assert clean_title("\n   \n") == ""


# ---------------------------------------------------------------------------
# degradation — empty conversation / empty response
# ---------------------------------------------------------------------------


def test_generate_title_empty_conversation_skips_provider() -> None:
    provider = FakeLLMProvider(responses=[])  # exhausted on first call
    assert generate_title(provider, [], model="m") == ""
    # No provider call was made (would have raised IndexError otherwise).
    assert provider.received_requests == []


def test_generate_title_empty_response_falls_back_to_empty() -> None:
    provider = FakeLLMProvider(responses=[_resp("   ")])
    assert generate_title(provider, _conversation(), model="m") == ""


def test_generate_title_ignores_non_text_blocks_in_response() -> None:
    """A response whose text rides among non-text blocks still titles."""
    resp = LLMResponse(
        stop_reason="end_turn",
        content=[TextBlock(text="Image triage")],
        usage=Usage(),
    )
    provider = FakeLLMProvider(responses=[resp])
    img = ContentRef(hash="0" * 64, size=9, media_type="image/png")
    convo = [
        Message(
            role="user",
            content=[TextBlock(text="what is in this screenshot?"), ImageBlock(source=img)],
        )
    ]
    assert generate_title(provider, convo, model="m") == "Image triage"


# ---------------------------------------------------------------------------
# D8 guard — the title agent is NOT a preset / sub-agent (no golden fingerprint)
# ---------------------------------------------------------------------------


def test_title_is_not_a_preset_subagent() -> None:
    assert "title" not in OFFICIAL_SUBAGENTS
    assert "title" not in official_specs()


def test_default_title_cap_is_sane() -> None:
    assert 20 <= DEFAULT_TITLE_MAX_CHARS <= 120
