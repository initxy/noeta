"""Model calls beside the task's own (a title, a plan name, a page digest),
shaped as Claude Code shapes them: the task's identity line, then the call's
own prompt as system blocks with no cache mark, no tools, the model's whole
output ceiling (limits are in the prompt or the schema), and the task's
effort. Reasoning is not asked for: on an Anthropic model the ``thinking``
field is left out (what Claude Code sends for its helper calls; some
gateways refuse an explicit ``disabled``), elsewhere it is switched off."""

from __future__ import annotations

from typing import Any

from noeta.core import models
from noeta.core.types import Effort, Message, ModelRequest, SystemPart

__all__ = ["DEFAULT_EFFORT", "side_request"]

#: The effort an Anthropic reasoning model is asked for when the task sets
#: none (the provider adapter's default for the main request).
DEFAULT_EFFORT: Effort = "medium"


def side_request(*, model: str, identity: str, messages: list[Message], prompt: str = "",
                 effort: Effort | None = None,
                 output_schema: dict[str, Any] | None = None) -> ModelRequest:
    spec = models.find_spec(model)
    anthropic = models.model_family(model) == "anthropic"
    return ModelRequest(
        model=model,
        system=[SystemPart(text=t) for t in (identity, prompt) if t],
        messages=messages,
        max_tokens=spec.max_output_tokens if spec is not None
        else models.UNKNOWN_MODEL_MAX_OUTPUT_TOKENS,
        thinking=None if anthropic else False,
        effort=(effort or DEFAULT_EFFORT) if anthropic and spec is not None
        and spec.is_reasoning else None,
        output_schema=output_schema)
