"""Deterministic LLM for mock mode: a content-routing responder for
FakeLLMProvider.

No positional cursor: routing is by the content of the request's message
history, and arbitrary input never crashes (end_turn fallback).

Trigger → behavior map (all triggers are matched against the message history;
user-facing triggers are simple lowercase English words):

- Demo chain (any first message): first turn → ask_user_question clarification
  (report audience) → skill activation of ``demo-skill`` →
  ``sandbox_file_write`` producing ``report.md`` → end_turn summary. When the
  sandbox is not enabled (no file tool in the request), the file write is
  skipped and the turn ends directly; later turns answer briefly.
- Delegation chain (goal contains ``parallel search``):
  spawn_subagent(background=true) dispatches explorer → the subagent (its goal
  starts with ``search:``, served by this same provider) runs a shell_run
  search → end_turn with a search conclusion → the result notice wakes the
  parent turn → end_turn summary. A subagent goal containing ``slow`` delays
  each of its LLM responses by 1s (timing window for the cancel-cascade test).
- Memory chain (goal contains ``remember``): memory_write → end_turn, letting
  e2e tests verify the memory resolver persists per space. The
  ``__consolidation__`` curation agent (goal starts with the SDK's
  "Memory consolidation run" preamble) likewise does memory_write → end_turn,
  letting e2e tests verify background consolidation persists per space.
- Feedback analysis (goal contains ``negative feedback items``, assembled by
  build_analysis_goal): read the first feedback task's transcript →
  submit_suggestion (memory channel by default; when the user note contains
  ``skill:<name>``, submit on the skill channel with a skill_patch instead,
  for the e2e one-click-apply path) → end_turn.
- Feedback report (goal contains ``into an improvement report``, assembled by
  build_report_goal): submit_report once → end_turn.
"""
from __future__ import annotations

import re
import time
import uuid
from typing import Any, Optional

from noeta.sdk import (
    LLMRequest,
    LLMResponse,
    Message,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
)
from noeta.sdk.testing import FakeLLMProvider


def _text_of(msg: Message) -> str:
    return "\n".join(
        b.text for b in (msg.content or []) if isinstance(b, TextBlock)
    ).strip()


def _user_texts(messages: list[Message]) -> list[str]:
    """Real user messages. Excludes synthetic user messages injected by the
    host:

    - origin-tagged ones (system/memory): delegation nudge, todo reminder,
      background-subtask result notice, memory recall, etc.;
    - legacy untagged injections: <workspace-environment>-style tag blocks and
      the "Activated skill: ..." instruction block after a skill activation.

    Note: background-subtask notices (origin=system) are excluded too; the
    branch that needs to see them scans origin messages itself (see
    _background_notice).
    """
    out: list[str] = []
    for m in messages:
        if m.role != "user" or m.origin:
            continue
        t = _text_of(m)
        if not t or t.lstrip().startswith("<") or t.startswith("Activated skill:"):
            continue
        out.append(t)
    return out


def _background_notice(messages: list[Message]) -> str:
    """The background-subtask result notice inside the trailing host-injected
    region (the signature of a woken turn).

    Scans backward over origin-tagged injected messages only (reminders,
    notices) and stops at the first real message — guaranteeing that only "the
    turn woken by the notice" matches; later turns no longer trigger.
    """
    for m in reversed(messages):
        if m.role == "user" and m.origin:
            t = _text_of(m)
            if "<background-subagent" in t:
                return t
            continue
        break
    return ""


def _tool_uses(messages: list[Message]) -> dict[str, str]:
    """call_id → tool_name index (scans ToolUseBlocks in assistant messages)."""
    index: dict[str, str] = {}
    for m in messages:
        if m.role != "assistant":
            continue
        for b in m.content or []:
            if isinstance(b, ToolUseBlock):
                index[b.call_id] = b.tool_name
    return index


def _has_tool_use(messages: list[Message], tool_name: str) -> bool:
    return tool_name in _tool_uses(messages).values()


def _last_tool_result(messages: list[Message]) -> Optional[ToolResultBlock]:
    """Return the last real message if it is a tool receipt.

    The composer's dynamic_suffix appends role=user/origin=system reminders at
    the tail (todo reminders, indexes, etc.); skip those host injections
    before judging.
    """
    for m in reversed(messages):
        if m.role == "user" and m.origin:
            continue
        if m.role != "tool":
            return None
        for b in m.content or []:
            if isinstance(b, ToolResultBlock):
                return b
        return None
    return None


def _answer_summary(messages: list[Message]) -> str:
    """Extract the user's choice/input from the question-answer echo."""
    for m in messages:
        if m.role != "tool":
            continue
        for b in m.content or []:
            if isinstance(b, ToolResultBlock) and isinstance(b.output, dict):
                answers = b.output.get("answers")
                if isinstance(answers, dict):
                    parts = []
                    for qid, ans in answers.items():
                        if isinstance(ans, dict):
                            parts.append(str(ans.get("choice_id") or ans.get("text") or ""))
                    return ", ".join(p for p in parts if p)
    return ""


