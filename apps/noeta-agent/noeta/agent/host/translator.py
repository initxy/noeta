"""Deterministic translation of noeta EventEnvelope → frontend UI events.

Replay (events_after) and live (subscribe) share this module, guaranteeing the
two paths produce identical output. The vocabulary is documented in the
implementation notes' "SSE wire protocol" section; envelopes outside the
vocabulary are never sent downstream.

deref is the ContentRef → bytes content-fetch callback (usually bound to
client.get_content).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Optional

#: Truncation cap (characters) for tool output/arguments in UI events.
OUTPUT_CLIP = 2000

DerefFn = Callable[[Any], Optional[bytes]]


@dataclass
class UIEvent:
    """One SSE event sent to the frontend. seq=None marks a synthetic event
    (not replayable)."""

    seq: Optional[int]
    type: str
    data: dict


def _clip(text: str, limit: int = OUTPUT_CLIP) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n… (truncated; {len(text)} characters total)"


def _deref_json(deref: DerefFn, ref: Any) -> Any:
    if ref is None:
        return None
    raw = deref(ref)
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return raw.decode("utf-8", errors="replace")


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, default=str)


# noeta memory tool name → memory_op's op value (semantic folding for the UI,
# see the ToolCallStarted branch).
_MEMORY_TOOL_OPS = {
    "memory_write": "write",
    "memory_read": "read",
    "memory_search": "search",
    "memory_archive": "archive",
}


def _is_content_ref(value: Any) -> bool:
    """Detect a ContentRef by canonical tag (getattr rather than isinstance —
    the translator does not import noeta types, and tests using
    SimpleNamespace are detected the same way)."""
    return getattr(value, "__canonical_tag__", "") == "content_ref"


def _wake_tag(wake_on: Any) -> str:
    """The canonical tag of wake_on (``human_response`` /
    ``subtask_group_completed`` / ``subtask_completed`` / ``timer_fired`` /
    ``external_event``); returns '' when there is no tag.

    noeta's WakeCondition is a tagged union: each condition dataclass carries a
    ``__canonical_tag__`` class attribute (see ``noeta.protocols.wake``). In
    subscribe / replay callbacks wake_on is a deserialized object and getattr
    reads the tag; tests mocking it with SimpleNamespace can set the same
    attribute.
    """
    return str(getattr(wake_on, "__canonical_tag__", "") or "")


def is_waiting_subtask(wake_on: Any) -> bool:
    """Whether the root task is suspended on a wait-for-subtasks barrier
    (foreground fan-out's SubtaskGroupCompleted, or a single subtask's
    SubtaskCompleted).

    During this window the session must not end the turn: subtasks are
    running, the frontend subtask cards are in progress, the composer should
    stay locked, and status stays running. Wrongly flipping to idle would let
    the user inject messages mid-subtask and pollute the conversation (the
    defect where the session shows as writable while a subagent is
    executing). The check reads the canonical tag rather than each
    condition's fields (SubtaskGroupCompleted has no ``handle``, which
    distinguishes it from HumanResponseReceived).
    """
    return _wake_tag(wake_on) in ("subtask_group_completed", "subtask_completed")


def _flatten_question(raw: dict) -> list[dict]:
    """Normalize the question body from the content store into the pinned
    protocol's questions array."""
    questions = []
    for q in raw.get("questions", []):
        questions.append(
            {
                "id": q.get("id", ""),
                "question": q.get("question", ""),
                "header": q.get("header"),
                "choices": [
                    {
                        "id": c.get("id", ""),
                        "label": c.get("label", ""),
                        "description": c.get("description"),
                    }
                    for c in (q.get("choices") or [])
                ],
                "allow_freeform": bool(q.get("allow_freeform", False)),
            }
        )
    return questions


