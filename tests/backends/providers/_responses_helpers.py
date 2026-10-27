"""Shared fixtures for the OpenAI Responses adapter tests."""

from __future__ import annotations

import base64
import json
from typing import Any

import httpx
import pytest
import respx

from noeta.backends.providers.openai_responses import OpenAIResponsesProvider
from noeta.core import models
from noeta.core.types import Delta, Message, ModelRequest, Text
from tests.backends.providers.sse import responses_sse, sse_bytes, sse_response

#: base_url IS the complete responses endpoint: the provider POSTs to it as is,
#: adding only the ``?api-version`` query. Appending a path would produce a
#: doubled segment a real gateway rejects, so the two are the same string.
BASE_URL = "https://gateway.test/api/modelhub/online/responses"
ENDPOINT = BASE_URL

#: No shipped catalog row is text-only, so the non-vision paths run against a
#: row registered for the test (see :func:`text_only_catalog_row`).
TEXT_ONLY_MODEL = "text-only-model"
#: Catalogued as vision + reasoning.
VISION_MODEL = "gpt-5.4-2026-03-05"

PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082"
)
JPEG_BYTES = b"\xff\xd8\xff\xe0fake-jpeg-bytes\xff\xd9"


@pytest.fixture
def text_only_catalog_row(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        models,
        "_EXTENSIONS",
        {
            **models._EXTENSIONS,
            TEXT_ONLY_MODEL: models.ModelSpec(
                real_model_id=TEXT_ONLY_MODEL,
                context_window=128_000,
                max_output_tokens=16_384,
                supports_vision=False,
            ),
        },
    )


def make_provider(**overrides: Any) -> OpenAIResponsesProvider:
    kwargs: dict[str, Any] = {"base_url": BASE_URL, "api_key": "sk-test"}
    kwargs.update(overrides)
    return OpenAIResponsesProvider(**kwargs)


def user_message(text: str) -> Message:
    return Message(role="user", content=[Text(text=text)])


def basic_request(
    *,
    model: str = "gpt-5.4",
    text: str = "hi",
    messages: list[Message] | None = None,
    **kw: Any,
) -> ModelRequest:
    return ModelRequest(
        model=model,
        messages=messages if messages is not None else [user_message(text)],
        **kw,
    )


def responses_payload(
    *,
    texts: list[str] | None = None,
    status: str = "completed",
    incomplete_reason: str | None = None,
    extra_output: list[dict[str, Any]] | None = None,
    usage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """A Responses response object. ``texts`` → one ``message`` item whose
    ``content[]`` holds that many ``output_text`` segments (None → no message
    item); ``extra_output`` is appended after it."""
    output: list[dict[str, Any]] = []
    if texts is not None:
        output.append(
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": t} for t in texts],
            }
        )
    if extra_output:
        output.extend(extra_output)
    payload: dict[str, Any] = {
        "id": "resp-xyz",
        "object": "response",
        "model": "gpt-5.4",
        "status": status,
        "output": output,
    }
    if incomplete_reason is not None:
        payload["incomplete_details"] = {"reason": incomplete_reason}
    if usage is not None:
        payload["usage"] = usage
    return payload


def ok_stream(payload: dict[str, Any] | None = None) -> httpx.Response:
    """A 200 SSE response whose terminal event carries ``payload``."""
    return sse_response(responses_sse(payload or responses_payload(texts=["ok"])))


def mock_ok(payload: dict[str, Any] | None = None) -> respx.Route:
    """Route answering every call with a fresh stream carrying ``payload``."""
    return respx.post(ENDPOINT).mock(side_effect=lambda _request: ok_stream(payload))


def sent_body(route: respx.Route, index: int = 0) -> dict[str, Any]:
    body: dict[str, Any] = json.loads(route.calls[index].request.content)
    return body


def data_uri(media_type: str, body: bytes) -> str:
    return f"data:{media_type};base64,{base64.b64encode(body).decode('ascii')}"


def text_delta(fragment: str, output_index: int = 0) -> tuple[str, dict[str, Any]]:
    return (
        "response.output_text.delta",
        {
            "type": "response.output_text.delta",
            "item_id": "msg_1",
            "output_index": output_index,
            "content_index": 0,
            "delta": fragment,
        },
    )


def completed(final: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    return ("response.completed", {"type": "response.completed", "response": final})


def stream_of(frames: list[tuple[str | None, Any]]) -> httpx.Response:
    return sse_response(sse_bytes(frames))


class DeltaRecorder:
    def __init__(self) -> None:
        self.deltas: list[Delta] = []

    def __call__(self, delta: Delta) -> None:
        self.deltas.append(delta)

    @property
    def as_tuples(self) -> list[tuple[str, str, int]]:
        return [(d.kind, d.text, d.index) for d in self.deltas]
