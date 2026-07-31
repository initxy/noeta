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


def _prefetch_refs(envelopes: Iterable[EventEnvelope]) -> list[ContentRef]:
    """Every ContentRef this projection will dereference, in one scan.

    The four projected event types that carry a body: ``messages_ref``, an
    offloaded ``ToolCallStarted.arguments_ref``, ``output_ref`` (this is the
    one consumer that *does* read tool outputs — it renders them) and a spilled
    ``TaskCompleted.answer_ref``. Types the projection skips contribute
    nothing, and neither does ``thinking_ref``: ``ThinkingBlock`` is explicitly
    not part of this view.

    This set is intentionally not fold's — the two traversals read different
    bodies, and a shared table would make each pay for the other's.
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

    Pure function: same input (envelopes, content_store) → same output list.
    Writes no state, enters no durable record, records no events.

    ``content_store`` must be the store **paired with** the envelope stream:
    the envelopes carry ``ContentRef``\\ s (every ``messages_ref``, tool
    ``output_ref``, a spilled ``answer_ref``) that only the originating host's
    store can resolve — a fresh store deterministically loses those bodies.
    With a ``Client``, use ``client.messages(task_id)``; with one-shot
    ``query``, use the pre-folded ``QueryResult.messages()``.
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

        # other types silently skipped

    return out


# ---------------------------------------------------------------------------
# Per-type projectors
# ---------------------------------------------------------------------------


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
    # One block walk for every conversational role: the assistant and
    # user/tool branches handle each block type identically — only the text
    # fragments' view type differs. (An all-ToolResultBlock user message —
    # the standard tool-feedback shape — needs no special case: the walk
    # emits exactly one ToolResultView per block and no text.)
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
    """Project one message's blocks, concatenating text runs.

    ``TextBlock`` runs concatenate into one ``text_factory`` item, flushed at
    the next non-text block or at message end. ``ThinkingBlock`` is skipped
    (raw reasoning rides the Extended Thinking channel). ``ImageBlock`` does
    not enter the view yet but still flushes text — otherwise text on either
    side of an image would wrongly concatenate. ``ToolUseBlock`` /
    ``ToolResultBlock`` emit their own items, first occurrence per call_id
    wins (``ToolUseBlock`` in a user message is defensive — the spec routes
    tool use through assistant messages).
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
    payload = _expect_payload(env, ToolResultRecordedPayload)
    if payload.call_id in seen_tool_result:
        return  # first occurrence wins (the MessagesAppended path's ToolResultBlock usually comes first)
    output: Optional[str]
    try:
        raw = content_store.get(payload.output_ref)
        output = raw.decode("utf-8", errors="replace")
    except Exception:
        # Documented degradation (see module docstring): an unresolvable
        # output body renders as None. Logged so a systematically mis-paired
        # ContentStore (every output None) leaves a trace to find.
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
