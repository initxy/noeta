"""Several providers behind one ``provider_for``: model listings, routing by
model id, windows from a listing, and the listing kept on disk."""

from __future__ import annotations

import re
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest

from noeta.backends.providers import listing as listing_module
from noeta.backends.providers.listing import (
    ListedModel,
    fetch_listing,
    listing_url,
    parse_listing,
)
from noeta.backends.providers.routing import Endpoint, ProviderRouter
from noeta.core import models
from noeta.core.errors import Fatal
from noeta.core.loop.state import MODEL_ROUTE
from noeta.core.types import ModelRequest
from noeta.testing import ScriptedProvider, reply

# The real one: the suite's autouse fixture stubs the module attribute.
_FETCH = fetch_listing


def fresh(name: str = "model") -> str:
    """A model id no other test registered (the catalog overlay is global)."""
    return f"{name}-{uuid.uuid4().hex[:8]}"


# -- listings ---------------------------------------------------------------


def test_parse_reads_every_published_limit() -> None:
    rows = parse_listing({"data": [
        {"id": "router/a", "context_length": 200000,
         "top_provider": {"max_completion_tokens": 32000},
         "architecture": {"input_modalities": ["text", "image"]}},
        {"id": "vllm-b", "max_model_len": 32768},
        {"id": "c", "context_window": "65536", "max_output_tokens": 8192,
         "input_modalities": ["text"]},
        {"id": "d"},
        {"no": "id"},
    ]})
    assert rows == [ListedModel("router/a", 200000, 32000, True),
                    ListedModel("vllm-b", 32768, None, None),
                    ListedModel("c", 65536, 8192, False),
                    ListedModel("d")]
    claude = parse_listing({"data": [{"id": "claude-x", "max_input_tokens": 1000000,
                                      "max_tokens": 128000}]}, "anthropic")
    assert claude == [ListedModel("claude-x", 1000000, 128000, None)]
    assert parse_listing([{"id": "bare"}]) == [ListedModel("bare")]
    assert parse_listing({"data": "nope"}) == []


def test_listing_urls() -> None:
    assert listing_url("anthropic", None) == "https://api.anthropic.com/v1/models"
    assert listing_url("anthropic", "https://gw/") == "https://gw/v1/models"
    assert listing_url("openai_compat", "https://gw/v1") == "https://gw/v1/models"
    assert listing_url("openai_compat", None) is None
    assert listing_url("openai_responses", "https://gw/v1/responses") == "https://gw/v1/models"
    assert listing_url("openai_responses", "https://gw/odd") is None
    assert listing_url("gemini", "https://gw") is None


def test_fetch_sends_the_providers_own_key_and_reads_every_page() -> None:
    seen: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if "after_id" not in request.url.params:
            return httpx.Response(200, json={"data": [{"id": "one"}], "has_more": True,
                                             "last_id": "one"})
        return httpx.Response(200, json={"data": [{"id": "two"}], "has_more": False})

    rows = _FETCH("anthropic", "https://gw", "k", {"x-extra": "1"},
                  transport=httpx.MockTransport(answer))
    assert [r.id for r in rows] == ["one", "two"]
    assert seen[0].headers["x-api-key"] == "k" and seen[0].headers["x-extra"] == "1"
    assert seen[1].url.params["after_id"] == "one"

    def compat(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"data": [{"id": "m", "context_length": 9000}]})

    assert _FETCH("openai_compat", "https://gw/v1", "sk",
                  transport=httpx.MockTransport(compat)) == [ListedModel("m", 9000)]
    assert seen[-1].headers["authorization"] == "Bearer sk"
    assert str(seen[-1].url) == "https://gw/v1/models"
    with pytest.raises(ValueError):
        _FETCH("gemini", "https://gw", "k")


# -- routing ----------------------------------------------------------------


class Listings:
    """A listing per endpoint, counting reads; ``fail`` raises instead."""

    def __init__(self, rows: list[ListedModel], fail: Exception | None = None) -> None:
        self.rows, self.fail, self.reads = rows, fail, 0

    def __call__(self) -> list[ListedModel]:
        self.reads += 1
        if self.fail is not None:
            raise self.fail
        return list(self.rows)


def inner(provider: Any) -> Any:
    return getattr(provider, "_inner", provider)


