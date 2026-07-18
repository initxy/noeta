"""envelope → UI event translator unit tests (including content-store deref)."""
from __future__ import annotations

import json
from types import SimpleNamespace as NS

from noeta.agent.host.translator import OUTPUT_CLIP, translate


def deref_map(mapping: dict[str, bytes]):
    return lambda ref: mapping.get(ref.hash)


def env(etype: str, seq: int, **payload):
    return NS(type=etype, seq=seq, task_id="t1", payload=NS(**payload))


def test_user_and_assistant_messages():
    body = [
        {"__canonical_tag__": "message", "role": "user",
         "content": [{"__canonical_tag__": "text_block", "text": "Hello"}]},
        {"__canonical_tag__": "message", "role": "assistant",
         "content": [
             {"__canonical_tag__": "text_block", "text": "Reply"},
             {"__canonical_tag__": "tool_use_block", "tool_name": "skill",
              "call_id": "c1", "arguments": {"skill": "demo-skill"}},
             {"__canonical_tag__": "tool_use_block", "tool_name": "ask_user_question",
              "call_id": "c2", "arguments": {}},
         ]},
        {"__canonical_tag__": "message", "role": "tool",
         "content": [{"__canonical_tag__": "tool_result_block", "call_id": "c1",
                      "output": "ok", "success": True}]},
    ]
    deref = deref_map({"h1": json.dumps(body).encode()})
    events = translate(
        env("MessagesAppended", 7, messages_ref=NS(hash="h1"), count=3), deref
    )
    assert [(e.type, e.seq) for e in events] == [
        ("user_message", 7), ("assistant_text", 7), ("skill_activated", 7),
    ]
    assert events[0].data == {"content": "Hello"}
    assert events[2].data == {"skill": "demo-skill"}


def test_question_deref_and_flatten():
    body = {"questions": [{"id": "aud", "question": "Audience?",
                            "choices": [{"id": "e", "label": "Engineer"}],
                            "allow_freeform": True}]}
    deref = deref_map({"q1": json.dumps(body).encode()})
    events = translate(
        env("UserQuestionRequested", 11, question_id="c1", call_id="c1",
            questions_ref=NS(hash="q1"), question_count=1,
            reason="needs clarification"),
        deref,
    )
    assert len(events) == 1 and events[0].type == "question"
    data = events[0].data
    assert data["question_id"] == "c1" and data["reason"] == "needs clarification"
    assert data["questions"][0]["choices"][0]["label"] == "Engineer"
    assert data["questions"][0]["allow_freeform"] is True


def test_tool_call_and_result_clip():
    events = translate(
        env("ToolCallStarted", 3, call_id="c9", tool_name="write",
            arguments={"path": "a.md"}, arguments_ref=None),
        lambda ref: None,
    )
    assert events[0].type == "tool_call"
    assert events[0].data["arguments"] == {"path": "a.md"}

    big = "x" * (OUTPUT_CLIP + 100)
    deref = deref_map({"o1": json.dumps(big).encode()})
    events = translate(
        env("ToolResultRecorded", 4, call_id="c9", success=True,
            output_ref=NS(hash="o1"), summary="written"),
        deref,
    )
    data = events[0].data
    assert data["success"] is True and data["summary"] == "written"
    assert len(data["output"]) < OUTPUT_CLIP + 60 and "truncated" in data["output"]


def test_lifecycle_mapping():
    assert translate(env("TaskStarted", 1, worker="w"), None)[0].type == "turn_started"
    assert translate(env("TaskWoken", 2, wake_event=None), None)[0].type == "turn_started"

    hung = translate(
        env("TaskSuspended", 5, reason="waiting_human",
            wake_on=NS(handle="question-c1")), None)
    assert hung == []

    done = translate(
        env("TaskSuspended", 6, reason="waiting_human",
            wake_on=NS(handle="noeta-code-next-goal")), None)
    assert done[0].type == "turn_finished"
    assert done[0].data["status"] == "awaiting_input"

    # Root suspended on a subtask barrier (foreground fan-out): not the end of
    # the turn, no turn_finished — subtasks are running and the session should
    # stay running.
    waiting_sub = translate(
        env("TaskSuspended", 7, reason="waiting_subtask_group",
            wake_on=NS(__canonical_tag__="subtask_group_completed",
                       group_id="g-1", subtask_ids=("t1", "t2"),
                       concurrent=True)), None)
    assert waiting_sub == []
    waiting_single = translate(
        env("TaskSuspended", 8, reason="waiting_subtask",
            wake_on=NS(__canonical_tag__="subtask_completed",
                       subtask_id="t1", result=None)), None)
    assert waiting_single == []

    cancelled = translate(env("TaskCancelled", 7, reason="user"), None)
    assert cancelled[0].data["status"] == "cancelled"

    failed = translate(env("TaskFailed", 8, error="boom"), None)
    assert [e.type for e in failed] == ["error", "turn_finished"]
    assert failed[1].data["status"] == "failed"

    assert translate(env("LLMRequestStarted", 9, model="m"), None) == []
    assert translate(env("ContextPlanComposed", 10), None) == []


