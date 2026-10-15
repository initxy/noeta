"""Claude Code's post-compaction file restore: when a compaction collapses the
prefix, the files most recently read or written come back with their current
content, as the Read call/result pair Claude Code attaches (or its
too-large note), recorded so a resume never reads the disk again.
"""

from __future__ import annotations

import json
from pathlib import Path

from noeta.builtins.fs.impl.read import ReadFileTool
from noeta.builtins.react.impl import ReActPolicy
from noeta.builtins.react.impl.react import (
    POST_COMPACT_MAX_FILES_TO_RESTORE,
    POST_COMPACT_MAX_TOKENS_PER_FILE,
    POST_COMPACT_TOKEN_BUDGET,
)
from noeta.context.composer import ThreeSegmentComposer
from noeta.core.engine import Engine
from noeta.core.fold import fold
from noeta.core.hooks import HookManager
from noeta.core.wiring import wire_default_observers
from noeta.policies.stub import StubScriptedPolicy
from noeta.protocols.decisions import CompactionRequestedDecision, FinishDecision
from noeta.protocols.messages import (
    LLMResponse,
    Message,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
)
from noeta.protocols.step_context import StepContext
from noeta.protocols.task import Task
from noeta.protocols.tool import ToolContext
from noeta.runtime.llm import RuntimeLLMClient
from noeta.runtime.tool import ToolRuntime
from noeta.runtime.workspace import WorkspaceRoot
from noeta.storage.memory import (
    InMemoryContentStore,
    InMemoryDispatcher,
    InMemoryEventLog,
)
from noeta.testing.composer import trivial_three_segment
from noeta.testing.fake_llm import FakeLLMProvider

_NOTE = (
    "1. Primary Request & Intent: keep working the task.\n"
    "6. Pending Tasks: finish it."
)


def test_budgets_are_claude_codes() -> None:
    assert POST_COMPACT_MAX_FILES_TO_RESTORE == 5
    assert POST_COMPACT_MAX_TOKENS_PER_FILE == 5_000
    assert POST_COMPACT_TOKEN_BUDGET == 50_000


def _call(i: int, tool: str, path: str, *, ok: bool = True) -> list[Message]:
    return [
        Message(
            role="assistant",
            content=[
                ToolUseBlock(call_id=f"c{i}", tool_name=tool, arguments={"file_path": path})
            ],
        ),
        Message(
            role="tool",
            content=[ToolResultBlock(call_id=f"c{i}", output="", success=ok)],
        ),
    ]


def _policy(tmp_path: Path, *, tail: int = 100_000) -> ReActPolicy:
    return ReActPolicy(
        llm=None,  # type: ignore[arg-type]
        tools={"Read": ReadFileTool(workspace=WorkspaceRoot.from_path(tmp_path))},
        system_prompt="sys",
        model="m",
        content_store=InMemoryContentStore(),
        tail_token_budget=tail,
    )


def _pair(path: Path) -> list[str]:
    body = ReadFileTool(workspace=WorkspaceRoot.from_path(path.parent)).invoke(
        {"file_path": str(path)},
        ToolContext(artifact_store=InMemoryContentStore()),
    ).output
    return [
        "Called the Read tool with the following input: "
        + json.dumps({"file_path": str(path)}, separators=(",", ":")),
        f"Result of calling the Read tool:\n{body}",
    ]


def test_newest_files_come_back_as_claude_codes_pairs(tmp_path: Path) -> None:
    for name in ("a", "b", "c", "d", "e", "f", "g", "kept", "AGENTS.md"):
        (tmp_path / name).write_text(f"content of {name}\n")
    history: list[Message] = [Message(role="user", content=[TextBlock(text="go")])]
    for i, name in enumerate(("a", "b", "c", "d", "e", "f", "AGENTS.md")):
        history += _call(i, "Read", str(tmp_path / name))
    history += _call(20, "Edit", str(tmp_path / "g"))
    history += _call(21, "Read", str(tmp_path / "missing"), ok=False)
    boundary = len(history)
    history += _call(30, "Read", str(tmp_path / "kept"))

    texts = _policy(tmp_path)._recent_files_to_reattach(history, boundary)

    expected: list[str] = []
    for name in ("g", "f", "e", "d", "c"):
        expected += _pair(tmp_path / name)
    assert list(texts) == expected


