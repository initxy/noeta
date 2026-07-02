"""D6 — as_messages: envelope stream → human-readable message view.

The Claude-style message view is **projection sugar** on top of the Noeta
envelope stream — same abstraction layer as ``read_models``: given the same
envelope stream + ContentStore, the output is deterministic. **The canonical
record of truth is always the envelope stream itself.** Projections don't enter
the durable record and don't touch recording; they exist only for logs, debugging,
and quick inspection by SDK users.

Folding rules
-------------

Process envelopes in order, preserving the true event timeline:

1. ``MessagesAppended`` → call ``noeta.core.fold.messages_from_appended`` to get
   ``list[Message]``, then split each Message by role / content into view items:

   * ``role == "assistant"``:
     - ``TextBlock`` text is **concatenated** in order, emitting one
       ``AssistantMessage`` at the next non-TextBlock or at message end;
     - ``ToolUseBlock`` emits its own ``ToolUse``.
   * ``role == "user"``:
     - If the whole message is **entirely** ``ToolResultBlock`` (the standard
       tool-feedback shape), emit a ``ToolResultView`` per block;
     - Otherwise concatenate ``TextBlock`` into a ``UserMessage``, emitting a
       ``ToolResultView`` for any interleaved ``ToolResultBlock``.
   * ``role == "tool"``: treat all content as ``ToolResultBlock`` and emit a
     ``ToolResultView`` per block. (Noeta's spec routes feedback through user
     messages by default; this is a fallback.)
   * ``role == "system"``: skip — the system prompt is request-level metadata,
     not part of the conversation view.
   * ``ThinkingBlock``: skip — raw reasoning is projected via the separate
     Extended Thinking channel.

2. ``ToolCallStarted`` → ``ToolUse``. **If the MessagesAppended path already
   emitted a ToolUse for the same ``call_id``, skip this one (keep the first).**

3. ``ToolResultRecorded`` → ``ToolResultView``. ``output_ref`` is dereferenced
   from the ContentStore (decoded as str); set to ``None`` if missing or on error.

4. ``TaskCompleted`` → ``Result(status="completed", answer=str(payload.answer))``.
   ``TaskFailed`` → ``Result(status="failed", answer=payload.reason)``.

All other event types are skipped. View items keep the relative order of the
envelope stream.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional, Union

from noeta.core.fold import messages_from_appended
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


# ---------------------------------------------------------------------------
# View dataclasses (frozen, hashable for dedup if needed)
# ---------------------------------------------------------------------------


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

    Two sources: a ``ToolUseBlock`` inside an assistant message, or a standalone
    ``ToolCallStarted`` event. The former wins (first occurrence wins; a later
    one with the same call_id is dropped).
    """

    call_id: str
    tool_name: str
    arguments: dict


@dataclass(frozen=True, slots=True)
class ToolResultView:
    """The result view of one tool call.

    ``output`` is the string representation resolved from the ContentStore;
    ``None`` when it can't be resolved. The caller can still identify the call
    and its outcome from ``success`` + ``call_id`` + ``tool_name``.
    """

    call_id: str
    tool_name: str
    success: bool
    output: Optional[str]