def test_todo_update_from_state_patch():
    todos = [
        {"id": "1", "content": "Read the docs", "status": "completed"},
        {"id": "2", "content": "Write the report", "status": "in_progress"},
    ]
    patch = {"set_goal": None, "add_todos": [], "set_todos": todos}
    events = translate(env("TaskStatePatched", 13, patch=patch), None)
    assert len(events) == 1 and events[0].type == "todo_update"
    assert events[0].data["todos"] == todos

    # Non-todo patches such as skill activation (no set_todos key) are not sent
    skill_patch = {"set_goal": None, "activate_skills": ["demo-skill"]}
    assert translate(env("TaskStatePatched", 14, patch=skill_patch), None) == []


def test_memory_tools_fold_to_memory_op():
    events = translate(
        env("ToolCallStarted", 15, call_id="m1", tool_name="memory_write",
            arguments={"name": "user-prefs", "text": "# Preferences"},
            arguments_ref=None),
        None,
    )
    assert [(e.type, e.data) for e in events] == [
        ("memory_op", {"call_id": "m1", "op": "write", "name": "user-prefs"}),
    ]

    events = translate(
        env("ToolCallStarted", 16, call_id="m2", tool_name="memory_read",
            arguments={"name": "user-prefs"}, arguments_ref=None),
        None,
    )
    assert events[0].type == "memory_op"
    assert events[0].data["op"] == "read" and events[0].data["name"] == "user-prefs"

    # search's object is the query term (parameter name query); archive uses
    # name just like read
    events = translate(
        env("ToolCallStarted", 17, call_id="m3", tool_name="memory_search",
            arguments={"query": "rate limiting"}, arguments_ref=None),
        None,
    )
    assert [(e.type, e.data) for e in events] == [
        ("memory_op", {"call_id": "m3", "op": "search", "name": "rate limiting"}),
    ]

    events = translate(
        env("ToolCallStarted", 18, call_id="m4", tool_name="memory_archive",
            arguments={"name": "stale-note"}, arguments_ref=None),
        None,
    )
    assert [(e.type, e.data) for e in events] == [
        ("memory_op", {"call_id": "m4", "op": "archive", "name": "stale-note"}),
    ]


def test_origin_tagged_user_message_not_forwarded():
    """Host-injected user messages with origin=system/memory (background
    subtask notices, memory recall) must not masquerade as user messages."""
    body = [
        {"__canonical_tag__": "message", "role": "user", "origin": "system",
         "content": [{"__canonical_tag__": "text_block",
                      "text": "Search finished\n<background-subagent id=\"s1\"/>"}]},
    ]
    deref = deref_map({"h2": json.dumps(body).encode()})
    events = translate(
        env("MessagesAppended", 20, messages_ref=NS(hash="h2"), count=1), deref
    )
    assert events == []


def test_background_subagent_parent_events():
    started = translate(
        env("BackgroundSubagentStarted", 21, subtask_id="s1",
            agent_name="explorer", goal="search: find code", call_id="c1"),
        None,
    )
    assert started[0].type == "subtask_started" and started[0].seq == 21
    assert started[0].data == {
        "subtask_id": "s1", "agent_name": "explorer", "goal": "search: find code",
    }

    delivered = translate(
        env("BackgroundSubagentDelivered", 22, subtask_id="s1",
            result_ref=NS(hash="r1"), summary="found 3 places",
            status="completed"),
        None,
    )
    assert delivered[0].type == "subtask_finished"
    assert delivered[0].data == {
        "subtask_id": "s1", "status": "completed", "summary": "found 3 places",
    }


def test_foreground_subtask_parent_events():
    spawned = translate(
        env("SubtaskSpawned", 23, subtask_id="s2", agent_name="explorer",
            goal="search: check config", inputs={}),
        None,
    )
    assert spawned[0].type == "subtask_started"
    assert spawned[0].data["subtask_id"] == "s2"

    completed = translate(
        env("SubtaskCompleted", 24, subtask_id="s2",
            result=NS(status="failed", output=None, error="boom")),
        None,
    )
    assert completed[0].type == "subtask_finished"
    assert completed[0].data["status"] == "failed"
    assert completed[0].data["summary"] == "boom"


