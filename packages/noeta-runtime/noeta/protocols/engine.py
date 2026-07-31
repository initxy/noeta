"""Engine Protocol — the seam between execution hosts and the kernel Engine.

Execution hosts (``noeta.execution``) and product-layer drivers reach the kernel
through this Protocol rather than the concrete :class:`noeta.core.engine.Engine`,
so an alternative implementation — a fake, a resume adapter — slots into the same
plumbing without subclassing. The surface is deliberately a strict subset of the
concrete Engine's public API: only what a host actually calls belongs here.
"""

from __future__ import annotations

from typing import Any, Callable, Optional, Protocol, runtime_checkable

from noeta.protocols.decisions import TaskStatePatch
from noeta.protocols.events import TaskHostBoundPayload
from noeta.protocols.messages import Block, MessageOrigin
from noeta.protocols.task import Task


__all__ = [
    "EngineProtocol",
]


@runtime_checkable
class EngineProtocol(Protocol):
    """Public Engine surface used by execution hosts.

    Implementations drive a single :class:`Task` toward suspend or terminal
    through a compose → decide loop. Hosts call these methods in a strict
    lifecycle:

    1. ``create_task`` appends genesis events and returns the in-memory Task;
    2. Optionally ``append_user_message`` seeds user turns;
       ``apply_state_patch`` applies operator-driven mutations (e.g. skill
       activation); ``note_model_bound`` / ``note_conversation_*`` log
       durable bindings;
    3. ``run_one_step`` advances the compose → decide loop to the next suspend
       or terminal;
    4. On resume (dispatcher-woken tasks): ``note_woken`` → optional
       prelude (``resolve_tool_approval`` / ``answer_user_question`` /
       ``append_user_message``) → ``run_one_step``;
    5. For delegated subtasks: ``append_subagent_result_message`` /
       ``append_subagent_group_result_messages`` render child results as
       paired ``tool_result`` blocks before the next ``run_one_step``.

    All methods take and return the in-memory ``Task``; the Engine is the
    single writer, so a caller MUST NOT mutate ``task`` fields outside these
    seams.
    """

    def create_task(
        self,
        *,
        goal: str,
        policy_name: str,
        agent_name: str = "unnamed",
        parent_task_id: Optional[str] = None,
        inputs: Optional[dict[str, Any]] = None,
        task_id: Optional[str] = None,
        trace_id: Optional[str] = None,
        host_binding: Optional[TaskHostBoundPayload] = None,
    ) -> Task: ...

    def append_user_message(
        self,
        task: Task,
        *,
        content: list[Block],
        lease_id: str,
        trace_id: Optional[str] = None,
        origin: Optional[MessageOrigin] = None,
    ) -> Task: ...

    def append_subagent_result_message(
        self,
        task: Task,
        *,
        call_id: str,
        output: Any,
        success: bool,
        lease_id: str,
        error: Optional[str] = None,
        trace_id: Optional[str] = None,
    ) -> Task: ...

    def append_subagent_group_result_messages(
        self,
        task: Task,
        wake_event: Any,
        call_ids: list[str],
        *,
        lease_id: str,
        trace_id: Optional[str] = None,
    ) -> Task: ...

    def apply_state_patch(
        self,
        task: Task,
        *,
        patch: TaskStatePatch,
        lease_id: str,
        trace_id: Optional[str] = None,
    ) -> Task: ...

    def resolve_tool_approval(
        self,
        task: Task,
        *,
        call_id: str,
        approved: bool,
        reason: Optional[str] = None,
        resolver: Optional[str] = None,
        lease_id: str,
        trace_id: Optional[str] = None,
    ) -> Task: ...

    def answer_user_question(
        self,
        task: Task,
        *,
        question_id: str,
        answers: dict[str, dict[str, Any]],
        answered_by: Optional[str] = None,
        lease_id: str,
        trace_id: Optional[str] = None,
    ) -> Task: ...

    def note_woken(
        self, task: Task, *, lease_id: str, wake_event: Any
    ) -> Task: ...

    def note_model_bound(
        self,
        task: Task,
        *,
        lease_id: str,
        model: str,
        principal_identity: str,
        provider: Optional[str] = None,
    ) -> Task: ...

    def note_conversation_closed(
        self,
        task: Task,
        *,
        closed_by: str,
        reason: Optional[str] = None,
        trace_id: Optional[str] = None,
    ) -> Task: ...

    def note_conversation_reopened(
        self,
        task: Task,
        *,
        reopened_by: str,
        reason: Optional[str] = None,
        trace_id: Optional[str] = None,
    ) -> Task: ...

    def run_one_step(
        self,
        task: Task,
        *,
        lease_id: str,
        cancelled: Optional[Callable[[], bool]] = None,
    ) -> Task: ...
