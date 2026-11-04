"""Closing a turn or the task: stop a turn, fail it, end the task,
continue a reply cut at the output cap, and end the turn after an empty
reply (or go on, when an extension asks). Every way out answers the calls
still open, so the conversation never keeps a call without a result."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING

from noeta.core import records as core_records
from noeta.core.errors import InboxUnread
from noeta.core.events import Cancel, ChildEnded, Event, TaskEnded
from noeta.core.runner import texts
from noeta.core.store import Post
from noeta.core.types import Message, ModelResponse, user

if TYPE_CHECKING:
    from noeta.core.runner.task import TaskRun

log = logging.getLogger("noeta.core.runner")


async def stop_turn(run: TaskRun, call_ids: Sequence[str]) -> None:
    stopped = core_records.TurnStopped()
    if not call_ids and run.state.model_attempts:
        # Stopped while the reply streamed: the streamed text and the
        # reasoning finished before it stay in the conversation, and the
        # marker opens the person's next turn.
        stopped = core_records.TurnStopped(
            partial="".join(run.partial), marker=texts.INTERRUPTED_MARKER,
            thinking=list(run.thinking) or None)
    run.partial, run.thinking = [], []
    await close_calls(run, call_ids, texts.STOPPED_CALL_ERROR, core_records.event(stopped),
                      "the parent's turn was stopped")


async def close_calls(run: TaskRun, call_ids: Sequence[str], error: str, closing: Event,
                      cancel_reason: str, *, started_error: str | None = None) -> None:
    """Answer the open calls (parked ones are withdrawn), cancel the
    children they wait on, and append ``closing`` with the last results,
    so the conversation never keeps a call without a result. A call that
    started gets ``started_error`` when given (its outcome is unknown)."""
    from noeta.core.runner.calls import resume_calls

    parked = [c for c in run.state.calls if c.use.id in call_ids and c.parked]
    children = [k for c in parked if (c.parked or {}).get("on") == "children"
                for k in (c.parked or {}).get("keys") or []]
    if parked:
        await resume_calls(run, [c.use.id for c in parked], withdrawn=True)
    rest = [c for c in run.state.calls if c.use.id in call_ids and not c.parked
            and c.result is None]
    events: list[Event] = [
        run.error_result(c.use.id, started_error if c.started and started_error else error)
        for c in rest]
    events.append(closing)
    await run.append(events, posts=[
        Post(task_id=k, body=Cancel(reason=cancel_reason))
        for k in children if run.state.children.get(k) is None])


async def end(run: TaskRun, status: str, answer: str = "", error: str = "",
              *, decide_again: bool = False) -> bool:
    """End the task, unless its inbox got something it has not taken:
    the end never lands over an unread message (it would otherwise be
    heard by no one). The input is taken first; with
    ``decide_again`` that returns ``False`` so the caller decides anew,
    otherwise (an end that stands whatever arrived) it ends after it."""
    while True:
        # Children die with their parent.
        posts: list[Post] = [Post(task_id=c, body=Cancel(reason=f"parent task {status}"))
                             for c in run.state.live_children()]
        if run.state.parent_id:
            posts.append(Post(
                task_id=run.state.parent_id,
                body=ChildEnded(child_id=run.task_id, status=status,  # type: ignore[arg-type]
                                answer=answer, error=error,
                                last_usage=run.state.last_usage,
                                tool_calls=run.state.tool_calls,
                                output_tokens=run.state.usage.output),
                dedupe_key=f"child:{run.task_id}",
            ))
        try:
            await run.append([TaskEnded(status=status, answer=answer, error=error)],  # type: ignore[arg-type]
                             posts=posts)
            return True
        except InboxUnread:
            await run.take_input()
            if decide_again:
                return False


async def fail_turn(run: TaskRun, reason: str, detail: str = "") -> bool:
    """A turn that cannot go on. A one-shot task ends failed; an
    interactive one keeps its conversation and waits for the next
    message (0.6's ``turn_failed``)."""
    assert run.agent is not None
    if run.one_shot:
        error = f"{reason}: {detail}" if detail else reason
        await end(run, "failed", run.state.last_answer, error)
        return False
    await close_calls(
        run, [c.use.id for c in run.state.open_calls], f"{texts.TURN_FAILED_CALL_ERROR}{reason}",
        core_records.event(core_records.TurnFailed(reason=reason, detail=detail)),
        "the parent's turn failed", started_error=texts.INTERRUPTED_CALL_ERROR)
    return True


async def continue_after_cap(run: TaskRun, *, dropped: ModelResponse | None = None) -> bool:
    """Ask the model to go on after a reply cut at the output cap; ``dropped``
    is a cut reply that held only reasoning, which is not recorded (its
    usage is)."""
    thinking_only = dropped is not None
    recoveries = 0
    for m in reversed(run.state.messages):
        if m.role == "user" and m.origin == "system" and m.text in (
                texts.OUTPUT_LIMIT_RESUME_NUDGE, texts.OUTPUT_LIMIT_THINKING_NUDGE):
            recoveries += 1
        elif m.role == "user":
            break
    if recoveries >= texts.MAX_OUTPUT_TOKENS_RECOVERY:
        return await fail_turn(run, texts.LLM_TRUNCATED,
                               "the response kept hitting the output token limit")
    nudge = texts.OUTPUT_LIMIT_THINKING_NUDGE if thinking_only \
        else texts.OUTPUT_LIMIT_RESUME_NUDGE
    record = core_records.Continue() if dropped is None else core_records.Continue(
        model=dropped.model or run.model, usage=dropped.usage)
    await run.append([core_records.event(record, message=user(nudge, origin="system"))])
    return True


#: Empty replies in a row a turn goes on after, at most (Claude Code's
#: re-ask goes on after one per step).
MAX_EMPTY_REPLIES = 3


async def after_empty_reply(run: TaskRun, response: ModelResponse) -> bool:
    """The model ended its reply with no text and no tool call (reasoning
    alone counts as nothing). The reply is not recorded (its usage is). An
    extension may go on with a message (``Extension.on_empty_reply``, the
    first one wins; one that fails is skipped); otherwise, or after
    :data:`MAX_EMPTY_REPLIES` in a row, the turn ends without an answer."""
    model, usage = response.model or run.model, response.usage
    view = run.view()
    message: Message | None = None
    # A hard stop the extensions cannot talk the kernel out of: so many
    # empty replies in a row, and the turn ends.
    for ext in run.exts if run.state.empty_replies < MAX_EMPTY_REPLIES else ():
        try:
            message = await ext.on_empty_reply(view, response)
        except Exception:  # a failing extension has no opinion
            log.exception("extension %s on_empty_reply failed", ext.name)
            message = None
        if message is not None:
            break
    # In one append with what the extensions recorded meanwhile.
    view.pending.append(core_records.event(core_records.TurnDone(
        reason=core_records.EMPTY_REPLY, model=model, usage=usage)) if message is None
        else core_records.event(core_records.Continue(
            reason=core_records.EMPTY_REPLY, model=model, usage=usage), message=message))
    await run.flush(view)
    return True
