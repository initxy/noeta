"""``react`` built-in — implementation package (microkernel phase 2b).

The official decision-mapping policy: :class:`ReActPolicy` (``react``) and the
workflow interpreter :class:`OrchestrationPolicy` + ``StructuredOutputPolicy``
+ ``WORKFLOW_SYSTEM_PROMPT`` (``orchestration``). The kernel keeps the control
band (``noeta.policies.control_tools`` / ``control_semantics`` /
``_workflow_sandbox`` — the control-tool schemas, translation, and the
workflow-script validation sandbox, per phase-1 D3) and the generic multi-turn
wrapper (``noeta.execution.multi_turn``); nothing imports this package
statically —
the SDK resolves :func:`build_react_policy_factory` through
``noeta.client.parts`` at client build.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from noeta.protocols.content_store import ContentStore
from noeta.protocols.policy import Policy
from noeta.protocols.tool import Tool

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
    "StructuredOutputPolicy",
    "WORKFLOW_SYSTEM_PROMPT",
    "build_react_policy_factory",
    "enforce_verbatim_constraints",
    "extract_safety_constraints",
]


def build_react_policy_factory(
    *,
    tools: dict[str, Tool],
    system_prompt: str,
    model: str,
    max_steps: int,
    delegation_enabled: bool,
    todo_write_enabled: bool,
    ask_user_question_enabled: bool,
    skill_invocation_enabled: bool,
    workflow_enabled: bool,
    skill_menu_names: frozenset[str],
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
    """

    def factory(llm: Any) -> Policy:
        return ReActPolicy(
            llm=llm,
            tools=tools,
            system_prompt=system_prompt,
            model=model,
            max_steps=max_steps,
            delegation_enabled=delegation_enabled,
            todo_write_enabled=todo_write_enabled,
            ask_user_question_enabled=ask_user_question_enabled,
            skill_invocation_enabled=skill_invocation_enabled,
            workflow_enabled=workflow_enabled,
            skill_menu_names=skill_menu_names,
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