def _cid() -> str:
    return f"mock-{uuid.uuid4().hex[:8]}"


def _tool_use(name: str, arguments: dict) -> LLMResponse:
    return LLMResponse(
        stop_reason="tool_use",
        content=[ToolUseBlock(call_id=_cid(), tool_name=name, arguments=arguments)],
        usage=Usage(uncached=1, output=1),
    )


def _end_turn(text: str) -> LLMResponse:
    return LLMResponse(
        stop_reason="end_turn",
        content=[TextBlock(text=text)],
        usage=Usage(uncached=1, output=1),
    )


def _report_markdown(goal: str, audience: str) -> str:
    lines = [
        "# Structured report (mock demo)",
        "",
        f"> Request: {goal[:120]}",
        f"> Audience: {audience or 'unspecified'}",
        "",
        "## Background",
        "This is a demo report generated in mock mode, used to verify the "
        "noeta-agent pipeline offline against a product-analytics scenario.",
        "",
        "## Key points",
        "1. Skill loading and activation work.",
        "2. The clarifying-question interaction (audience) completed.",
        "3. The workspace file write succeeded.",
        "",
        "## Conclusion",
        "End-to-end verification passed.",
    ]
    return "\n".join(lines) + "\n"


def _write_report_or_finish(
    request: LLMRequest, messages: list[Message], goal: str
) -> LLMResponse:
    """Write report.md when the sandbox file tool is present; in pure
    conversation mode (no file tool) end the turn directly."""
    tool_names = {t.get("name") for t in (request.tools or []) if isinstance(t, dict)}
    if "sandbox_file_write" not in tool_names:
        return _end_turn(
            "(mock) Sandbox not enabled, skipping the file write. Report "
            "highlights: skill activation and the clarifying-question flow "
            "work."
        )
    return _tool_use(
        "sandbox_file_write",
        {
            "path": "report.md",
            "content": _report_markdown(goal, _answer_summary(messages)),
        },
    )


