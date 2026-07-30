"""``react`` built-in — implementation package (microkernel phase 2b).

The official decision-mapping policy: :class:`ReActPolicy` (``react``) and the
workflow interpreter :class:`OrchestrationPolicy` + ``StructuredOutputPolicy``
+ ``WORKFLOW_SYSTEM_PROMPT`` (``orchestration``). Since control-tool-surface S2b
this built-in ALSO owns the workflow control tools — ``run_workflow`` +
``structured_output`` schemas + translate + their ``.md`` descriptions
(``control_tool``) and the determinism sandbox (``workflow_sandbox``) — declared
as two ``control_tool`` contributions on the manifest. The kernel keeps only the
neutral control MECHANISM (``noeta.policies.control_semantics`` — the mount /
routing types + reserved ``run_workflow`` / ``__workflow__`` vocabulary) and the
generic multi-turn wrapper (``noeta.execution.multi_turn``); nothing imports this
package statically — the SDK resolves :func:`build_react_policy_factory` and the
two control-tool factories through the manifest at client build.
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
) -> Callable[[Any], Policy]:
    """The kernel builder's ``default_policy_factory`` injection.

    Takes exactly the kernel-computed kwargs the builder used to close over
    inline and returns the ``(llm) -> Policy`` factory — the same
    :class:`ReActPolicy` construction, byte-identical prompts and schemas.
    ``Options.policy`` / the plugin ``policy`` surface (D10) still take
    priority over this default at the builder.

    Control-tool-surface S1: the policy receives the routing-ordered
    ``control_translate_specs`` the mount loop produced, replacing the five
    ``*_enabled`` flags + ``skill_menu_names`` — mounting IS enablement.
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
        )

    return factory
