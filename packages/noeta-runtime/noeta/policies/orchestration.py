"""Interpreter Policy for model-written orchestration scripts ("workflow").

One workflow run = **one Task + one Policy that interprets an orchestration script**:

* The main agent calls the control tool ``run_workflow(script=...)`` → (``_control_translate``)
  translates it into ``SpawnSubtaskDecision(agent_name=WORKFLOW_AGENT_NAME, inputs={script,args})``,
  same family as ``spawn_subagent`` and sharing its plumbing (D5).
* The spawned subtask is not bound to a catalog agent but to :class:`OrchestrationPolicy`
  (the host's ``_build_child_engine`` builds it when it sees the reserved name
  :data:`WORKFLOW_AGENT_NAME`; script/args are read from the subtask's
  ``TaskCreated.inputs`` — deterministic and resumable). It is exactly "a Policy
  interpreting a Task whose body is an orchestration script": **no new
  Workflow-named runtime primitive class is added** (red line, guarded by ``test_lint_naming``).
* Each ``agent(goal)`` in the script spawns a **real Subtask** (its own EventLog;
  inspect + fold/resume apply automatically, D2).

**Pause/resume = re-run from scratch + EventLog as the journal (D3).** The Policy
does not freeze a coroutine; every :meth:`OrchestrationPolicy.decide` **re-runs the
script from line one**. Each ``agent()`` return value maps positionally (by execution
order) to this subtask's recorded subtask results (folded out of the ``tool_result``
entries in ``view.rolling_history``):

* If the Nth ``agent()`` call already has a result → resume the "recording" and return it instantly;
* The first ``agent()`` call without a result → raise :class:`_WorkflowSuspend` carrying a
  ``SpawnSubtaskDecision`` (with a synthetic assistant_message shaped like ``spawn_subagent``
  + a **deterministic call_id** ``wf-<i>``); the engine spawns the subtask and suspends;
* Script runs to completion → ``FinishDecision(answer=script return value)``.

Determinism is a hard constraint (D3 re-run-from-scratch requires same input, same
order — a resume re-runs the script and must map each ``agent()`` call back to the
same recorded result): call_id is derived from execution order, and the Policy
reads no clock/random/EventLog (only ``ctx`` + ``view``). The script's own determinism
guard (controlled namespace + AST ban on non-determinism) lives in issue 03.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from typing import Any, Optional

from noeta.policies._workflow_sandbox import SAFE_BUILTINS
from noeta.policies.control_semantics import _concurrent_fanout_enabled
from noeta.policies.control_tools import STRUCTURED_OUTPUT_TOOL, WORKFLOW_AGENT_NAME
from noeta.policies.react import SPAWN_SUBAGENT_TOOL
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


#: Fixed system prompt for an orchestration subtask (model-independent). OrchestrationPolicy
#: does not read it (the script comes from inputs), but the subtask engine still composes a
#: View; a fixed string keeps this subtask's ContextPlanComposed bytes stable. Shared by both
#: hosts — runner (session.py) and server (SdkHost) — to avoid two strings drifting apart.
WORKFLOW_SYSTEM_PROMPT = "Workflow orchestration runtime."


#: Max nudge count: when the assistant end_turns without calling
#: ``structured_output``, prompt it at most twice in the conversation; still no call after two → fail it.
MAX_STRUCTURED_OUTPUT_NUDGES = 2

#: Nudge text (deterministic constant). Also serves as the "how many nudges so far" counter —
#: count the user messages in ``view.rolling_history`` containing this text, so the Policy
#: stores no state (it only reads view; rebuild = recompute, deterministic across re-runs).
STRUCTURED_OUTPUT_NUDGE = (
    "You must call the structured_output tool with your final answer matching "
    "the required JSON schema — plain text is not accepted."
)


#: For the subtask spawned by the i-th ``agent()`` call in the script, the deterministic
#: call_id of its spawn tool_use is ``wf-<i>``. i is the execution order (positional cursor),
#: keeping the re-run mapping stable across a resume;
#: it is also the anchor drain uses to pair tool_result with its spawning call.
WORKFLOW_CALL_PREFIX = "wf-"

#: Default sub-agent name for ``agent()`` when the script does not specify ``agent=`` explicitly.
_DEFAULT_AGENT = "general-purpose"

#: Name of the wrapper function the script is wrapped in, so it can use ``return`` to hand back its final answer.
_ENTRY_NAME = "__noeta_workflow__"

#: fan-out v2 concurrency judgment is shared with the SR2 ``spawn_subagent``
#: fan-out and lives in ``control_semantics`` (the lowest common point that
#: avoids a ``control_semantics → orchestration`` import cycle) —
#: ``_concurrent_fanout_enabled`` is imported above. Default ON; set
#: ``NOETA_SUBTASK_CONCURRENCY`` to ``0``/``false``/``off``/``no`` to force
#: sequential drain.


class _WorkflowSuspend(BaseException):
    """Raised by the script host when "the next ``agent()`` has no recorded result yet",
    carrying the ``SpawnSubtaskDecision`` to hand to the engine. :meth:`OrchestrationPolicy.decide`
    catches it and returns it as-is — the engine then spawns the real subtask and suspends
    this (orchestration) task on ``SubtaskCompleted``.

    **Subclasses ``BaseException``, not ``Exception``**: suspension is control flow, not an
    error. When the script uses ``try/except Exception`` to catch a failed helper (see
    :class:`_WorkflowAgentError`), it must never swallow the "spawn a subtask" suspension too —
    that would leave the subtask never spawned and the workflow running empty."""

    def __init__(self, decision: Decision) -> None:
        self.decision = decision
        super().__init__("workflow suspended on a pending agent() call")


class _WorkflowAgentError(Exception):
    """A helper subtask the script depends on **terminated in failure**. Raised from
    ``agent()``/``parallel()`` (rather than silently returning ``""``) to make failure loud:

    * Script does not catch it → bubbles to :meth:`OrchestrationPolicy.decide` → the whole
      workflow ``FailDecision`` (the main agent clearly sees "workflow halted: sub-agent … failed: <reason>");
    * The script may ``try/except`` it to tolerate a failed helper (``_WorkflowSuspend`` is a
      ``BaseException`` precisely so such an ``except Exception`` does not swallow a suspension).

    The message is deterministic (execution-order index + agent name + subtask failure ``reason``) → re-derives identically on resume."""

    def __init__(self, *, index: int, agent_name: str, reason: Optional[str]) -> None:
        self.index = index
        self.agent_name = agent_name
        self.reason = reason or "failed"
        super().__init__(f"sub-agent #{index} ({agent_name!r}) failed: {self.reason}")


def _spawn_assistant_message(call_id: str, *, agent_name: str, goal: str) -> Message:
    """Synthesize an assistant turn shaped like ``spawn_subagent``.

    The engine records it as this turn's assistant message (``MessagesAppended``); drain uses it
    (``_pending_spawn_call_id`` scans for the ``spawn_subagent`` tool_use) to pair the subtask
    result back to this call. The call_id is deterministic (``wf-<i>``), so live and resume write the same bytes.
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
    """Synthesize one assistant turn holding N ``spawn_subagent`` tool_uses (in member order).

    Matches ReActPolicy's ≥2-spawn fan-out shape: drain's ``_pending_spawn_call_ids`` pairs the
    group results positionally to these N tool_uses (= spawn order, B2);
    ``_resume_parent`` uses ``append_subagent_group_result_messages`` to render N paired
    ``tool_result`` entries (call_id = ``wf-<i>``). Deterministic call_id ⇒ live/resume byte-identical.
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
    """Normalize one ``parallel`` member into ``(goal, agent)``.

    A member may be a string (goal, with the default agent), a ``{"goal":..., "agent":...}``
    dict, or a ``(goal, agent)`` pair."""
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
    """The orchestration API injected into the script namespace (``agent`` / ``log`` / ``args``).

    Stateless "re-run from scratch": each :meth:`OrchestrationPolicy.decide` builds a fresh host
    carrying this subtask's **recorded** subtask results (keyed by execution order). Each
    ``agent()`` call in the script either hits a recorded result and returns instantly, or triggers a suspension.
    """

    #: Execution order → recorded subtask result block (a ToolResultBlock folded out of
    #: view.rolling_history, holding output/success/error — a failed helper raises on success=False, see ``_recorded``).
    prior_results: dict[int, ToolResultBlock]
    #: The ``args`` visible to the script (from ``run_workflow(args=...)``).
    args: dict[str, Any]
    #: Execution-order cursor for the next ``agent()`` call.
    _cursor: int = 0

    def _recorded(self, index: int, agent_name: str) -> Any:
        """Fetch the recorded result for call site ``index``: success → return ``output``;
        **failure → raise :class:`_WorkflowAgentError`** (not a silent ``""``), making failure loud."""
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
        """Spawn a subtask to run ``goal``, wait for it to finish, and return its output.

        Implemented as "re-run from scratch + instant resume": if this call site (by execution
        order) already has a recorded result, return it directly; otherwise raise
        :class:`_WorkflowSuspend`, and the engine spawns the real subtask and suspends this task,
        so the next re-run finds a result at this call site.

        ``schema``: passing a JSON Schema injects the
        ``structured_output`` tool + nudge into that subtask (the host wires it from
        ``inputs.output_schema``); the return value is that subtask's ``structured_output`` call arguments (an object conforming to the schema).
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
        """Fan out a batch of helpers at once (group barrier),
        wait for all to terminate, and return results in **spawn order**.

        ``items`` is a list of members (each: a goal string / ``{"goal","agent"}`` / ``(goal, agent)``).
        Still implemented as "re-run from scratch + instant resume": this call site claims a
        **contiguous** run of execution-order cursors (one ``wf-<i>`` per member); if the whole
        group already has recorded results, return them in order; otherwise raise
        :class:`_WorkflowSuspend` carrying a ``SpawnSubtasksDecision`` (an N-way group, reusing
        the all-of barrier + result rebuild in spawn order). An empty list returns ``[]``.

        Members run with **wall-clock concurrency by default**: the decision is marked
        ``concurrent=True`` and the live drain fans the members onto the bounded
        executor. Set ``NOETA_SUBTASK_CONCURRENCY`` to ``0``/``false``/``off``/``no``
        to force the legacy sequential drain — then the group's
        ``SubtaskGroupCompleted`` carries no ``concurrent`` field (conditional
        folding), **byte-identical to a pre-v2 recording**. Either way results
        are rebuilt in spawn order (keyed), and resume drains deterministically
        (it re-injects recorded observer events, never the live executor).
        """
        specs_raw = [_normalize_parallel_item(it, str(agent)) for it in items]
        start = self._cursor
        self._cursor += len(specs_raw)
        if not specs_raw:
            return []
        indices = list(range(start, start + len(specs_raw)))
        if all(i in self.prior_results for i in indices):
            # Whole group recorded: return in spawn order; any failed member → raise (loud).
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
                # Transient ``bool`` carrier only — the decision is not folded.
                # The Engine's ``handle_spawn_subtasks`` does the ``or None``
                # conditional fold onto the persisted ``SubtaskGroupCompleted``,
                # so a disabled rollout (``False``) stays byte-identical to a
                # pre-v2 sequential recording.
                concurrent=_concurrent_fanout_enabled(),
            )
        )

    def log(self, message: Any) -> None:
        """Script progress-log hook (v1 no-op: stays purely deterministic, no side effects/events).

        Kept as API shape (the script can call ``log(...)`` without error); when this later lands
        as visible progress it will go through the Decision channel — never direct IO inside the
        Policy (determinism red line)."""
        return None


