"""Interpreter Policy for model-written orchestration scripts.

One workflow run is one Task plus a Policy that interprets a script: each
``agent()`` call in the script spawns a real Subtask, and the script's ``return``
value becomes the workflow's answer. Nothing freezes a coroutine — every
:meth:`OrchestrationPolicy.decide` **re-runs the script from line one** and maps
each ``agent()`` call positionally onto the results already recorded in this
task's history. Determinism is therefore a hard constraint: call ids are derived
from execution order (``wf-<i>``) and the Policy reads only ``ctx`` and ``view``,
never a clock, randomness, or the EventLog.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from typing import Any, Optional

from noeta.policies.control_semantics import (
    SPAWN_SUBAGENT_TOOL,
    WORKFLOW_AGENT_NAME,
    ack_patch_decision,
    concurrent_fanout_enabled,
)

from .control_tool import STRUCTURED_OUTPUT_TOOL
from .schema_check import validate_structured_payload
from .workflow_sandbox import SAFE_BUILTINS
from noeta.protocols.decisions import (
    Decision,
    FailDecision,
    FinishDecision,
    SpawnSubtaskDecision,
    SpawnSubtaskSpec,
    SpawnSubtasksDecision,
    StatePatchDecision,
    ToolCallsDecision,
)
from noeta.protocols.messages import Message, TextBlock, ToolResultBlock, ToolUseBlock
from noeta.protocols.policy import Policy
from noeta.protocols.step_context import StepContext
from noeta.protocols.view import View


__all__ = [
    "WORKFLOW_AGENT_NAME",
    "WORKFLOW_CALL_PREFIX",
    "WORKFLOW_SYSTEM_PROMPT",
    "OrchestrationPolicy",
    "StructuredOutputPolicy",
    "STRUCTURED_OUTPUT_NUDGE",
    "MAX_STRUCTURED_OUTPUT_NUDGES",
]


#: ``OrchestrationPolicy`` never reads this (the script comes from ``inputs``),
#: but the subtask engine still composes a View, and a fixed string keeps the
#: subtask's ``ContextPlanComposed`` bytes stable across resume.
WORKFLOW_SYSTEM_PROMPT = "Workflow orchestration runtime."


#: How many times an assistant may miss ``structured_output`` — by end_turning
#: without calling it, or by calling it with an off-schema payload — before the
#: subtask is failed.
MAX_STRUCTURED_OUTPUT_NUDGES = 2

#: Doubles as the nudge counter: the retry budget is derived by counting user
#: messages in ``view.rolling_history`` carrying this text, so the Policy stores
#: no state of its own and recomputes identically on a re-run.
STRUCTURED_OUTPUT_NUDGE = (
    "You must call the structured_output tool with your final answer matching "
    "the required JSON schema — plain text is not accepted."
)


#: The subtask spawned by the i-th ``agent()`` call gets call_id ``wf-<i>``,
#: where i is the execution-order cursor: deriving it from position is what keeps
#: the re-run mapping stable across a resume, and it is also the anchor drain
#: uses to pair a ``tool_result`` back to its spawning call.
WORKFLOW_CALL_PREFIX = "wf-"

_DEFAULT_AGENT = "general-purpose"

#: The script is spliced into a function of this name so a top-level ``return``
#: is legal and can hand back the workflow's answer.
_ENTRY_NAME = "__noeta_workflow__"


class _WorkflowSuspend(BaseException):
    """Raised when the next ``agent()`` call has no recorded result yet, carrying
    the ``SpawnSubtaskDecision`` the engine should act on.

    **Subclasses ``BaseException``, not ``Exception``**: suspension is control
    flow, not an error. A script that wraps a helper in ``try/except Exception``
    (see :class:`_WorkflowAgentError`) must not swallow the suspension too — that
    would leave the subtask never spawned and the workflow running empty."""

    def __init__(self, decision: Decision) -> None:
        self.decision = decision
        super().__init__("workflow suspended on a pending agent() call")


class _WorkflowAgentError(Exception):
    """A helper subtask the script depends on terminated in failure.

    Raised from ``agent()`` / ``parallel()`` rather than silently returning ``""``
    so failure is loud: uncaught it fails the whole workflow, and a script that
    wants to tolerate a dead helper must say so with ``try/except``. The message
    is built from execution-order index, agent name and the subtask's failure
    reason, so it re-derives identically on resume."""

    def __init__(self, *, index: int, agent_name: str, reason: Optional[str]) -> None:
        self.index = index
        self.agent_name = agent_name
        self.reason = reason or "failed"
        super().__init__(f"sub-agent #{index} ({agent_name!r}) failed: {self.reason}")


def _spawn_assistant_message(call_id: str, *, agent_name: str, goal: str) -> Message:
    """Synthesize an assistant turn shaped like ``spawn_subagent``.

    Drain scans the recorded turn for a ``spawn_subagent`` tool_use to pair the
    subtask result back to this call; the deterministic ``wf-<i>`` call_id is what
    makes live and resume write the same bytes.
    """
    return Message(
        role="assistant",
        content=[
            ToolUseBlock(
                call_id=call_id,
                tool_name=SPAWN_SUBAGENT_TOOL,
                arguments={"agent": agent_name, "goal": goal},
            )
        ],
    )


def _group_spawn_assistant_message(
    specs: tuple[SpawnSubtaskSpec, ...],
) -> Message:
    """Synthesize one assistant turn holding N ``spawn_subagent`` tool_uses.

    Matches ReActPolicy's fan-out shape: drain pairs the group's results
    positionally to these N tool_uses, so member order IS spawn order, and the
    deterministic ``wf-<i>`` call_ids keep live and resume byte-identical.
    """
    return Message(
        role="assistant",
        content=[
            ToolUseBlock(
                call_id=spec.call_id,
                tool_name=SPAWN_SUBAGENT_TOOL,
                arguments={"agent": spec.agent_name, "goal": spec.goal},
            )
            for spec in specs
        ],
    )


def _normalize_parallel_item(item: Any, default_agent: str) -> tuple[str, str]:
    if isinstance(item, str):
        return item, default_agent
    if isinstance(item, dict):
        return str(item.get("goal", "")), str(item.get("agent", default_agent))
    if isinstance(item, (tuple, list)) and len(item) == 2:
        return str(item[0]), str(item[1])
    raise TypeError(
        "parallel() items must be a goal string, a {'goal','agent'} dict, "
        f"or a (goal, agent) pair; got {type(item).__name__}"
    )


@dataclass
class _ScriptHost:
    """The orchestration API injected into the script namespace.

    Carries no state between decisions: each :meth:`OrchestrationPolicy.decide`
    builds a fresh host holding this subtask's already-recorded results, so an
    ``agent()`` call either hits a recorded result and returns instantly or
    triggers a suspension.
    """

    #: Execution order → the recorded result block, kept whole (output, success,
    #: error) so ``_recorded`` can raise on a failed helper.
    prior_results: dict[int, ToolResultBlock]
    args: dict[str, Any]
    _cursor: int = 0

    def _recorded(self, index: int, agent_name: str) -> Any:
        """Return the recorded output for call site ``index``, or raise
        :class:`_WorkflowAgentError` — never a silent ``""``."""
        block = self.prior_results[index]
        if not block.success:
            raise _WorkflowAgentError(
                index=index, agent_name=agent_name, reason=block.error
            )
        return block.output

    def agent(
        self,
        goal: str,
        *,
        agent: str = _DEFAULT_AGENT,
        schema: Optional[dict] = None,
    ) -> Any:
        """Spawn a subtask to run ``goal``, wait for it, and return its output.

        A call site that already has a recorded result returns it directly;
        otherwise this raises :class:`_WorkflowSuspend` so the engine spawns the
        real subtask and suspends, and the next re-run finds a result here.

        Passing ``schema`` injects the ``structured_output`` tool into that
        subtask (the host wires it from ``inputs.output_schema``), and the return
        value becomes the arguments of its ``structured_output`` call.
        """
        index = self._cursor
        self._cursor += 1
        if index in self.prior_results:
            return self._recorded(index, str(agent))
        call_id = f"{WORKFLOW_CALL_PREFIX}{index}"
        inputs = {"output_schema": dict(schema)} if schema is not None else {}
        raise _WorkflowSuspend(
            SpawnSubtaskDecision(
                agent_name=str(agent),
                goal=str(goal),
                inputs=inputs,
                assistant_message=_spawn_assistant_message(
                    call_id, agent_name=str(agent), goal=str(goal)
                ),
            )
        )

    def parallel(self, items: Any, *, agent: str = _DEFAULT_AGENT) -> list:
        """Fan out a batch of helpers behind one barrier and return their results
        in **spawn order**.

        Each member is a goal string, a ``{"goal","agent"}`` dict, or a
        ``(goal, agent)`` pair. The call site claims a **contiguous** run of
        execution-order cursors (one ``wf-<i>`` per member) so the re-run mapping
        stays positional.

        Members run with wall-clock concurrency unless
        ``NOETA_SUBTASK_CONCURRENCY`` is ``0``/``false``/``off``/``no``; either
        way results are rebuilt in spawn order, and a resume drains from recorded
        observer events rather than the live executor.
        """
        specs_raw = [_normalize_parallel_item(it, str(agent)) for it in items]
        start = self._cursor
        self._cursor += len(specs_raw)
        if not specs_raw:
            return []
        indices = list(range(start, start + len(specs_raw)))
        if all(i in self.prior_results for i in indices):
            return [self._recorded(i, a) for i, (g, a) in zip(indices, specs_raw)]
        specs = tuple(
            SpawnSubtaskSpec(
                agent_name=a,
                goal=g,
                call_id=f"{WORKFLOW_CALL_PREFIX}{i}",
            )
            for i, (g, a) in zip(indices, specs_raw)
        )
        raise _WorkflowSuspend(
            SpawnSubtasksDecision(
                specs=specs,
                assistant_message=_group_spawn_assistant_message(specs),
                # Transient carrier only — the decision itself is never folded;
                # the Engine folds this conditionally onto the persisted
                # ``SubtaskGroupCompleted`` so a sequential run records no
                # ``concurrent`` field at all.
                concurrent=concurrent_fanout_enabled(),
            )
        )

    def log(self, message: Any) -> None:
        """Script progress-log hook.

        A no-op: the Policy must stay side-effect free, so surfacing progress
        would have to travel the Decision channel, never direct IO from here."""
        return None


def _collect_prior_results(view: View) -> dict[int, ToolResultBlock]:
    """Fold the recorded ``agent()`` results out of ``rolling_history`` into
    ``{execution order: block}``, keyed by the index in the ``wf-<i>`` call_id.

    Blocks are kept whole rather than reduced to ``output`` so ``_recorded`` can
    raise on a failed helper. Reads ``view`` only — the Policy contract forbids
    touching the EventLog.
    """
    results: dict[int, ToolResultBlock] = {}
    for msg in view.rolling_history:
        if msg.role != "tool":
            continue
        for block in msg.content:
            if not isinstance(block, ToolResultBlock):
                continue
            cid = block.call_id
            if not cid.startswith(WORKFLOW_CALL_PREFIX):
                continue
            suffix = cid[len(WORKFLOW_CALL_PREFIX):]
            if not suffix.isdigit():
                continue
            results[int(suffix)] = block
    return results


def _run_script(script: str, host: _ScriptHost) -> Any:
    """Wrap, execute, and return the script's ``return`` value.

    The wrap operates on the **parsed AST**, splicing the script's own statements
    into a synthetic function body. Wrapping the source text instead — indenting
    every physical line — silently corrupts multi-line string literals, whose
    interior lines would absorb the indentation into their value.
    """
    tree = ast.parse(script, filename="<workflow>", mode="exec")
    wrapper = ast.parse(f"def {_ENTRY_NAME}(): pass", filename="<workflow>", mode="exec")
    entry = wrapper.body[0]
    assert isinstance(entry, ast.FunctionDef)
    if tree.body:
        entry.body = tree.body
    ast.fix_missing_locations(wrapper)
    code = compile(wrapper, "<workflow>", "exec")
    # Controlled namespace: only the orchestration API plus the safe-builtins
    # allowlist, so a non-deterministic source the AST guard missed is still
    # unreachable at runtime.
    namespace: dict[str, Any] = {
        "__builtins__": SAFE_BUILTINS,
        "agent": host.agent,
        "parallel": host.parallel,
        "log": host.log,
        "args": host.args,
    }
    exec(code, namespace)  # noqa: S102 — the goal is determinism, not sandboxing: the model already holds shell/file tools
    return namespace[_ENTRY_NAME]()


@dataclass
class OrchestrationPolicy:
    """Orchestration-script interpreter — a plain Policy, not a new runtime
    primitive.

    Each ``decide`` folds the recorded ``agent()`` results out of ``view``, re-runs
    the script from scratch, and either suspends on the next unspawned call or
    finishes. Script and args are injected at construction (the host reads them
    from the subtask's ``TaskCreated.inputs``), so the same view always yields the
    same decision and a resume re-derives it identically.
    """

    script: str
    args: dict[str, Any]

    def decide(self, ctx: StepContext, view: View) -> Decision:
        prior = _collect_prior_results(view)
        host = _ScriptHost(prior_results=prior, args=dict(self.args))
        try:
            answer = _run_script(self.script, host)
        except _WorkflowSuspend as suspend:
            return suspend.decision
        except _WorkflowAgentError as exc:
            # The script did not try/except a helper it depends on, so the whole
            # workflow fails loudly rather than folding an empty result back and
            # leaving the parent unable to tell success from failure.
            return FailDecision(reason=f"workflow halted: {exc}", retryable=False)
        except Exception as exc:  # noqa: BLE001 — see below
            # A model-written script that fails to compile or run must not crash
            # drain and take the whole run with it. The error text is
            # deterministic (``<workflow>`` filename, line number, exception
            # type), so the re-run mapping is unaffected.
            return FailDecision(
                reason=f"workflow script error: {type(exc).__name__}: {exc}",
                retryable=False,
            )
        return FinishDecision(
            answer=answer,
            assistant_message=Message(
                role="assistant",
                content=[TextBlock(text=_finish_text(answer))],
            ),
        )


def _finish_text(answer: Any) -> str:
    """A deterministic projection of the answer, for the recorded assistant turn."""
    return answer if isinstance(answer, str) else repr(answer)


def _count_structured_output_retries(view: View) -> int:
    """Count the structured-output retries already spent in ``view.rolling_history``.

    Two channels, one budget:

    * a ``role="user"`` message whose text contains :data:`STRUCTURED_OUTPUT_NUDGE` — the
      assistant end_turned instead of calling the tool;
    * an assistant ``structured_output`` ``tool_use`` already in the history — a call whose
      payload was rejected. A payload that *passes* finishes the subtask on the spot, so a
      recorded call can only ever be a rejected one.

    Deriving the count from the conversation (rather than storing it on the Policy instance) keeps
    the Policy stateless — rebuild = recompute, deterministic across re-runs. The rejected-call arm
    reads message *structure* rather than the rejection text on purpose: a large tool result can be
    spilled out of line, and a counter that stopped matching would loop forever.
    """
    count = 0
    for msg in view.rolling_history:
        if msg.role == "user":
            for block in msg.content:
                if isinstance(block, TextBlock) and STRUCTURED_OUTPUT_NUDGE in block.text:
                    count += 1
                    break
        elif msg.role == "assistant":
            for block in msg.content:
                if (
                    isinstance(block, ToolUseBlock)
                    and block.tool_name == STRUCTURED_OUTPUT_TOOL
                ):
                    count += 1
                    break
    return count


def _rejection_text(violations: tuple[str, ...]) -> str:
    """The tool-result body handed back for a payload that missed the schema."""
    listed = "\n".join(f"- {v}" for v in violations)
    return (
        "structured_output rejected: the arguments did not match the required "
        f"JSON schema.\n{listed}\n"
        "Call structured_output again with the same answer, corrected to match "
        "the schema."
    )


def _rejection_fail_reason(violations: tuple[str, ...]) -> str:
    """The terminal reason once the retry budget is spent on invalid payloads."""
    return (
        "structured_output arguments did not match the required JSON schema after "
        f"{MAX_STRUCTURED_OUTPUT_NUDGES} nudges: " + "; ".join(violations)
    )


@dataclass
class StructuredOutputPolicy:
    """A "structured receipt" decorator wrapping an assistant subtask's inner Policy.

    Used by an assistant from ``agent(goal, schema=...)``: the ``structured_output`` control schema
    has already been sent into that assistant's ``provider_tool_schemas`` via the composer (the
    host wires it from ``inputs.output_schema``). This wrapper intercepts inner decisions:

    * inner emits a ``ToolCallsDecision`` containing a ``structured_output`` call → check the
      call's **arguments** against the schema. Clean → they are the assistant's final answer,
      turned into a ``FinishDecision`` (the tool never reaches ToolRuntime, so it is intercepted
      before execution). Not clean → reject the call the way every control tool acks a malformed
      input (assistant tool_use + a failed tool_result naming the violations) and let the
      assistant try again, because the provider's tool schema only *steers* the model: an
      unchecked payload would be handed to the ``agent(goal, schema=...)`` caller as a completed
      task and fail far from its cause;
    * inner wants a ``FinishDecision`` (the assistant end_turns without calling ``structured_output``)
      → if nudged <2 times, return a loop-continue ``StatePatchDecision`` (record this end_turn +
      append a user nudge) to make it decide again; still no call after 2 → ``FailDecision`` (fail the assistant after two);
    * everything else (plain tool_calls / non-finish) → pass through unchanged.

    Both failure modes — no call, and a call that missed the schema — share the one
    :data:`MAX_STRUCTURED_OUTPUT_NUDGES` budget, so a model that alternates between them cannot
    loop for free.

    No state of its own: the retry count is derived from view, so live and resume decide alike
    (deterministic). The schema check is deterministic for the same reason — its violation text
    lands in the recorded conversation.
    """

    inner: Policy
    schema: dict

    def decide(self, ctx: StepContext, view: View) -> Decision:
        decision = self.inner.decide(ctx, view)
        if isinstance(decision, ToolCallsDecision):
            for call in decision.calls:
                if call.tool_name == STRUCTURED_OUTPUT_TOOL:
                    payload = dict(call.arguments)
                    violations = validate_structured_payload(payload, self.schema)
                    if not violations:
                        # The call arguments are the structured answer; carry that assistant turn
                        # (with the tool_use) to record before TaskCompleted. The subtask terminates
                        # here with no follow-on request, so leaving the tool_use without a paired
                        # result is harmless (no "dangling function_call → gateway 400" — that only
                        # happens on a continuation request).
                        return FinishDecision(
                            answer=payload,
                            assistant_message=decision.assistant_message,
                        )
                    if (
                        _count_structured_output_retries(view)
                        >= MAX_STRUCTURED_OUTPUT_NUDGES
                    ):
                        return FailDecision(
                            reason=_rejection_fail_reason(violations),
                            retryable=False,
                        )
                    if decision.assistant_message is not None:
                        # Unlike the finish path above there IS a follow-on request, so the
                        # rejected tool_use must be answered — a dangling function_call is
                        # exactly what a continuation request rejects.
                        return ack_patch_decision(
                            (call,),
                            decision.assistant_message,
                            decision.assistant_thinking,
                            patch=None,
                            text=_rejection_text(violations),
                            valid=False,
                        )
                    # No assistant turn to record ⇒ no tool_use to answer. Fall back to the
                    # plain user nudge, carrying the same marker so the retry still counts.
                    return StatePatchDecision(
                        messages_after=(
                            Message(
                                role="user",
                                content=[
                                    TextBlock(
                                        text=STRUCTURED_OUTPUT_NUDGE
                                        + "\n"
                                        + _rejection_text(violations)
                                    )
                                ],
                            ),
                        ),
                    )
            return decision
        if isinstance(decision, FinishDecision):
            nudges = _count_structured_output_retries(view)
            if nudges < MAX_STRUCTURED_OUTPUT_NUDGES:
                before = (
                    (decision.assistant_message,)
                    if decision.assistant_message is not None
                    else ()
                )
                return StatePatchDecision(
                    messages_before=before,
                    patch=None,
                    messages_after=(
                        Message(
                            role="user",
                            content=[TextBlock(text=STRUCTURED_OUTPUT_NUDGE)],
                        ),
                    ),
                )
            return FailDecision(
                reason=(
                    "structured_output not called after "
                    f"{MAX_STRUCTURED_OUTPUT_NUDGES} nudges"
                ),
                retryable=False,
            )
        return decision