@dataclass(frozen=True, slots=True)
class Result:
    """The terminal-state fold of a Task (completed / failed).

    Check ``status`` before trusting ``answer``: on ``status == "failed"``,
    ``answer`` holds the **failure reason**, not a successful answer. Callers
    who want the exception path instead use ``QueryResult.answer()``, which
    raises a coded ``QueryFailedError`` on a non-completed terminal.
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


# ---------------------------------------------------------------------------
# Public entry
# ---------------------------------------------------------------------------


def as_messages(
    envelopes: Iterable[EventEnvelope],
    content_store: ContentStore,
) -> list[ViewItem]:
    """Project the envelope stream into a human-readable list of message views.

    Pure function: same input (envelopes, content_store) → same output list.
    Writes no state, enters no durable record, records no events.

    ``content_store`` must be the store **paired with** the envelope stream:
    the envelopes carry ``ContentRef``\\ s (every ``messages_ref``, tool
    ``output_ref``, a spilled ``answer_ref``) that only the originating host's
    store can resolve — a fresh store deterministically loses those bodies.
    With a ``Client``, use ``client.messages(task_id)``; with one-shot
    ``query``, use the pre-folded ``QueryResult.messages()``.
    """
    out: list[ViewItem] = []
    seen_tool_use: set[str] = set()
    seen_tool_result: set[str] = set()

    for env in envelopes:
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
            payload = env.payload
            assert isinstance(payload, TaskCompletedPayload)
            answer = answer_from_payload(payload, content_store)
            out.append(Result(answer=str(answer), status="completed"))

        elif t == "TaskFailed":
            payload = env.payload
            assert isinstance(payload, TaskFailedPayload)
            out.append(Result(answer=payload.reason, status="failed"))

        # other types silently skipped

    return out


# ---------------------------------------------------------------------------
# Per-type projectors
# ---------------------------------------------------------------------------


def _project_messages(
    env: EventEnvelope,
    content_store: ContentStore,  # noqa: ARG001  — kept for signature symmetry
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
    blocks = msg.content

    if role == "system":
        return

    if role == "assistant":
        text_buf: list[str] = []

        def flush_text() -> None:
            if text_buf:
                out.append(AssistantMessage(text="".join(text_buf)))
                text_buf.clear()

        for block in blocks:
            if isinstance(block, TextBlock):
                text_buf.append(block.text)
            elif isinstance(block, ThinkingBlock):
                # ThinkingBlock does not enter the normal message view
                continue
            elif isinstance(block, ImageBlock):
                # Images don't enter the message view yet, but text
                # must still be flushed — otherwise text on either side of the image gets wrongly concatenated. Full image rendering is left for later.
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
        return

    # user / tool roles
    all_tool_result = all(isinstance(b, ToolResultBlock) for b in blocks) and bool(
        blocks
    )

    if all_tool_result:
        for block in blocks:
            assert isinstance(block, ToolResultBlock)
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
        return

    text_buf = []

    def flush_text2() -> None:
        if text_buf:
            out.append(UserMessage(text="".join(text_buf)))
            text_buf.clear()

    for block in blocks:
        if isinstance(block, TextBlock):
            text_buf.append(block.text)
        elif isinstance(block, ThinkingBlock):
            continue
        elif isinstance(block, ImageBlock):
            # Images don't enter the message view yet, but flush text to avoid concatenating adjacent text.
            flush_text2()
            continue
        elif isinstance(block, ToolResultBlock):
            flush_text2()
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
        elif isinstance(block, ToolUseBlock):
            # a user message should not contain ToolUseBlock in theory; defensive handling
            flush_text2()
            if block.call_id not in seen_tool_use:
                out.append(
                    ToolUse(
                        call_id=block.call_id,
                        tool_name=block.tool_name,
                        arguments=dict(block.arguments or {}),
                    )
                )
                seen_tool_use.add(block.call_id)
    flush_text2()


def _project_tool_call_started(
    env: EventEnvelope,
    content_store: ContentStore,
    out: list[ViewItem],
    seen_tool_use: set[str],
) -> None:
    payload = env.payload
    assert isinstance(payload, ToolCallStartedPayload)
    call_id = payload.call_id
    if call_id in seen_tool_use:
        return  # first occurrence wins (the MessagesAppended path usually comes first)
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
    payload = env.payload
    assert isinstance(payload, ToolResultRecordedPayload)
    if payload.call_id in seen_tool_result:
        return  # first occurrence wins (the MessagesAppended path's ToolResultBlock usually comes first)
    output: Optional[str]
    try:
        raw = content_store.get(payload.output_ref)
        output = raw.decode("utf-8", errors="replace")
    except Exception:
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _block_output_to_str(output: object) -> Optional[str]:
    """ToolResultBlock.output → view string.

    ``None`` is returned as-is; other scalars use ``str``; for dict/list, a
    JSON-style repr is enough (the view is for human reading only).
    """
    if output is None:
        return None
    return str(output)
