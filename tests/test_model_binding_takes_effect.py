"""The model a request carries is the model the caller asked for, on the turn
they asked for it.

Two regressions, both invisible in the event log alone and only caught by
looking at what the provider actually received:

* with no ``model`` argument the Client fell back to the ``"sonnet"`` alias and
  sent that string verbatim — a real endpoint rejects it;
* a ``send_goal(..., model_selector=...)`` switch recorded its ``ModelBound``
  but the switching turn still ran on the previous model (and a crash-resume of
  that same turn would have folded the new one).
"""

from __future__ import annotations

from typing import Any

from noeta.sdk import Client, LLMResponse, Options, TextBlock, Usage, query
from noeta.sdk.providers import find_spec
from noeta.sdk.testing import FakeLLMProvider


class _RecordingProvider(FakeLLMProvider):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.models: list[str] = []

    def complete(self, request: Any, *args: Any, **kwargs: Any) -> Any:
        self.models.append(request.model)
        return super().complete(request, *args, **kwargs)


def _say(text: str) -> LLMResponse:
    return LLMResponse(
        stop_reason="end_turn",
        content=[TextBlock(text=text)],
        usage=Usage(uncached=1, output=1),
    )


def _options() -> Options:
    return Options(system_prompt="Be brief.", allowed_tools=())


def _real_id(alias: str) -> str:
    spec = find_spec(alias)
    assert spec is not None
    return spec.real_model_id


def test_default_model_is_sent_as_a_real_id() -> None:
    provider = _RecordingProvider(responses=[_say("hi")])
    query(_options(), goal="hi", provider=provider).answer()
    assert provider.models == [_real_id("sonnet")]
    assert provider.models[0] != "sonnet"


def test_explicit_alias_is_resolved_too() -> None:
    provider = _RecordingProvider(responses=[_say("hi")])
    query(_options(), goal="hi", provider=provider, model="opus").answer()
    assert provider.models == [_real_id("opus")]


def test_non_catalog_model_passes_through_unchanged() -> None:
    provider = _RecordingProvider(responses=[_say("hi")])
    query(_options(), goal="hi", provider=provider, model="my-gateway/model-x").answer()
    assert provider.models == ["my-gateway/model-x"]


def test_model_switch_applies_to_the_switching_turn() -> None:
    provider = _RecordingProvider(responses=[_say("a"), _say("b"), _say("c")])
    with Client(_options(), provider=provider, model="claude-sonnet-5") as client:
        task_id = client.start(goal="first").task_id
        client.send_goal(task_id, goal="second", model_selector="opus")
        client.send_goal(task_id, goal="third")
    opus = _real_id("opus")
    # Turn 1 on the opening model; the switch answers turn 2 on the new model;
    # turn 3 keeps it (the binding is sticky).
    assert provider.models == ["claude-sonnet-5", opus, opus]
