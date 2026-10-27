"""OpenAI Responses adapter — images: top-level images inline as
``input_image`` data URIs (any message, any role), the vision guard refuses a
catalogued text-only model before anything is sent, and tool-result images
become a content-part array or degrade to text with a note."""

from __future__ import annotations

import base64

import pytest
import respx

from noeta.core.errors import Fatal
from noeta.core.types import Image, Message, Text, ToolResult
from tests.backends.providers._responses_helpers import (
    JPEG_BYTES,
    PNG_BYTES,
    TEXT_ONLY_MODEL,
    VISION_MODEL,
    basic_request,
    data_uri,
    make_provider,
    mock_ok,
    sent_body,
    text_only_catalog_row,  # noqa: F401 — fixture
    user_message,
)

# ---------------------------------------------------------------------------
# Images
# ---------------------------------------------------------------------------


@respx.mock
async def test_image_in_user_message_becomes_input_image_data_uri() -> None:
    route = mock_ok()
    request = basic_request(
        model=VISION_MODEL,
        messages=[
            Message(
                role="user",
                content=[Text(text="what is this?"), Image(media_type="image/png", data=PNG_BYTES)],
            )
        ],
    )
    await make_provider().complete(request)
    assert sent_body(route)["input"] == [
        {
            "type": "message",
            "role": "user",
            "content": [
                {"type": "input_text", "text": "what is this?"},
                {"type": "input_image", "image_url": data_uri("image/png", PNG_BYTES)},
            ],
        }
    ]


@respx.mock
async def test_image_data_uri_prefix_and_base64_payload_correct() -> None:
    route = mock_ok()
    request = basic_request(
        model=VISION_MODEL,
        messages=[Message(role="user", content=[Image(media_type="image/jpeg", data=JPEG_BYTES)])],
    )
    await make_provider().complete(request)
    url = sent_body(route)["input"][0]["content"][0]["image_url"]
    assert url.startswith("data:image/jpeg;base64,")
    assert base64.b64decode(url.split(",", 1)[1]) == JPEG_BYTES


@respx.mock
async def test_image_in_historical_and_assistant_messages_is_inlined() -> None:
    route = mock_ok()
    png = Image(media_type="image/png", data=PNG_BYTES)
    request = basic_request(
        model=VISION_MODEL,
        messages=[
            Message(role="user", content=[Text(text="look at this"), png]),
            Message(role="assistant", content=[Text(text="here is mine"), png]),
            user_message("one more question"),
        ],
    )
    await make_provider().complete(request)
    items = sent_body(route)["input"]
    segment = {"type": "input_image", "image_url": data_uri("image/png", PNG_BYTES)}
    assert items[0]["content"] == [{"type": "input_text", "text": "look at this"}, segment]
    assert items[1]["role"] == "assistant"
    assert items[1]["content"] == [{"type": "output_text", "text": "here is mine"}, segment]
    assert items[2]["content"] == [{"type": "input_text", "text": "one more question"}]


@respx.mock
async def test_multiple_images_in_one_message_all_inlined_in_order() -> None:
    route = mock_ok()
    request = basic_request(
        model=VISION_MODEL,
        messages=[
            Message(
                role="user",
                content=[
                    Image(media_type="image/png", data=PNG_BYTES),
                    Text(text="compare these two"),
                    Image(media_type="image/jpeg", data=JPEG_BYTES),
                ],
            )
        ],
    )
    await make_provider().complete(request)
    assert sent_body(route)["input"][0]["content"] == [
        {"type": "input_image", "image_url": data_uri("image/png", PNG_BYTES)},
        {"type": "input_text", "text": "compare these two"},
        {"type": "input_image", "image_url": data_uri("image/jpeg", JPEG_BYTES)},
    ]


@pytest.mark.usefixtures("text_only_catalog_row")
@respx.mock
async def test_image_with_non_vision_model_raises_fatal_before_request() -> None:
    route = mock_ok()
    request = basic_request(
        model=TEXT_ONLY_MODEL,
        messages=[
            Message(role="user", content=[Image(media_type="image/png", data=PNG_BYTES)]),
            Message(role="assistant", content=[Text(text="got it")]),
            user_message("another question"),
        ],
    )
    with pytest.raises(Fatal, match="vision"):
        await make_provider().complete(request)
    assert not route.called


@respx.mock
async def test_image_with_unregistered_model_passes_through() -> None:
    """Absent from the catalog → unknown, not text-only: the image goes out."""
    route = mock_ok()
    request = basic_request(
        model="totally-unknown-model",
        messages=[Message(role="user", content=[Image(media_type="image/png", data=PNG_BYTES)])],
    )
    await make_provider().complete(request)
    assert sent_body(route)["input"][0]["content"] == [
        {"type": "input_image", "image_url": data_uri("image/png", PNG_BYTES)}
    ]


@pytest.mark.usefixtures("text_only_catalog_row")
@respx.mock
async def test_text_only_request_with_non_vision_model_passes_guard() -> None:
    route = mock_ok()
    await make_provider().complete(basic_request(model=TEXT_ONLY_MODEL, text="plain"))
    assert route.called


# --- tool-result images ------------------------------------------------------


def _tool_result_with_images(*images: bytes, text: str = "read /tmp/pic.png") -> Message:
    media = ["image/png", "image/jpeg"]
    return Message(
        role="user",
        content=[
            ToolResult(
                call_id="call_img",
                content=[
                    Text(text=text),
                    *(Image(media_type=media[i], data=b) for i, b in enumerate(images)),
                ],
            )
        ],
    )


@pytest.mark.parametrize("model", [VISION_MODEL, "totally-unknown-model"])
@respx.mock
async def test_tool_result_image_with_admitting_model_becomes_input_image_array(
    model: str,
) -> None:
    route = mock_ok()
    request = basic_request(model=model, messages=[_tool_result_with_images(PNG_BYTES, JPEG_BYTES)])
    await make_provider().complete(request)
    assert sent_body(route)["input"] == [
        {
            "type": "function_call_output",
            "call_id": "call_img",
            "output": [
                {"type": "input_text", "text": "read /tmp/pic.png"},
                {"type": "input_image", "image_url": data_uri("image/png", PNG_BYTES)},
                {"type": "input_image", "image_url": data_uri("image/jpeg", JPEG_BYTES)},
            ],
        }
    ]


@pytest.mark.usefixtures("text_only_catalog_row")
@respx.mock
async def test_tool_result_image_with_non_vision_model_degrades_to_string() -> None:
    """The vision guard does NOT fire on a tool-result image; it degrades to
    the string plus a note so the model knows an image existed."""
    route = mock_ok()
    request = basic_request(model=TEXT_ONLY_MODEL, messages=[_tool_result_with_images(PNG_BYTES)])
    await make_provider().complete(request)
    assert (
        sent_body(route)["input"][0]["output"]
        == "read /tmp/pic.png\n[image omitted: model is not vision-capable]"
    )


@respx.mock
async def test_tool_result_without_images_output_stays_string() -> None:
    route = mock_ok()
    request = basic_request(model=VISION_MODEL, messages=[_tool_result_with_images(text="plain")])
    await make_provider().complete(request)
    assert sent_body(route)["input"][0]["output"] == "plain"
