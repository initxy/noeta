"""Project the envelope stream into a human-readable message view.

Sugar for logs, debugging, and quick inspection by SDK users: given the same
envelopes and the ContentStore they were written against, the output is
deterministic and preserves the stream's order. The envelope stream stays the
record of truth — nothing here enters the durable record or records events, so
an unresolvable body degrades to ``None`` rather than failing the whole read.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Iterable, Optional, TypeVar, Union

from noeta.core.fold import messages_from_appended
from noeta.core.prefetch import prefetched
from noeta.protocols.content_store import ContentStore
from noeta.protocols.events import (
    EventEnvelope,
    TaskCompletedPayload,
    TaskFailedPayload,
    answer_from_payload,
    ToolCallStartedPayload,
    ToolResultRecordedPayload,
)
from noeta.protocols.messages import (
    ImageBlock,
    Message,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from noeta.protocols.tool_args import resolve_tool_call_arguments
from noeta.protocols.values import ContentRef


_log = logging.getLogger(__name__)

_P = TypeVar("_P")


def _expect_payload(env: EventEnvelope, cls: type[_P]) -> _P:
    """Narrow ``env.payload`` to the payload class its ``type`` promises.

    A mismatch means the stream itself is malformed (an envelope whose
    ``type`` says one event but whose payload is another) — raised as
    ``TypeError`` rather than ``assert`` so the guard survives ``python -O``.
    """
    payload = env.payload
    if not isinstance(payload, cls):
        raise TypeError(
            f"envelope type {env.type!r} carries a "
            f"{type(payload).__name__} payload; expected {cls.__name__}"
        )
    return payload


@dataclass(frozen=True, slots=True)
class AssistantMessage:
    """A plain-text reply fragment from the assistant."""

    text: str


@dataclass(frozen=True, slots=True)
class UserMessage:
    """A plain-text input fragment from the user (goal / send_goal / follow-up)."""

    text: str


@dataclass(frozen=True, slots=True)
class ToolUse:
    """The model requests a tool call.

    A ``ToolUseBlock`` inside an assistant message and a standalone
    ``ToolCallStarted`` event describe the same call, so the first occurrence
    of a ``call_id`` wins and any later one is dropped.
    """

    call_id: str
    tool_name: str
    arguments: dict


@dataclass(frozen=True, slots=True)
class ToolResultView:
    """The result view of one tool call.

    ``output`` is ``None`` when the body could not be resolved; the call and its
    outcome are still identifiable from ``success`` + ``call_id``.
    """

    call_id: str
    tool_name: str
    success: bool
    output: Optional[str]


@dataclass(frozen=True, slots=True)
class Result:
    """The terminal-state fold of a Task (completed / failed).

    Check ``status`` before trusting ``answer``: on ``"failed"`` it holds the
    failure reason, not an answer. Callers who prefer an exception use
    ``QueryResult.answer()``, which raises ``QueryFailedError`` instead.
    """

    answer: str
    status: str


ViewItem = Union[
    AssistantMessage,
    UserMessage,
    ToolUse,
    ToolResultView,
    Result,
]


def _prefetch_refs(envelopes: Iterable[EventEnvelope]) -> list[ContentRef]:
    """Every ContentRef this projection will dereference, in one scan.

    Deliberately not the same set fold prefetches: the two traversals read
    different bodies (this is the one consumer that renders tool outputs, and
    it never reads ``thinking_ref``), and sharing one table would make each pay
    for the other's reads.
    """
    refs: list[ContentRef] = []
    for env in envelopes:
        event_type = env.type
        if event_type == "MessagesAppended":
            refs.append(env.payload.messages_ref)
        elif event_type == "ToolCallStarted":
            arguments_ref = env.payload.arguments_ref
            if arguments_ref is not None:
                refs.append(arguments_ref)
        elif event_type == "ToolResultRecorded":
            refs.append(env.payload.output_ref)
        elif event_type == "TaskCompleted":
            answer_ref = env.payload.answer_ref
            if answer_ref is not None:
                refs.append(answer_ref)
    return refs


def as_messages(
    envelopes: Iterable[EventEnvelope],
    content_store: ContentStore,
) -> list[ViewItem]:
    """Project the envelope stream into a human-readable list of message views.

    ``content_store`` must be the store **paired with** the envelope stream:
    the envelopes carry ``ContentRef``\\ s only the originating host's store can
    resolve, so a fresh store deterministically loses every body. With a
    ``Client``, use ``client.messages(task_id)``; with one-shot ``query``, use
    the pre-folded ``QueryResult.messages()``.
    """
    # Materialised because the stream is walked twice — once to collect refs,
    # once to project — and ``envelopes`` may be a one-shot iterator.
    stream = list(envelopes)
    # One batch read for the whole projection. Unlike fold there is no snapshot
    # to shorten the walk: this reads the task's entire history, so the per-ref
    # cost it replaces grows without bound as the conversation does.
    content_store = prefetched(content_store, _prefetch_refs(stream))

    out: list[ViewItem] = []
    seen_tool_use: set[str] = set()
    seen_tool_result: set[str] = set()

    for env in stream:
        t = env.type

        if t == "MessagesAppended":
            _project_messages(env, content_store, out, seen_tool_use, seen_tool_result)

        elif t == "ToolCallStarted":
            _project_tool_call_started(env, content_store, out, seen_tool_use)

        elif t == "ToolResultRecorded":
            _project_tool_result_recorded(
                env, content_store, out, seen_tool_result
            )

        elif t == "TaskCompleted":
            payload = _expect_payload(env, TaskCompletedPayload)
            answer = answer_from_payload(payload, content_store)
            out.append(Result(answer=str(answer), status="completed"))

        elif t == "TaskFailed":
            payload = _expect_payload(env, TaskFailedPayload)
            out.append(Result(answer=payload.reason, status="failed"))

    return out


def _project_messages(
    env: EventEnvelope,
    content_store: ContentStore,
    out: list[ViewItem],
    seen_tool_use: set[str],
    seen_tool_result: set[str],
) -> None:
    for msg in messages_from_appended(env, content_store):
        _project_one_message(msg, out, seen_tool_use, seen_tool_result)


def _project_one_message(
    msg: Message,
    out: list[ViewItem],
    seen_tool_use: set[str],
    seen_tool_result: set[str],
) -> None:
    role = msg.role
    if role == "system":
        return
    # One block walk serves every conversational role: the branches handle each
    # block type identically and only the text fragments' view type differs. An
    # all-ToolResultBlock user message — the standard tool-feedback shape —
    # needs no special case; the walk emits one view per block and no text.
    text_factory: Callable[[str], ViewItem] = (
        AssistantMessage if role == "assistant" else UserMessage
    )
    _walk_blocks(msg.content, text_factory, out, seen_tool_use, seen_tool_result)


def _walk_blocks(
    blocks: Iterable[object],
    text_factory: Callable[[str], ViewItem],
    out: list[ViewItem],
    seen_tool_use: set[str],
    seen_tool_result: set[str],
) -> None:
    """Project one message's blocks, concatenating runs of text.

    ``ThinkingBlock`` stays out of the view: raw reasoning rides the Extended
    Thinking channel. ``ImageBlock`` also stays out, but still flushes the text
    buffer — otherwise text on either side of an image would wrongly join into
    one fragment.
    """
    text_buf: list[str] = []

    def flush_text() -> None:
        if text_buf:
            out.append(text_factory("".join(text_buf)))
            text_buf.clear()

    for block in blocks:
        if isinstance(block, TextBlock):
            text_buf.append(block.text)
        elif isinstance(block, ThinkingBlock):
            continue
        elif isinstance(block, ImageBlock):
            flush_text()
            continue
        elif isinstance(block, ToolUseBlock):
            flush_text()
            if block.call_id not in seen_tool_use:
                out.append(
                    ToolUse(
                        call_id=block.call_id,
                        tool_name=block.tool_name,
                        arguments=dict(block.arguments or {}),
                    )
                )
                seen_tool_use.add(block.call_id)
        elif isinstance(block, ToolResultBlock):
            flush_text()
            if block.call_id not in seen_tool_result:
                out.append(
                    ToolResultView(
                        call_id=block.call_id,
                        tool_name="",
                        success=block.success,
                        output=_block_output_to_str(block.output),
                    )
                )
                seen_tool_result.add(block.call_id)
    flush_text()


def _project_tool_call_started(
    env: EventEnvelope,
    content_store: ContentStore,
    out: list[ViewItem],
    seen_tool_use: set[str],
) -> None:
    payload = _expect_payload(env, ToolCallStartedPayload)
    call_id = payload.call_id
    if call_id in seen_tool_use:
        return  # first occurrence of a call_id wins
    out.append(
        ToolUse(
            call_id=call_id,
            tool_name=payload.tool_name,
            arguments=resolve_tool_call_arguments(payload, content_store),
        )
    )
    seen_tool_use.add(call_id)


def _project_tool_result_recorded(
    env: EventEnvelope,
    content_store: ContentStore,
    out: list[ViewItem],
    seen_tool_result: set[str],
) -> None:
    payload = _expect_payload(env, ToolResultRecordedPayload)
    if payload.call_id in seen_tool_result:
        return  # first occurrence of a call_id wins
    output: Optional[str]
    try:
        raw = content_store.get(payload.output_ref)
        output = raw.decode("utf-8", errors="replace")
    except Exception:
        # An unresolvable body renders as None rather than failing the read.
        # Logged so a systematically mis-paired ContentStore — every output
        # None — leaves a trace to find.
        _log.debug(
            "tool output %s unresolvable against the given ContentStore",
            payload.call_id,
            exc_info=True,
        )
        output = None
    out.append(
        ToolResultView(
            call_id=payload.call_id,
            # ToolResultRecordedPayload carries no tool_name; left empty in the view
            tool_name="",
            success=payload.success,
            output=output,
        )
    )
    seen_tool_result.add(payload.call_id)


def _block_output_to_str(output: object) -> Optional[str]:
    """Render ``ToolResultBlock.output``; ``repr`` suffices — humans read this."""
    if output is None:
        return None
    return str(output)
