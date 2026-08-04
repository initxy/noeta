"""``react`` built-in implementation — the default policy and the workflow story.

Nothing imports this package statically: the SDK reaches
:func:`build_react_policy_factory` and the two control-tool factories only
through the manifest's ``ref`` strings at client build, which is what keeps
policy implementation out of the kernel.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from noeta.policies.control_semantics import ControlToolSpec
from noeta.protocols.content_store import ContentStore
from noeta.protocols.policy import Policy
from noeta.protocols.tool import Tool

from .control_tool import (
    STRUCTURED_OUTPUT_TOOL,
    build_run_workflow_control_tool,
    build_structured_output_control_tool,
    run_workflow_tool_schema,
    structured_output_tool_schema,
    translate_run_workflow,
)
from .orchestration import (
    OrchestrationPolicy,
    StructuredOutputPolicy,
    WORKFLOW_SYSTEM_PROMPT,
)
from .react import (
    ReActPolicy,
    enforce_verbatim_constraints,
    extract_safety_constraints,
)


__all__ = [
    "OrchestrationPolicy",
    "ReActPolicy",
    "STRUCTURED_OUTPUT_TOOL",
    "StructuredOutputPolicy",
    "WORKFLOW_SYSTEM_PROMPT",
    "build_react_policy_factory",
    "build_run_workflow_control_tool",
    "build_structured_output_control_tool",
    "enforce_verbatim_constraints",
    "extract_safety_constraints",
    "run_workflow_tool_schema",
    "structured_output_tool_schema",
    "translate_run_workflow",
]


def build_react_policy_factory(
    *,
    tools: dict[str, Tool],
    system_prompt: str,
    model: str,
    max_steps: int,
    control_translate_specs: tuple[ControlToolSpec, ...],
    content_store: ContentStore,
    context_window: Optional[int],
    max_output_tokens: Optional[int],
    compaction_buffer: Optional[int],
    tail_token_budget: int,
    composer_version: Optional[str],
    output_schema: Optional[dict[str, Any]],
    thinking: Optional[str],
    effort: Optional[str],
    compaction_model: Optional[str] = None,
    compaction_max_output_tokens: Optional[int] = None,
) -> Callable[[Any], Policy]:
    """The ``(llm) -> Policy`` factory the builder falls back to; ``Options.policy``
    and a plugin's ``policy`` contribution both outrank it.

    ``compaction_model`` / ``compaction_max_output_tokens`` default here
    (unlike their siblings) so a caller predating the knobs — a third-party
    ``PolicyFactoryBuilder`` implementation, or a test constructing the
    factory directly — keeps working unchanged.
    """

    def factory(llm: Any) -> Policy:
        return ReActPolicy(
            llm=llm,
            tools=tools,
            system_prompt=system_prompt,
            model=model,
            max_steps=max_steps,
            control_translate_specs=control_translate_specs,
            content_store=content_store,
            context_window=context_window,
            max_output_tokens=max_output_tokens,
            compaction_buffer=compaction_buffer,
            tail_token_budget=tail_token_budget,
            composer_version=composer_version,
            output_schema=output_schema,
            thinking=thinking,
            effort=effort,
            compaction_model=compaction_model,
            compaction_max_output_tokens=compaction_max_output_tokens,
        )

    return factory
