"""Live smoke tests: each adapter against a real gateway (``-m live``).

Same env contract as the 0.6 live suite: ``NOETA_LIVE_BASE_URL``,
``NOETA_LIVE_API_KEY``, ``NOETA_LIVE_MODEL`` (optional
``NOETA_LIVE_MAX_TOKENS``), read from the environment or a repo-root ``.env``
(git-ignored). One gateway, three endpoints: ``/v1/messages``,
``/v1/chat/completions``, ``/v1/responses``. Missing any of base/key/model
skips. Assertions check structure only — a real model's wording is not
something a test can pin.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from noeta.backends.providers import Provider, provider_for
from noeta.core.retry import complete_with_retries
from noeta.core.types import (
    Delta,
    Message,
    ModelRequest,
    SystemPart,
    Text,
    ToolResult,
    ToolSpec,
    user,
)

pytestmark = pytest.mark.live


def _load_dotenv() -> None:
    """``KEY=VALUE`` lines from the nearest ``.env`` up the tree, without
    clobbering variables already set."""
    for parent in Path(__file__).resolve().parents:
        path = parent / ".env"
        if path.is_file():
            for raw in path.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                if key.strip():
                    os.environ.setdefault(key.strip(), value.strip().strip("'\""))
            return


_load_dotenv()
BASE = (os.environ.get("NOETA_LIVE_BASE_URL") or "").rstrip("/")
KEY = os.environ.get("NOETA_LIVE_API_KEY") or ""
MODEL = os.environ.get("NOETA_LIVE_MODEL") or ""
MAX_TOKENS = int(os.environ.get("NOETA_LIVE_MAX_TOKENS") or 1024)

requires_live = pytest.mark.skipif(
    not (BASE and KEY and MODEL),
    reason="live tests need NOETA_LIVE_BASE_URL / NOETA_LIVE_API_KEY / NOETA_LIVE_MODEL",
)


def _build(family: str) -> Provider:
    if family == "anthropic":
        return provider_for("anthropic", base_url=BASE, api_key=KEY)
    if family == "openai_compat":
        return provider_for("openai_compat", base_url=f"{BASE}/v1", api_key=KEY)
    # The adapter sends an Azure-style ``api-key``; a gateway fronting the
    # OpenAI shape may want a Bearer token, which rides extra_headers.
    return provider_for(
        "openai_responses",
        base_url=f"{BASE}/v1/responses",
        api_key=KEY,
        extra_headers={"Authorization": f"Bearer {KEY}"},
    )


_WEATHER = ToolSpec(
    name="get_weather",
    description="Get the current weather for a city.",
    input_schema={
        "type": "object",
        "properties": {"city": {"type": "string"}},
        "required": ["city"],
    },
)

FAMILIES = ["anthropic", "openai_compat", "openai_responses"]


@requires_live
@pytest.mark.parametrize("family", FAMILIES)
async def test_live_streaming_text(family: str) -> None:
    provider = _build(family)
    deltas: list[Delta] = []
    response = await complete_with_retries(
        provider,
        ModelRequest(
            model=MODEL,
            system=[SystemPart(text="You are terse.", cache=True)],
            messages=[user("Reply with the single word: pong")],
            max_tokens=MAX_TOKENS,
        ),
        deltas.append,
        max_attempts=3,
    )
    assert response.stop_reason in ("end_turn", "max_tokens")
    assert response.message.text.strip()
    assert response.usage.input > 0
    assert response.usage.output > 0
    streamed = "".join(d.text for d in deltas if d.kind == "text")
    assert streamed == response.message.text


@requires_live
@pytest.mark.parametrize("family", FAMILIES)
async def test_live_tool_round_trip(family: str) -> None:
    provider = _build(family)
    first_request = ModelRequest(
        model=MODEL,
        system=[SystemPart(text="Use the get_weather tool to answer weather questions.")],
        messages=[user("What's the weather in Paris? Use the tool.")],
        tools=[_WEATHER],
        max_tokens=MAX_TOKENS,
    )
    first = await complete_with_retries(provider, first_request, max_attempts=3)
    assert first.stop_reason == "tool_use"
    calls = first.message.tool_uses
    assert calls and calls[0].name == "get_weather"
    assert isinstance(calls[0].input, dict)

    results = Message(
        role="user",
        content=[
            ToolResult(call_id=call.id, content=[Text(text="Sunny, 21 C")])
            for call in calls
        ],
    )
    second = await complete_with_retries(
        provider,
        ModelRequest(
            model=MODEL,
            system=first_request.system,
            messages=[*first_request.messages, first.message, results],
            tools=[_WEATHER],
            max_tokens=MAX_TOKENS,
            cache_after=[2],
        ),
        max_attempts=3,
    )
    assert second.stop_reason in ("end_turn", "max_tokens")
    assert second.message.text.strip()