def endpoint(name: str, provider: Any, rows: Listings | None = None,
             family: str = "openai_compat") -> Endpoint:
    return Endpoint(name=name, family=family, make=lambda: provider, listing=rows,
                    base_url=f"https://{name}/v1")


def test_routing_order_explicit_then_listings_then_the_default() -> None:
    a, b, c = ScriptedProvider(), ScriptedProvider(), ScriptedProvider()
    listed_a, listed_b = Listings([ListedModel("both"), ListedModel("only-a")]), \
        Listings([ListedModel("both"), ListedModel("only-b")])
    router = ProviderRouter([endpoint("a", a, listed_a), endpoint("b", b, listed_b),
                             endpoint("c", c)], explicit={"pinned": "c"})
    assert router.route("pinned").name == "c"
    assert router.route("only-b").name == "b"
    assert router.route("both").name == "a"  # the default first
    assert router.route("nowhere").name == "a"
    assert (listed_a.reads, listed_b.reads) == (1, 1)  # read once per process
    # Several endpoints: each one's adapter behind its reasoning tagger.
    assert inner(router.provider_for("only-b")) is b
    assert inner(router.provider_for("pinned")) is c
    assert router.available() == [("both", "a"), ("only-a", "a"), ("only-b", "b"),
                                  ("pinned", "c")]


def test_one_endpoint_routes_without_reading_its_listing() -> None:
    rows = Listings([], fail=AssertionError("not read"))
    only = ScriptedProvider()
    router = ProviderRouter([endpoint("only", only, rows)])
    assert router.provider_for("claude-opus-5-5") is only
    assert router.route("anything").name == "only" and rows.reads == 0


async def test_an_unsettled_model_settles_on_its_first_call() -> None:
    model = fresh("gw-big")
    a, b = ScriptedProvider(), ScriptedProvider([reply("from b")])
    router = ProviderRouter([
        endpoint("a", a, Listings([])),
        endpoint("b", b, Listings([ListedModel(model, 262_144, 32_000, None)]))])
    proxy = router.provider_for(model)
    assert inner(proxy) is not b  # its route needs the listings
    response = await proxy.complete(ModelRequest(model=model))
    assert response.message.text == "from b" and b.requests[0].model == model
    # Its window came from b's listing; images are left to the provider.
    assert models.model_limits(model) == (262_144, 32_000)
    spec = models.find_spec(model)
    assert spec is not None and spec.supports_vision
    assert inner(router.provider_for(model)) is b  # settled now
    assert await proxy.count_tokens(model, "hi") is None  # b cannot count


def test_prepare_leaves_a_catalogued_model_and_an_unlisted_one_alone() -> None:
    unknown = fresh("nowhere")
    rows = Listings([ListedModel("claude-opus-5-5", 5)])
    router = ProviderRouter([endpoint("a", ScriptedProvider(), rows)])
    router.prepare("claude-opus-5-5")
    assert models.model_limits("claude-opus-5-5")[0] == 1_000_000
    router.prepare(unknown)
    assert models.find_spec(unknown) is None and rows.reads == 1
    assert models.unknown_model_notice(unknown) is not None


def test_listings_are_kept_on_disk(tmp_path: Path) -> None:
    now = [1000.0]
    rows = Listings([ListedModel("m1", 64_000)])

    def make(name: str, listed: Listings) -> ProviderRouter:
        return ProviderRouter([endpoint(name, ScriptedProvider(), listed)],
                              cache_dir=tmp_path, clock=lambda: now[0], max_age=100)

    assert make("a", rows).listing("a") == [ListedModel("m1", 64_000)]
    assert make("a", rows).listing("a") == [ListedModel("m1", 64_000)] and rows.reads == 1
    (_kept,) = tmp_path.glob("a-*.json")
    (tmp_path / "junk").write_text("x")
    assert make("zz", Listings([])).listing("zz") == []