def test_a_large_file_comes_back_as_a_note(tmp_path: Path) -> None:
    big = tmp_path / "big.txt"
    big.write_text("".join(f"line {i} " + "x" * 80 + "\n" for i in range(1000)))
    history = [Message(role="user", content=[TextBlock(text="go")])]
    history += _call(0, "Read", str(big))
    texts = _policy(tmp_path)._recent_files_to_reattach(history, len(history))
    assert texts == (
        f"Note: {big} was read before the last conversation was summarized, "
        "but the contents are too large to include. Use Read tool if you need "
        "to access it.",
    )


def test_the_restore_stays_within_the_protected_tail(tmp_path: Path) -> None:
    (tmp_path / "a").write_text("a" * 2000)
    (tmp_path / "b").write_text("b\n")
    history = [Message(role="user", content=[TextBlock(text="go")])]
    history += _call(0, "Read", str(tmp_path / "b"))
    history += _call(1, "Read", str(tmp_path / "a"))
    texts = _policy(tmp_path, tail=200)._recent_files_to_reattach(history, len(history))
    # ``a`` (newest) does not fit; ``b`` still does.
    assert list(texts) == _pair(tmp_path / "b")


def test_compaction_carries_the_restore(tmp_path: Path) -> None:
    (tmp_path / "notes.md").write_text("hello\n")
    store = InMemoryContentStore()
    composer = ThreeSegmentComposer(
        system_prompt="sys", tools={}, content_store=store, tail_token_budget=300
    )
    task = Task(task_id="t-1")
    task.runtime.messages = [
        Message(role="user", content=[TextBlock(text="go")]),
        *_call(0, "Read", str(tmp_path / "notes.md")),
        *[
            Message(role="user", content=[TextBlock(text=f"m{i}-" + "x" * 400)])
            for i in range(12)
        ],
    ]
    view = composer.compose(task)
    provider = FakeLLMProvider(
        responses=[LLMResponse(stop_reason="end_turn", content=[TextBlock(text=_NOTE)], usage=Usage(uncached=1, output=1))]
    )
    policy = ReActPolicy(
        llm=RuntimeLLMClient(provider=provider, event_log=InMemoryEventLog(), content_store=store),
        tools={"Read": ReadFileTool(workspace=WorkspaceRoot.from_path(tmp_path))},
        system_prompt="sys",
        model="gpt-4o",
        content_store=store,
        context_window=600,
        max_output_tokens=50,
        compaction_buffer=50,
        tail_token_budget=200,
    )
    decision = policy.decide(StepContext(task_id="t-1", lease_id="l", trace_id="tr"), view)
    assert isinstance(decision, CompactionRequestedDecision)
    assert decision.reattached == tuple(_pair(tmp_path / "notes.md"))


def test_reattached_texts_are_recorded_as_injected_turns() -> None:
    dispatcher = InMemoryDispatcher()
    store = InMemoryContentStore()
    log = InMemoryEventLog(lease_validator=dispatcher)
    wire_default_observers(log, dispatcher)
    engine = Engine(
        event_log=log,
        content_store=store,
        composer=trivial_three_segment(store),
        policy=StubScriptedPolicy(
            [
                CompactionRequestedDecision(
                    reason="proactive",
                    summary="condensed",
                    boundary_count=1,
                    reattached=("call text", "result text"),
                ),
                FinishDecision(answer="done"),
            ]
        ),
        tools={},
        tool_runtime=ToolRuntime(event_log=log, content_store=store),
        hooks=HookManager(),
    )
    task = engine.create_task(goal="g", policy_name="scripted")
    dispatcher.enqueue(task.task_id)
    lease = dispatcher.lease(worker_id="w")
    assert lease is not None
    final = engine.run_one_step(task, lease_id=lease.lease_id)
    restored = [m for m in final.runtime.messages if m.origin == "system"]
    assert [m.content for m in restored] == [
        [TextBlock(text="call text")],
        [TextBlock(text="result text")],
    ]
    assert all(m.role == "user" for m in restored)
    # A resume folds the same turns back from the log.
    refolded = fold(log, store, task.task_id)
    assert [m for m in refolded.runtime.messages if m.origin == "system"] == restored
