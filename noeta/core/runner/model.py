"""The model step: build the request, call the model, record the reply."""

from __future__ import annotations

import asyncio
import inspect
from typing import TYPE_CHECKING, Any

import msgspec

from noeta.core import records
from noeta.core.errors import ContextOverflow, ModelError, RequestStale, TurnDone, TurnFailed
from noeta.core.events import ModelCalled, ModelReplied
from noeta.core.extension import Reminder, TaskView
from noeta.core.loop.request import build_request, cache_breakpoints, request_hash
from noeta.core.retry import complete_with_retries
from noeta.core.runner import closing, texts
from noeta.core.types import (
    Delta,
    Message,
    ModelRequest,
    ModelResponse,
    Text,
    ToolUse,
    system_message,
    user,
)

if TYPE_CHECKING:
    from noeta.core.loop.state import TaskState
    from noeta.core.runner.task import TaskRun


def build_model_request(run: TaskRun, *, final: bool) -> ModelRequest:
    agent = run.agent
    assert agent is not None
    view = run.view()
    config = run.state.config
    offer = run.tool_offer(view)
    request = build_request(
        run.state,
        model=run.model,
        system_prompt=run.state.prompt(f"system/{agent.name}", agent.system_prompt),
        identity=run.identity,
        parts=[p for ext in run.exts for p in ext.context(view)],
        tools=offer.tools,
        withheld=offer.withheld,
        entries=offer.entries,
        plain=offer.plain,
        effort=run.effort,
        max_tokens=agent.max_tokens or run.rt.output_ceiling(run.model),
    )
    if final:
        # The step-limit round: tool schemas stay (the prefix stays cached),
        # tools are switched off, and the instruction is not recorded.
        prompt = texts.STEP_LIMIT_PROMPT.format(n=agent.step_budget)
        request = msgspec.structs.replace(
            request, messages=[*request.messages, user(prompt, origin="system")],
            tool_choice="none")
    return request


async def _remind(run: TaskRun, request: ModelRequest) -> ModelRequest:
    """Record the extensions' reminders and put them in ``request`` right
    after its recorded messages (trailing unrecorded ones, such as the
    step-limit prompt, stay last), with the breakpoints the next request,
    built from the log, gets."""
    view = run.view()
    due: list[tuple[str, Reminder]] = []
    try:
        for ext in run.exts:
            due += [(ext.name, r) for r in await ext.reminders(view)]
    except TurnFailed:
        _record_reminders(view, due)
        await run.flush(view)  # what was recorded before the failure stands
        raise
    _record_reminders(view, due)
    if not view.pending:
        return request
    unrecorded = request.messages[len(run.state.messages):]
    await run.flush(view)
    # The recorded messages are taken again from the folded log, not the
    # old ones plus what was added: a record among the reminders' may
    # also have taken messages out (an erase).
    return msgspec.structs.replace(
        request, messages=[*run.state.messages, *unrecorded],
        cache_after=cache_breakpoints(run.state))


def _record_reminders(view: TaskView, due: list[tuple[str, Reminder]]) -> None:
    due.sort(key=lambda item: (item[1].role == "system", item[1].order))
    for name, r in due:
        message = system_message(r.text) if r.role == "system" else user(r.text, origin=r.origin)
        view.record(name, r.kind, r.data, message=message)


def _empty(message: Message) -> bool:
    return not message.text.strip() and not message.tool_uses


def _final_answer(run: TaskRun, response: ModelResponse) -> ModelResponse:
    assert run.agent is not None
    head = texts.STEP_LIMIT_HEAD.format(n=run.agent.step_budget)
    text = response.message.text.strip()
    blocks: list[Any] = [b for b in response.message.content if not isinstance(b, ToolUse)
                         and not isinstance(b, Text)]
    blocks.append(Text(text=f"{head}\n\n{text}" if text else head))
    return ModelResponse(message=Message(role="assistant", content=blocks),
                         stop_reason="end_turn", usage=response.usage, model=response.model)


async def _run_hooks(run: TaskRun, request: ModelRequest) -> ModelRequest | bool:
    """The reminders, then every ``before_model``: the request to send, or
    what ``call_model`` returns when a hook ended the step (the turn failed
    or is done, or the request went stale)."""
    view = run.view()
    try:
        # The reminders first, as Claude Code adds its notes before it
        # decides to compact: the hooks see the request that would go.
        request = await _remind(run, request)
        view = run.view()
        for ext in run.exts:
            request = await ext.before_model(view, request)
        await run.flush(view)
    except TurnFailed as exc:
        await run.flush(view)
        return await closing.fail_turn(run, exc.reason, exc.detail)
    except TurnDone:
        await run.flush(view)
        await run.append([records.event(records.TurnDone())])
        return True
    except RequestStale:
        await run.flush(view)
        return True  # the next boundary builds the request from the new state
    return request