@pytest.mark.parametrize("failure", [
    httpx.ConnectError("down"),
    httpx.HTTPStatusError("503", request=httpx.Request("GET", "https://a/v1/models"),
                          response=httpx.Response(503)),
    httpx.HTTPStatusError("429", request=httpx.Request("GET", "https://a/v1/models"),
                          response=httpx.Response(429)),
])
def test_a_transient_failure_keeps_the_stale_copy_and_tries_again(
        tmp_path: Path, failure: Exception) -> None:
    now = [1000.0]
    ProviderRouter([endpoint("a", ScriptedProvider(), Listings([ListedModel("old", 1)]))],
                   cache_dir=tmp_path, clock=lambda: now[0], max_age=100).listing("a")
    (kept,) = tmp_path.glob("a-*.json")
    before = kept.read_text()
    now[0] += 500  # stale
    failing = Listings([ListedModel("new", 2)], fail=failure)
    router = ProviderRouter([endpoint("a", ScriptedProvider(), failing)], cache_dir=tmp_path,
                            clock=lambda: now[0], max_age=100)
    assert router.listing("a") == [ListedModel("old", 1)] and failing.reads == 1
    assert kept.read_text() == before  # its age is not reset
    assert router.listing("a") == [ListedModel("old", 1)] and failing.reads == 1  # paused
    now[0] += 61
    assert router.listing("a") == [ListedModel("old", 1)] and failing.reads == 2
    now[0] += 61  # the pause grew (60 s, then up to the 100 s max age)
    router.listing("a")
    assert failing.reads == 2
    now[0] += 40
    failing.fail = None
    assert router.listing("a") == [ListedModel("new", 2)] and failing.reads == 3
    assert "new" in kept.read_text()


@pytest.mark.parametrize("status", [404, 405])
def test_an_endpoint_without_a_listing_is_not_asked_again(tmp_path: Path,
                                                          status: int) -> None:
    request = httpx.Request("GET", "https://b/v1/models")
    missing = Listings([], fail=httpx.HTTPStatusError(
        str(status), request=request, response=httpx.Response(status, request=request)))
    for _ in range(2):
        router = ProviderRouter([endpoint("b", ScriptedProvider(), missing)],
                                cache_dir=tmp_path, clock=lambda: 1000.0, max_age=100)
        assert router.listing("b") == [] and missing.reads == 1


def test_a_provider_that_cannot_be_built_fails_the_call() -> None:
    def broken() -> Any:
        raise ValueError("no key")

    router = ProviderRouter([endpoint("a", ScriptedProvider()),
                             Endpoint(name="b", family="anthropic", make=broken)],
                            explicit={"x": "b"})
    with pytest.raises(Fatal, match="provider 'b' cannot be used: no key"):
        router.provider_for("x")
    adopted = ScriptedProvider()
    router.adopt("b", adopted)
    assert inner(router.provider_for("x")) is adopted


def test_bad_configurations() -> None:
    with pytest.raises(ValueError):
        ProviderRouter([])
    with pytest.raises(ValueError):
        ProviderRouter([endpoint("a", None), endpoint("a", None)])
    with pytest.raises(ValueError, match="not configured"):
        ProviderRouter([endpoint("a", None)], explicit={"m": "zz"})


async def test_close_closes_what_was_built() -> None:
    closed: list[str] = []

    class Closing(ScriptedProvider):
        async def aclose(self) -> None:
            closed.append("x")

    router = ProviderRouter([endpoint("a", Closing())])
    router.provider_for("m")
    await router.aclose()
    assert closed == ["x"]


def test_the_suite_stubs_the_network_listing() -> None:
    assert listing_module.fetch_listing("anthropic", None, None) == []


# -- routes a task records ---------------------------------------------------


class State:
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config


def test_a_recorded_route_wins_over_the_listings() -> None:
    a, b = ScriptedProvider(), ScriptedProvider()
    down = Listings([], fail=httpx.ConnectError("down"))
    router = ProviderRouter([endpoint("a", a, down), endpoint("b", b, down)],
                            explicit={"claude-haiku-5-5": "a"})
    route = {"model": "on-b", "provider": "b"}
    assert inner(router.task_provider(State({MODEL_ROUTE: route}), "on-b")) is b  # type: ignore[arg-type]
    # A route of another model does not apply; that one resolves here.
    assert inner(router.task_provider(State({MODEL_ROUTE: route}), "claude-haiku-5-5")) is a  # type: ignore[arg-type]
    with pytest.raises(Fatal, match="provider 'c', which is not configured here"):
        router.task_provider(State({MODEL_ROUTE: {"model": "x", "provider": "c"}}), "x")  # type: ignore[arg-type]


