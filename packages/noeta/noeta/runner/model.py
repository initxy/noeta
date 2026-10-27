"""The model step: build the request, call the model, record the reply."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import msgspec

from noeta.errors import ContextOverflow, ModelError
from noeta.events import ExtensionRecorded, ModelCalled, ModelReplied
from noeta.loop.request import build_request, request_hash
from noeta.loop.state import CORE
from noeta.providers.retry import complete_with_retries
from noeta.runner import texts
from noeta.types import Message, ModelRequest, ModelResponse, Text, ToolUse, user

if TYPE_CHECKING:
    from noeta.runner.task import TaskRun


def _request(run: TaskRun, *, final: bool) -> ModelRequest:
    agent = run.agent
    assert agent is not None
    view = run.view()
    config = run.state.config
    request = build_request(
        run.state,
        model=str(config.get("model") or agent.model),
        system_prompt=str(config.get("system_prompt") or agent.system_prompt),
        parts=[p for ext in run.exts for p in ext.context(view)],
        tools=run.tools(view),
        effort=config.get("effort") or agent.effort,
        max_tokens=agent.max_tokens,
        output_schema=agent.output_schema,
    )
    if final:
        # The step-limit round: tool schemas stay (the prefix stays cached),
        # tools are switched off, and the instruction is not recorded.
        prompt = texts.STEP_LIMIT_PROMPT.format(n=agent.max_steps_per_turn)
        request = msgspec.structs.replace(
            request, messages=[*request.messages, user(prompt, origin="system")],
            tool_choice="none")
    return request


def _empty(message: Message) -> bool:
    return not message.text.strip() and not message.tool_uses


def _final_answer(run: TaskRun, response: ModelResponse) -> ModelResponse:
    assert run.agent is not None
    head = texts.STEP_LIMIT_HEAD.format(n=run.agent.max_steps_per_turn)
    text = response.message.text.strip()
    blocks: list[Any] = [b for b in response.message.content if not isinstance(b, ToolUse)
                         and not isinstance(b, Text)]
    blocks.append(Text(text=f"{head}\n\n{text}" if text else head))
    return ModelResponse(message=Message(role="assistant", content=blocks),
                         stop_reason="end_turn", usage=response.usage, model=response.model)


async def call_model(run: TaskRun, *, final: bool = False) -> bool:
    if run.state.model_attempts >= texts.MAX_MODEL_ATTEMPTS:
        return await run.fail_turn(texts.LLM_ERROR, "the model call kept being interrupted")
    view = run.view()
    request = _request(run, final=final)
    for ext in run.exts:
        request = await ext.before_model(view, request)
    await run.flush(view)
    await run.append([ModelCalled(model=request.model, request_hash=request_hash(request),
                                  attempt=run.state.model_attempts + 1)])

    on_delta = None
    if run.rt.on_delta is not None:
        sink, tid = run.rt.on_delta, run.task_id
        on_delta = lambda d: sink(tid, d)  # noqa: E731
    pending: list[asyncio.Task[None]] = []

    def on_retry(attempt: int, delay: float, error: Exception) -> None:
        # Hosts see "retrying" (and drop the failed attempt's deltas).
        pending.append(asyncio.ensure_future(run.append([ExtensionRecorded(
            extension=CORE, kind="retry",
            data={"attempt": attempt, "delay": delay,
                  "error": str(error)[: texts.ERROR_DETAIL_CHARS]},
        )])))

    try:
        response = await complete_with_retries(run.rt.provider_for(request.model), request,
                                               on_delta, sleep=run.rt.sleep, on_retry=on_retry)
    except ContextOverflow as exc:
        await asyncio.gather(*pending, return_exceptions=True)
        overflow_view = run.view()
        for ext in run.exts:
            if await ext.on_context_overflow(overflow_view):
                await run.flush(overflow_view)
                return True  # room was made; the next boundary calls again
        return await run.fail_turn(texts.CONTEXT_OVERFLOW, _detail(exc))
    except ModelError as exc:
        await asyncio.gather(*pending, return_exceptions=True)
        return await run.fail_turn(texts.LLM_ERROR, _detail(exc))
    await asyncio.gather(*pending, return_exceptions=True)

    if final:
        response = _final_answer(run, response)
    message = response.message
    if response.stop_reason == "max_tokens" and _empty(message):
        # Cut while still reasoning: nothing to record (an empty assistant
        # turn is a provider 400 on every later request).
        return await run.continue_after_cap(thinking_only=True)
    if _empty(message):
        return await run.fail_turn(texts.LLM_EMPTY_RESPONSE,
                                   f"the model returned no text and no tool call "
                                   f"(stop: {response.stop_reason})")
    await run.append([ModelReplied(response=response)])
    return True


def _detail(exc: ModelError) -> str:
    body = (exc.body or "")[: texts.ERROR_DETAIL_CHARS]
    head = str(exc)
    return f"{head} — {body}" if body and body not in head else head