def _collect_prior_results(view: View) -> dict[int, ToolResultBlock]:
    """Fold the recorded ``agent()`` result blocks out of this (orchestration) subtask's ``rolling_history``.

    When the subtask spawned by each ``agent()`` finishes, drain writes a paired ``tool_result``
    back to the parent (= this orchestration task) stream (call_id = ``wf-<i>``, see
    ``subtask_drain._resume_parent``). Here we collect those ``ToolResultBlock`` entries into
    ``{i: block}`` by the execution order i in the call_id — keeping ``success``/``error`` and not
    just ``output``, so a failed helper can raise in ``_recorded`` instead of silently becoming
    ``""``. **Reads view only** (Policy contract: never touch the EventLog).
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
    """Compile and execute the orchestration script, returning its ``return`` value (no return → ``None``).

    The script is wrapped in a function (so a top-level ``return`` is legal) and ``exec``'d in a
    namespace containing only the orchestration API. The wrap operates on the **parsed AST**, not
    the source text: splicing the script's already-parsed statements into a synthetic function
    body preserves every literal exactly as written. An earlier ``textwrap.indent``-based
    string wrap prepended 4 spaces to every physical line, which silently corrupted any
    multi-line (e.g. triple-quoted) string literal in the script — its interior lines gained the
    indentation as part of the string's value. The determinism guard (the "do not inject" side of
    the controlled namespace + AST ban on non-determinism) belongs to issue 03 and runs on the
    ORIGINAL script text before this ever executes; this function only does "wrap + run + grab
    return value".
    """
    tree = ast.parse(script, filename="<workflow>", mode="exec")
    # Parse a trivial wrapper for its FunctionDef shape, then swap in the script's own
    # (already-parsed, unmodified) statements as the body — no source text is re-indented or
    # re-serialized, so every literal in ``script`` reaches the namespace byte-for-byte.
    wrapper = ast.parse(f"def {_ENTRY_NAME}(): pass", filename="<workflow>", mode="exec")
    entry = wrapper.body[0]
    assert isinstance(entry, ast.FunctionDef)
    if tree.body:
        entry.body = tree.body
    ast.fix_missing_locations(wrapper)
    code = compile(wrapper, "<workflow>", "exec")
    # Controlled namespace (D4): inject only the orchestration API + a safe-builtins allowlist
    # (no import/open/eval/__import__), so even if the AST guard misses something, runtime cannot reach time/random/os.
    namespace: dict[str, Any] = {
        "__builtins__": SAFE_BUILTINS,
        "agent": host.agent,
        "parallel": host.parallel,
        "log": host.log,
        "args": host.args,
    }
    exec(code, namespace)  # noqa: S102 — D4: exec'ing model-written scripts adds no new attack surface
    return namespace[_ENTRY_NAME]()


@dataclass
class OrchestrationPolicy:
    """Orchestration-script interpreter — a plain :class:`~noeta.protocols.policy.Policy`
    (``decide(ctx, view) -> Decision``), not a new runtime primitive.

    Each ``decide``: fold the recorded ``agent()`` results out of ``view`` → re-run the script
    from scratch → either raise a suspension (spawn the next subtask) or the script completes
    (``FinishDecision``). Script and args are injected at construction (the host reads them from
    the subtask's ``TaskCreated.inputs``); the whole thing is deterministic (same view, same order) → re-derives identically on resume.
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
            # A helper the script depends on failed, and the script did not try/except to tolerate
            # it → the whole workflow fails loudly (deterministic message: execution order + agent +
            # subtask failure reason), rather than folding an empty skeleton back as a result and
            # leaving the main agent unable to tell success from failure.
            return FailDecision(reason=f"workflow halted: {exc}", retryable=False)
        except Exception as exc:  # noqa: BLE001 — see below
            # The model-written script failed to compile/run → **gracefully fail** this workflow
            # task (non-retryable), rather than letting the exception bubble up and crash
            # drain/the whole run. The error text is deterministic (``<workflow>`` filename + line
            # number + exception type), so the re-run mapping is unaffected. The script's
            # compile-time determinism guard (AST ban on non-determinism, pointing clearly at the
            # offending site) is issue 03, which moves such failures forward into translation.
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
    """The assistant text recorded when orchestration completes (deterministic projection: ``str(answer)``)."""
    return answer if isinstance(answer, str) else repr(answer)


def _count_structured_output_nudges(view: View) -> int:
    """Count the structured_output nudges already issued in ``view.rolling_history``.

    A nudge is a ``role="user"`` message whose text contains :data:`STRUCTURED_OUTPUT_NUDGE`.
    Deriving the count from the conversation (rather than storing it on the Policy instance) keeps
    the Policy stateless — rebuild = recompute, deterministic across re-runs.
    """
    count = 0
    for msg in view.rolling_history:
        if msg.role != "user":
            continue
        for block in msg.content:
            if isinstance(block, TextBlock) and STRUCTURED_OUTPUT_NUDGE in block.text:
                count += 1
                break
    return count


@dataclass
class StructuredOutputPolicy:
    """A "structured receipt" decorator wrapping an assistant subtask's inner Policy.

    Used by an assistant from ``agent(goal, schema=...)``: the ``structured_output`` control schema
    has already been sent into that assistant's ``provider_tool_schemas`` via the composer (the
    host wires it from ``inputs.output_schema``). This wrapper intercepts inner decisions:

    * inner emits a ``ToolCallsDecision`` containing a ``structured_output`` call → treat that call's
      **arguments** as the assistant's final answer and turn it into a ``FinishDecision`` (the tool
      never reaches ToolRuntime, so it is intercepted before execution);
    * inner wants a ``FinishDecision`` (the assistant end_turns without calling ``structured_output``)
      → if nudged <2 times, return a loop-continue ``StatePatchDecision`` (record this end_turn +
      append a user nudge) to make it decide again; still no call after 2 → ``FailDecision`` (D6: fail the assistant after two);
    * everything else (plain tool_calls / non-finish) → pass through unchanged.

    No state of its own: the nudge count is derived from view, so live and resume decide alike (deterministic).
    """

    inner: Policy
    schema: dict

    def decide(self, ctx: StepContext, view: View) -> Decision:
        decision = self.inner.decide(ctx, view)
        if isinstance(decision, ToolCallsDecision):
            for call in decision.calls:
                if call.tool_name == STRUCTURED_OUTPUT_TOOL:
                    # The call arguments are the structured answer; carry that assistant turn
                    # (with the tool_use) to record before TaskCompleted. The subtask terminates
                    # here with no follow-on request, so leaving the tool_use without a paired
                    # result is harmless (no "dangling function_call → gateway 400" — that only
                    # happens on a continuation request).
                    return FinishDecision(
                        answer=dict(call.arguments),
                        assistant_message=decision.assistant_message,
                    )
            return decision
        if isinstance(decision, FinishDecision):
            nudges = _count_structured_output_nudges(view)
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