def mock_responder(request: LLMRequest) -> LLMResponse:
    messages = list(request.messages or [])
    users = _user_texts(messages)
    goal = users[0] if users else ""
    index = _tool_uses(messages)
    last_result = _last_tool_result(messages)

    # --- __consolidation__ curation agent (goal starts with the SDK's
    # _GOAL_PREAMBLE) ---
    if goal.startswith("Memory consolidation run"):
        if last_result is not None:
            return _end_turn(
                "Consolidation done (mock): merged the user preferences from "
                "recent sessions into one memory."
            )
        return _tool_use(
            "memory_write",
            {
                "name": "consolidated-note",
                "text": "Recent sessions show the user prefers concise replies "
                        "(merged by consolidation).",
                "description": "user preference merged by the mock consolidation",
                "type": "user",
            },
        )

    # --- __feedback_analysis__ root-cause agent (goal assembled by
    # build_analysis_goal): read the first feedback task's transcript →
    # submit_suggestion (memory channel by default; when the user note
    # contains `skill:<name>`, switch to the skill channel + skill_patch, for
    # the e2e one-click-apply path) → end turn. ---
    if "negative feedback items" in goal:
        m = re.search(r"feedback_id=(\S+) task_id=(\S+)", goal)
        fid = m.group(1) if m else ""
        tid = m.group(2) if m else ""
        skill_m = re.search(r"skill:([\w-]+)", goal)
        if last_result is not None:
            prev_tool = index.get(last_result.call_id, "")
            if prev_tool == "feedback_transcript" and fid:
                if skill_m:
                    name = skill_m.group(1)
                    return _tool_use(
                        "submit_suggestion",
                        {
                            "channel": "skill",
                            "title": f"Tighten the instructions of {name} (mock root-cause)",
                            "body": f"Feedback shows the output rules of {name} "
                                    "are ambiguous; add explicit rules.",
                            "skill_name": name,
                            "skill_patch": (
                                "---\nname: " + name + "\ndescription: mock revision\n---\n\n"
                                "# " + name + "\n\nVerify the key metrics before "
                                "stating any conclusion (mock patch).\n"
                            ),
                            "evidence": [
                                {
                                    "feedback_id": fid,
                                    "note": f"this feedback points at ambiguous "
                                            f"instructions in {name} (mock)",
                                }
                            ],
                        },
                    )
                return _tool_use(
                    "submit_suggestion",
                    {
                        "channel": "memory",
                        "title": "Align reply conclusions with user feedback (mock root-cause)",
                        "body": "Negative feedback shows the reply's conclusion "
                                "did not match expectations; for similar tasks, "
                                "verify the key conclusions before answering.",
                        "evidence": [
                            {
                                "feedback_id": fid,
                                "note": "the feedback's labels and note point at "
                                        "a wrong conclusion (mock)",
                            }
                        ],
                    },
                )
            return _end_turn("Root-cause analysis done (mock): submitted 1 suggestion.")
        if tid and tid != "?":
            return _tool_use("feedback_transcript", {"task_id": tid})
        return _end_turn(
            "Root-cause analysis done (mock): the feedback has no traceable task."
        )

    # --- __feedback_analysis__ report mode (goal assembled by
    # build_report_goal): submit_report once → end turn. Lets e2e verify the
    # report run and draft persistence. ---
    if "into an improvement report" in goal:
        if last_result is not None:
            return _end_turn("Report submitted (mock).")
        return _tool_use(
            "submit_report",
            {
                "title": "Agent improvement report (mock)",
                "body": (
                    "# Agent improvement report\n\n## Problem classification\n\n"
                    "Mostly conclusion-accuracy issues.\n\n"
                    "## Suggested actions\n\n1. Verify the key metrics before "
                    "stating conclusions.\n"
                ),
            },
        )

    # --- explorer subagent (goal starts with "search:", see the construction
    # in the spawn branch) ---
    if goal.startswith("search:"):
        if "slow" in goal:
            # Timing window for the cancel-cascade test: slow down every LLM
            # response of the subagent so the subtask is still running when
            # the parent task is cancelled (the cascade lands at a step
            # boundary)
            time.sleep(1.0)
        if last_result is not None:
            return _end_turn(
                "Search complete (mock): scanned the relevant workspace files; "
                "the key logic is concentrated under the project source tree."
            )
        return _tool_use("shell_run", {"command": "find . -name '*.md' -type f"})

    # --- last message is a tool receipt: decide the next step by the
    # previous tool ---
    if last_result is not None:
        # Question-answer echo (ask_user_question's receipt is
        # {"answers":…,"question_id":…})
        if isinstance(last_result.output, dict) and "question_id" in last_result.output:
            return _tool_use("skill", {"skill": "demo-skill"})

        prev_tool = index.get(last_result.call_id, "")
        if prev_tool == "memory_write":
            return _end_turn(
                "Remembered (mock): this preference will apply in later sessions."
            )
        if prev_tool == "spawn_subagent":
            return _end_turn(
                "Started explorer in the background for a parallel search; I "
                "will summarize once the results arrive."
            )
        if prev_tool == "skill":
            return _write_report_or_finish(request, messages, goal)
        if prev_tool == "sandbox_file_write":
            return _end_turn(
                "The report has been generated and written to the sandbox "
                "session directory as `report.md`. You can preview it in the "
                "file panel on the right; tell me directly if the structure or "
                "content needs adjusting."
            )
        return _end_turn("(mock) Tool result processed.")

    # --- last message is a user message ---
    # A turn woken by a background search-result notice (origin=system,
    # containing the <background-subagent> marker)
    if _background_notice(messages):
        return _end_turn(
            "The background search finished; its conclusions are summarized "
            "into this reply (mock demo)."
        )
    # Memory demo: goal contains "remember" → memory_write (e2e verifies the
    # resolver persists with per-space isolation)
    if users and "remember" in goal and not _has_tool_use(messages, "memory_write"):
        return _tool_use(
            "memory_write",
            {
                "name": "user-preference-demo",
                "text": f"User asked to remember: {goal[:120]}",
                "description": "user preference written by the mock demo",
                "type": "user",
            },
        )
    # Delegation demo: goal contains "parallel search" → dispatch explorer in
    # the background
    if users and "parallel search" in goal and not _has_tool_use(messages, "spawn_subagent"):
        return _tool_use(
            "spawn_subagent",
            {
                "spawns": [{"agent": "explorer", "goal": f"search: {goal[:60]}"}],
                "background": True,
            },
        )
    if users and not _has_tool_use(messages, "ask_user_question") and len(users) == 1:
        return _tool_use(
            "ask_user_question",
            {
                "questions": [
                    {
                        "id": "audience",
                        "question": "Who is the primary audience for this report?",
                        "header": "Audience",
                        # choice ids are validated by noeta against
                        # ^[A-Za-z0-9_-]{1,64}$ — ASCII only
                        "choices": [
                            {"id": "eng", "label": "Engineer",
                             "description": "prefers technical detail"},
                            {"id": "pm", "label": "Product manager",
                             "description": "prefers conclusions and impact"},
                        ],
                        "allow_freeform": True,
                    }
                ],
                "reason": "The audience determines the report's depth and framing.",
            },
        )
    if users:
        return _end_turn(
            f'(mock mode) Received your message: "{users[-1][:80]}". A real '
            "model would continue from the context here."
        )
    return _end_turn("(mock mode) Hello, I am noeta-agent.")


def build_mock_provider() -> FakeLLMProvider:
    return FakeLLMProvider(responder=mock_responder)
