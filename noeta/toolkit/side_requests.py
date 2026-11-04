"""Model calls beside the task's own (a title, a plan name, a page digest),
shaped as Claude Code shapes them: the task's identity line, then the call's
own prompt as system blocks with no cache mark, no tools, the main
request's ``max_tokens`` (the model's default output; limits are in the
prompt or the schema), and the task's effort (else the model's default).
Reasoning is not asked for: on an Anthropic model the ``thinking``
field is left out (what Claude Code sends for its helper calls; some
gateways refuse an explicit ``disabled``), and so on a model the catalog
does not know (as its main requests leave it out); on another known
family it is switched off."""

from __future__ import annotations

from typing import Any, cast

from noeta.core import models
from noeta.core.types import Effort, Message, ModelRequest, SystemPart

__all__ = ["side_request"]


def side_request(*, model: str, identity: str, messages: list[Message], prompt: str = "",
                 effort: Effort | None = None,
                 output_schema: dict[str, Any] | None = None) -> ModelRequest:
    spec = models.find_spec(model)
    family = models.model_family(model)
    anthropic = family == "anthropic"
    return ModelRequest(
        model=model,
        system=[SystemPart(text=t) for t in (identity, prompt) if t],
        messages=messages,
        max_tokens=spec.output_default if spec is not None
        else models.UNKNOWN_MODEL_MAX_OUTPUT_TOKENS,
        thinking=None if anthropic or family is None else False,
        effort=(effort or cast(Effort, models.default_effort(model)))
        if anthropic and spec is not None and spec.is_reasoning else None,
        output_schema=output_schema)