def _overflow_hook(ext: Any, view: TaskView, request: ModelRequest) -> Any:
    """``ext.on_context_overflow``, also for one written before it took the
    request (``(view)`` only)."""
    hook = ext.on_context_overflow
    try:
        old = len(inspect.signature(hook).parameters) < 2
    except (TypeError, ValueError):
        old = False
    return hook(view) if old else hook(view, request)


async def _on_overflow(run: TaskRun, exc: ContextOverflow, request: ModelRequest) -> bool:
    """The model refused ``request`` as too long: an extension makes room
    and the next boundary calls again, or the turn fails."""
    overflow_view = run.view()
    try:
        for ext in run.exts:
            if await _overflow_hook(ext, overflow_view, request):
                await run.flush(overflow_view)
                return True  # room was made; the next boundary calls again
    except TurnFailed as failed:
        await run.flush(overflow_view)
        return await closing.fail_turn(run, failed.reason, failed.detail)
    return await closing.fail_turn(run, texts.CONTEXT_OVERFLOW, _detail(exc))


async def call_model(run: TaskRun, *, final: bool = False) -> bool:
    if run.state.model_attempts >= texts.MAX_MODEL_ATTEMPTS:
        return await closing.fail_turn(run, texts.LLM_ERROR,
                                       "the model call kept being interrupted")
    prepared = await _run_hooks(run, build_model_request(run, final=final))
    if isinstance(prepared, bool):
        return prepared
    request = prepared
    await run.append([ModelCalled(model=request.model, request_hash=request_hash(request),
                                  attempt=run.state.model_attempts + 1)])

    current = {"attempt": 1}
    sink, tid, called = run.rt.on_delta, run.task_id, run.state.last_seq
    run.partial, run.thinking = [], []

    def on_delta(d: Delta) -> None:
        if d.kind == "block":
            if d.block is not None and (d.block.text or d.block.signature or d.block.data):
                run.thinking.append(d.block)
            return
        if d.kind == "text":
            run.partial.append(d.text)
        if sink is not None:
            sink(tid, msgspec.structs.replace(d, call_seq=called, attempt=current["attempt"]))

    pending: list[asyncio.Task[None]] = []

    def on_retry(attempt: int, delay: float, error: Exception) -> None:
        # Hosts see "retrying" (and drop the failed attempt's deltas).
        current["attempt"] = attempt + 1
        run.partial, run.thinking = [], []
        pending.append(asyncio.ensure_future(run.append([records.event(records.Retry(
            attempt=attempt, delay=delay, error=str(error)[: texts.ERROR_DETAIL_CHARS]))])))

    try:
        provider = run.rt.provider_for(request.model)
        response = await complete_with_retries(provider, request, on_delta, sleep=run.rt.sleep,
                                               on_retry=on_retry)
    except ContextOverflow as exc:
        await asyncio.gather(*pending, return_exceptions=True)
        return await _on_overflow(run, exc, request)
    except ModelError as exc:
        await asyncio.gather(*pending, return_exceptions=True)
        return await closing.fail_turn(run, texts.LLM_ERROR, _detail(exc))
    await asyncio.gather(*pending, return_exceptions=True)
    run.partial, run.thinking = [], []
    if final:
        response = _final_answer(run, response)
    message = response.message
    if response.stop_reason == "max_tokens" and _empty(message):
        # Cut while still reasoning: nothing to record (an empty assistant
        # turn is a provider 400 on every later request).
        return await closing.continue_after_cap(run, dropped=response)
    if response.stop_reason == "end_turn" and _empty(message):
        return await closing.after_empty_reply(run, response)
    if _empty(message):
        return await closing.fail_turn(run, texts.LLM_EMPTY_RESPONSE,
                                   f"the model returned no text and no tool call "
                                   f"(stop: {response.stop_reason})")
    response = unique_call_ids(run.state, response, run.state.last_seq + 1)
    await run.append([ModelReplied(response=response)])
    return True


def unique_call_ids(state: TaskState, response: ModelResponse, seq: int) -> ModelResponse:
    """Give every tool call an id no earlier call of this task has. Answers,
    approvals and child ids are keyed by call id, so a gateway that sends no
    id (or reuses one) must not let one call inherit another's answer."""
    used = set(state.call_ids)
    content: list[Any] = []
    changed = False
    n = 0
    for block in response.message.content:
        if isinstance(block, ToolUse):
            new_id = block.id
            while not new_id or new_id in used:
                new_id = f"call_{seq}_{n}"
                n += 1
            used.add(new_id)
            if new_id != block.id:
                block = msgspec.structs.replace(block, id=new_id)
                changed = True
        content.append(block)
    if not changed:
        return response
    message = msgspec.structs.replace(response.message, content=content)
    return msgspec.structs.replace(response, message=message)


def _detail(exc: ModelError) -> str:
    body = (exc.body or "")[: texts.ERROR_DETAIL_CHARS]
    head = str(exc)
    return f"{head} — {body}" if body and body not in head else head