def _from_messages(seq: int, body: Any) -> list[UIEvent]:
    """MessagesAppended message body → user_message / assistant_text /
    skill_activated.

    role=tool (tool receipts, question-answer echoes) is not sent —
    tool_result is ToolResultRecorded's job, and question answers are
    question_answered's. user messages carrying an origin (system/memory:
    background-subtask completion notices, memory recall and other
    host-injected content) are not sent either — those are for the model to
    read, not something the user said.
    """
    events: list[UIEvent] = []
    if not isinstance(body, list):
        return events
    for msg in body:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        content = msg.get("content") or []
        if role == "user" and not msg.get("origin"):
            text = "\n".join(
                b.get("text", "")
                for b in content
                if isinstance(b, dict) and b.get("__canonical_tag__") == "text_block"
            ).strip()
            if text:
                events.append(UIEvent(seq, "user_message", {"content": text}))
        elif role == "assistant":
            for block in content:
                if not isinstance(block, dict):
                    continue
                tag = block.get("__canonical_tag__")
                if tag == "text_block":
                    text = (block.get("text") or "").strip()
                    if text:
                        events.append(UIEvent(seq, "assistant_text", {"text": text}))
                elif tag == "tool_use_block" and block.get("tool_name") == "skill":
                    skill = (block.get("arguments") or {}).get("skill", "")
                    if skill:
                        events.append(UIEvent(seq, "skill_activated", {"skill": skill}))
                # Other tool_use blocks (ask_user_question included) are not
                # sent from here: actually-executing tools go through
                # ToolCallStarted, questions through UserQuestionRequested.
    return events


def _translate_subtask(env: Any, deref: DerefFn, subtask_id: str) -> list[UIEvent]:
    """The narrow vocabulary for subtask streams: tool activity + cancel
    wrap-up; everything else is never sent.

    - All seq=None (synthetic events): a subtask stream's seq counts
      independently of the parent stream, so carrying it would get the events
      wrongly swallowed by the frontend's parent-stream lastSeq dedup; subtask
      events also take no part in replay (replay reads only the root stream —
      the parent is always the root, nested delegation does not exist).
    - Lifecycle does not map to turn_started/turn_finished (the session
      follows only the root task); completion/failure is expressed by the
      parent stream's BackgroundSubagentDelivered / SubtaskCompleted. The one
      exception is cancel cascades: the subtask only writes TaskCancelled to
      its own stream (no Delivered), so it must be wrapped up here, otherwise
      the frontend card stays stuck in running.
    """
    etype = env.type
    p = env.payload

    if etype == "ToolCallStarted":
        arguments = p.arguments
        if arguments is None and getattr(p, "arguments_ref", None) is not None:
            arguments = _deref_json(deref, p.arguments_ref)
        return [
            UIEvent(
                None,
                "tool_call",
                {
                    "call_id": p.call_id,
                    "tool_name": p.tool_name,
                    "arguments": arguments or {},
                    "subtask_id": subtask_id,
                },
            )
        ]

    if etype == "ToolResultRecorded":
        output = _deref_json(deref, getattr(p, "output_ref", None))
        return [
            UIEvent(
                None,
                "tool_result",
                {
                    "call_id": p.call_id,
                    "success": bool(p.success),
                    "summary": getattr(p, "summary", "") or "",
                    "output": _clip(_as_text(output)),
                    "subtask_id": subtask_id,
                },
            )
        ]

    if etype == "TaskCancelled":
        return [
            UIEvent(
                None,
                "subtask_finished",
                {"subtask_id": subtask_id, "status": "cancelled", "summary": ""},
            )
        ]

    return []