def test_foreground_subtask_completed_derefs_output():
    """When SubtaskCompleted's output is a ContentRef, deref out the real
    content.

    Defect regression: _as_text was once applied to the ContentRef directly,
    so the card's "result" section showed the "ContentRef(hash=…)" repr string
    instead of the subtask's returned content.
    """
    ref = NS(__canonical_tag__="content_ref", hash="out1")
    deref = deref_map({"out1": json.dumps("found 3 tracking designs").encode()})
    completed = translate(
        env("SubtaskCompleted", 25, subtask_id="s3",
            result=NS(status="completed", output=ref, error=None)),
        deref,
    )
    assert completed[0].data["status"] == "completed"
    assert completed[0].data["summary"] == "found 3 tracking designs"

    # A small inline output (str) skips deref and is sent as-is
    inline = translate(
        env("SubtaskCompleted", 26, subtask_id="s4",
            result=NS(status="completed", output="inline summary", error=None)),
        None,
    )
    assert inline[0].data["summary"] == "inline summary"

    # Long results are not clipped: this is the subtask's final return
    # (equivalent to assistant body text); truncation would lose the conclusion
    long_text = "L" * 1200
    completed = translate(
        env("SubtaskCompleted", 27, subtask_id="s5",
            result=NS(status="completed", error=None,
                      output=NS(__canonical_tag__="content_ref", hash="out2"))),
        deref_map({"out2": json.dumps(long_text).encode()}),
    )
    assert completed[0].data["summary"] == long_text


def test_subtask_stream_vocabulary():
    """Narrow vocabulary for subtask streams: tool events carry subtask_id
    with seq=None; cancel wraps up; lifecycle/message events are never sent."""
    call = translate(
        env("ToolCallStarted", 5, call_id="c1", tool_name="glob",
            arguments={"pattern": "**/*.md"}, arguments_ref=None),
        None, subtask_id="s1",
    )
    assert call[0].type == "tool_call" and call[0].seq is None
    assert call[0].data["subtask_id"] == "s1"
    assert call[0].data["tool_name"] == "glob"

    result = translate(
        env("ToolResultRecorded", 6, call_id="c1", success=True,
            output_ref=None, summary="2 matches"),
        lambda ref: None, subtask_id="s1",
    )
    assert result[0].type == "tool_result" and result[0].seq is None
    assert result[0].data["subtask_id"] == "s1"

    cancelled = translate(env("TaskCancelled", 7, reason="user"), None, subtask_id="s1")
    assert cancelled[0].type == "subtask_finished"
    assert cancelled[0].data == {"subtask_id": "s1", "status": "cancelled", "summary": ""}

    # Subtask lifecycle/messages do not map into the parent-session vocabulary
    assert translate(env("TaskStarted", 1, lease_id="l"), None, subtask_id="s1") == []
    assert translate(env("TaskCompleted", 9, outcome=None), None, subtask_id="s1") == []
    assert translate(
        env("MessagesAppended", 10, messages_ref=NS(hash="x"), count=1),
        lambda ref: None, subtask_id="s1",
    ) == []


def test_question_answered():
    events = translate(
        env("UserQuestionAnswered", 12, question_id="c1", call_id="c1",
            answers_ref=NS(hash="a"), answer_count=1),
        lambda ref: None,
    )
    assert events[0].type == "question_answered"
    assert events[0].data == {"question_id": "c1"}


def test_compacted_forwards_replaced_count():
    events = translate(
        env("Compacted", 744, boundary_count=103, replaced_count=103,
            composer_version="three_segment.v5", summary_ref=NS(hash="s")),
        lambda ref: None,
    )
    assert [(e.type, e.seq) for e in events] == [("compaction", 744)]
    assert events[0].data == {"replaced_count": 103}


def test_compacted_missing_count_defaults_zero():
    events = translate(env("Compacted", 5, summary_ref=NS(hash="s")), None)
    assert events[0].data == {"replaced_count": 0}


def test_compaction_requested_not_forwarded():
    # Macro-compaction sends one event only when it lands (Compacted);
    # Requested stays outside the vocabulary.
    assert translate(
        env("CompactionRequested", 743, estimated_tokens=37484, reason="proactive"),
        None,
    ) == []


def test_subtask_compacted_not_forwarded():
    # Subtask streams use the narrow vocabulary: compaction inside a subtask
    # must not show up in the parent session's chat stream.
    assert translate(
        env("Compacted", 9, replaced_count=4, summary_ref=NS(hash="s")),
        None, subtask_id="s1",
    ) == []