def test_route_records() -> None:
    big = fresh("listed")
    one = ProviderRouter([endpoint("a", ScriptedProvider(), Listings([ListedModel(big, 99_000,
                                                                                    9_000)]))])
    assert one.route_record("claude-opus-5-5") is None  # nothing to decide differently
    assert one.route_record(big) == {"model": big, "provider": "a", "context_window": 99_000,
                                     "max_output_tokens": 9_000}
    two = ProviderRouter([endpoint("a", ScriptedProvider()), endpoint("b", ScriptedProvider())],
                         explicit={"x": "b"})
    assert two.route_record("x") == {"model": "x", "provider": "b"}
    assert two.route_record("opus") == {"model": "claude-opus-5-5", "provider": "a"}
    adopted = fresh("adopted")
    two.adopt_route({"model": adopted, "provider": "b", "context_window": 77_000,
                     "supports_vision": False})
    assert models.model_limits(adopted) == (77_000, models.UNKNOWN_MODEL_MAX_OUTPUT_TOKENS)
    two.adopt_route(None)


# -- replay across providers ---------------------------------------------------


def _signed(family: str | None, text: str) -> Any:
    from noeta.core.types import Message, Text, Thinking

    return Message(role="assistant", content=[
        Thinking(text=text, signature="sig", family=family), Text(text="answer")])


async def test_signed_reasoning_goes_back_only_to_the_provider_that_signed_it() -> None:
    from noeta.backends.providers._anthropic_wire import messages_to_wire
    from noeta.core.types import ModelResponse, Usage, user

    def answer(_r: ModelRequest) -> ModelResponse:
        return ModelResponse(message=_signed("anthropic", "mine"), stop_reason="end_turn",
                             usage=Usage())

    gw, direct = ScriptedProvider([answer, reply("x"), reply("y")]), ScriptedProvider(
        [reply("z")])
    router = ProviderRouter([endpoint("gw", gw, family="anthropic"),
                             endpoint("direct", direct, family="anthropic")],
                            explicit={"m": "gw", "n": "direct"})
    response = await router.provider_for("m").complete(ModelRequest(model="m"))
    [thinking] = [b for b in response.message.content if hasattr(b, "signature")]
    assert thinking.family == "anthropic@gw"  # as the log keeps it
    history = [user("q"), response.message, user("next")]
    await router.provider_for("m").complete(ModelRequest(model="m", messages=history))
    await router.provider_for("n").complete(ModelRequest(model="n", messages=history))

    def thinking_sent(request: ModelRequest) -> list[dict[str, Any]]:
        turns, _, _ = messages_to_wire(request.messages, True, True)
        return [b for t in turns if t["role"] == "assistant" for b in t["content"]
                if b["type"] == "thinking"]

    assert thinking_sent(gw.requests[1]) == [{"type": "thinking", "thinking": "mine",
                                              "signature": "sig"}]
    assert thinking_sent(direct.requests[0]) == []  # another endpoint's signature
    # Reasoning recorded before (untagged) is sent as it was.
    old = [user("q"), _signed("anthropic", "old"), user("next")]
    await router.provider_for("m").complete(ModelRequest(model="m", messages=old))
    assert thinking_sent(gw.requests[2])[0]["thinking"] == "old"


def test_tool_call_ids_another_provider_wrote_are_made_anthropics() -> None:
    from noeta.backends.providers._anthropic_wire import messages_to_wire, wire_call_id
    from noeta.core.types import Message, Text, ToolResult, ToolUse, user

    odd = "call:0/fn.1"
    history = [user("q"),
               Message(role="assistant", content=[ToolUse(id=odd, name="Read", input={})]),
               Message(role="user", content=[ToolResult(call_id=odd,
                                                        content=[Text(text="r")])])]
    turns, _, _ = messages_to_wire(history, True, True)
    used = turns[1]["content"][0]["id"]
    assert used == wire_call_id(odd) == turns[2]["content"][0]["tool_use_id"]
    assert re.fullmatch(r"[a-zA-Z0-9_-]+", used) and used != wire_call_id("call_0_fn_1")
    assert wire_call_id("toolu_01AbC-x") == "toolu_01AbC-x"