def translate(env: Any, deref: DerefFn, subtask_id: Optional[str] = None) -> list[UIEvent]:
    """One noeta envelope → zero or more UI events (pure function).

    A non-None subtask_id means env comes from one of this session's subtask
    streams (the caller decides by task_id) and goes through
    _translate_subtask's narrow vocabulary.
    """
    if subtask_id is not None:
        return _translate_subtask(env, deref, subtask_id)

    etype = env.type
    seq = env.seq
    p = env.payload

    if etype == "MessagesAppended":
        return _from_messages(seq, _deref_json(deref, p.messages_ref))

    if etype == "AssistantThinkingRecorded":
        body = _deref_json(deref, p.thinking_ref)
        texts = []
        if isinstance(body, list):
            for block in body:
                if isinstance(block, dict) and block.get("thinking"):
                    texts.append(block["thinking"])
                elif isinstance(block, dict) and block.get("text"):
                    texts.append(block["text"])
        text = "\n".join(texts).strip()
        return [UIEvent(seq, "thinking", {"text": _clip(text)})] if text else []

    if etype == "LLMRetryScheduled":
        # A transient LLM failure (429 rate limit / network flap) is about to
        # back off and retry: an observational event, written to the EventLog
        # (visible in replay too). The frontend uses it to clear the streaming
        # delta buffer for the same call_id — the retry re-streams reusing the
        # same call_id, and not clearing the buffer would splice the old and
        # new half-streams into garbage. The frontend renders no UI bar (an
        # explicit "retrying" indicator is left for later work).
        return [UIEvent(seq, "llm_retry", {"call_id": p.call_id})]

    if etype == "Compacted":
        # Macro-compaction landed: the old prefix has been folded into one
        # summary. The chat page renders a lightweight divider so the user
        # knows early history was compacted (aligned with the "Context
        # compacted" notice inside Claude Code sessions); the trigger reason
        # and micro-compaction details are on the Trace page.
        replaced = getattr(p, "replaced_count", None)
        return [
            UIEvent(
                seq,
                "compaction",
                {"replaced_count": replaced if isinstance(replaced, int) else 0},
            )
        ]

    if etype == "ToolCallStarted":
        arguments = p.arguments
        if arguments is None and getattr(p, "arguments_ref", None) is not None:
            arguments = _deref_json(deref, p.arguments_ref)
        arguments = arguments or {}
        # In noeta, memory is ordinary tools (the four write/read/search/
        # archive); for the UI they fold into a semantic marker instead of
        # going out as a generic step. The paired ToolResultRecorded still
        # translates to tool_result; when the frontend cannot find a step for
        # the call_id it silently drops it (existing behavior), and the full
        # detail lives on the Trace page.
        # The name field uniformly carries the "object": write/read/archive
        # use the memory name, search uses the query string.
        memory_op = _MEMORY_TOOL_OPS.get(p.tool_name)
        if memory_op is not None:
            key = "query" if memory_op == "search" else "name"
            name = arguments.get(key, "") if isinstance(arguments, dict) else ""
            return [
                UIEvent(
                    seq,
                    "memory_op",
                    {"call_id": p.call_id, "op": memory_op, "name": str(name)},
                )
            ]
        return [
            UIEvent(
                seq,
                "tool_call",
                {
                    "call_id": p.call_id,
                    "tool_name": p.tool_name,
                    "arguments": arguments,
                },
            )
        ]

    if etype == "ToolResultRecorded":
        output = _deref_json(deref, getattr(p, "output_ref", None))
        return [
            UIEvent(
                seq,
                "tool_result",
                {
                    "call_id": p.call_id,
                    "success": bool(p.success),
                    "summary": getattr(p, "summary", "") or "",
                    "output": _clip(_as_text(output)),
                },
            )
        ]

    if etype == "TaskStatePatched":
        # The todo_write control tool lands as TaskStatePatched
        # (patch.set_todos replaces the whole list). The same envelope also
        # carries skill activation and other patches — without a set_todos key
        # nothing is sent.
        patch = getattr(p, "patch", None)
        todos = patch.get("set_todos") if isinstance(patch, dict) else None
        if not isinstance(todos, list):
            return []
        return [
            UIEvent(
                seq,
                "todo_update",
                {
                    "todos": [
                        {
                            "id": str(t.get("id", "")),
                            "content": str(t.get("content", "")),
                            "status": str(t.get("status", "pending")),
                        }
                        for t in todos
                        if isinstance(t, dict)
                    ]
                },
            )
        ]

    if etype == "UserQuestionRequested":
        body = _deref_json(deref, p.questions_ref)
        questions = _flatten_question(body) if isinstance(body, dict) else []
        return [
            UIEvent(
                seq,
                "question",
                {
                    "question_id": p.question_id,
                    "reason": getattr(p, "reason", None),
                    "questions": questions,
                },
            )
        ]

    if etype == "UserQuestionAnswered":
        return [UIEvent(seq, "question_answered", {"question_id": p.question_id})]

    # Subtask spawn/wrap-up (parent-stream events). Both shapes map to the
    # same pair of UI events:
    # - background=true single spawn: BackgroundSubagentStarted → …Delivered
    #   (parallel, the parent turn does not block)
    # - fan-out (multi-entry spawns, foreground concurrency): SubtaskSpawned →
    #   SubtaskCompleted
    if etype == "BackgroundSubagentStarted":
        return [
            UIEvent(
                seq,
                "subtask_started",
                {
                    "subtask_id": p.subtask_id,
                    "agent_name": p.agent_name,
                    "goal": p.goal,
                },
            )
        ]

    if etype == "BackgroundSubagentDelivered":
        # summary is not clipped: a subtask result is equivalent to assistant
        # body text (assistant_text is not clipped either); Delivered's
        # summary is inlined in the envelope and bounded by the payload cap.
        return [
            UIEvent(
                seq,
                "subtask_finished",
                {
                    "subtask_id": p.subtask_id,
                    "status": str(p.status),
                    "summary": str(getattr(p, "summary", "") or ""),
                },
            )
        ]

    if etype == "SubtaskSpawned":
        return [
            UIEvent(
                seq,
                "subtask_started",
                {
                    "subtask_id": p.subtask_id,
                    "agent_name": p.agent_name,
                    "goal": p.goal,
                },
            )
        ]

    if etype == "SubtaskCompleted":
        result = getattr(p, "result", None)
        status = str(getattr(result, "status", "completed"))
        if status == "failed":
            summary = getattr(result, "error", None)
        else:
            # When output exceeds the inline threshold it is a ContentRef and
            # must be deref'ed to get the subtask's real return content —
            # calling _as_text directly would only yield the ContentRef's repr
            # string.
            summary = getattr(result, "output", None)
            if _is_content_ref(summary):
                summary = _deref_json(deref, summary)
        return [
            UIEvent(
                seq,
                "subtask_finished",
                {
                    # summary is not clipped: this is the subtask's final
                    # return (equivalent to assistant body text) and truncation
                    # would lose the conclusion; folding the card on demand is
                    # the frontend presentation layer's business.
                    "subtask_id": p.subtask_id,
                    "status": status,
                    "summary": _as_text(summary),
                },
            )
        ]

    if etype in ("TaskStarted", "TaskWoken"):
        return [UIEvent(seq, "turn_started", {})]

    if etype == "TaskSuspended":
        wake_on = getattr(p, "wake_on", None)
        if is_waiting_subtask(wake_on):
            # The root is suspended on a subtask barrier: not the end of the
            # turn, so no turn_finished. Subtask spawn / wrap-up
            # (subtask_started / finished) express the progress; the session
            # stays running (_update_status uses the same check) and the
            # composer stays locked.
            return []
        handle = getattr(wake_on, "handle", "")
        if isinstance(handle, str) and handle.startswith("question-"):
            # Suspended on a question: the question event already expresses
            # this state; no additional turn_finished.
            return []
        return [UIEvent(seq, "turn_finished", {"status": "awaiting_input"})]

    if etype == "TaskCancelled":
        return [UIEvent(seq, "turn_finished", {"status": "cancelled"})]

    if etype == "TaskFailed":
        message = _as_text(getattr(p, "error", None) or getattr(p, "reason", ""))
        return [
            UIEvent(seq, "error", {"message": _clip(message, 500)}),
            UIEvent(seq, "turn_finished", {"status": "failed"}),
        ]

    if etype == "TaskCompleted":
        return [UIEvent(seq, "turn_finished", {"status": "completed"})]

    return []
