"""Goldens for the three built-in compose-time reminders.

Reminder text is model-facing prose that no other assertion reads, so a
one-word edit changes what every session's model sees while every behavioural
test stays green. These goldens are the only thing that makes such an edit
deliberate. The three are:

* **unfinished-todos** — surfaces the unfinished ``TaskState.todos`` the model
  would otherwise never see again;
* **delegation-nudge** — the just-in-time fan-out note, live only while
  delegation is offered AND no ``spawn_subagent`` has landed yet;
* **read-suggestion** — the "different reading strategy" hint, live only while
  ``ContextState.compaction_thrashing`` is latched.

The test runs the **real** assembly path (``build_session_inputs`` →
``ThreeSegmentComposer.compose``) over representative folded states and pins the
whole ``dynamic_suffix`` — the fixed base user turn followed by whichever
reminders fire, in the exact order they are appended (todo → delegation →
read). Because the base message is fixed and the Task activates no
skills/memory/environment residents, the dynamic suffix contains nothing but
the base turn and the reminders, so the golden captures the rendered text AND
the relative order in one artifact.

Re-pin (regenerate goldens) with one command::

    UPDATE_SNAPSHOTS=1 uv run pytest \\
        tests/test_composer_reminders_characterization.py -q -p no:cacheprovider
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from tests._session_inputs import default_factory_kwargs
from noeta.execution.builder import COMPACTION_OFF, build_session_inputs
from noeta.runtime.governance import Budget
from noeta.protocols.messages import Message, TextBlock, ToolUseBlock
from noeta.protocols.task import Task
from noeta.storage.memory import InMemoryContentStore

from tests._snapshot import assert_snapshot, stable_json


# The fixed base user turn — the only real message in the rolling history, so
# every message that follows it in the composed dynamic suffix is a reminder.
_BASE_GOAL = "Do the task."

# Representative unfinished + finished todos. Only the two non-completed items
# feed the todo reminder; the completed one must NOT appear (a finished item
# does not nag) — the golden pins that filtering.
_TODOS = [
    {"id": "1", "content": "First step", "status": "pending"},
    {"id": "2", "content": "Already done", "status": "completed"},
    {"id": "3", "content": "Second step", "status": "in_progress"},
]

# A prior spawn_subagent call in history — suppresses the delegation nudge
# (self-limiting once the first sub-agent has been spawned).
_SPAWN_HISTORY_BLOCK = ToolUseBlock(
    call_id="c1", tool_name="spawn_subagent", arguments={"agent": "explore"}
)


def _dynamic_suffix_payload(
    *,
    todos: list[dict[str, str]],
    delegation_enabled: bool,
    already_spawned: bool,
    compaction_thrashing: bool,
) -> list[dict[str, object]]:
    """Compose a Task in the given folded state; return its dynamic suffix.

    The composer is built through the real ``build_session_inputs`` path.
    ``delegation_enabled`` drives whether the ``spawn_subagent`` control schema
    is injected (the condition the concurrency reminder gates on). Every
    reminder is a View-only product appended to the dynamic suffix — never
    written to ``runtime.messages`` — so composing is a pure read of the folded
    state passed in.
    """
    workspace = Path(tempfile.mkdtemp(prefix="composer_reminders_"))
    content_store = InMemoryContentStore()

    inputs = build_session_inputs(
        **default_factory_kwargs(),
        workspace_dir=workspace,
        system_prompt="You are main.",
        allowed_tools=frozenset({"read"}),
        content_store=content_store,
        model="stub-model",
        compaction=COMPACTION_OFF,
        budget=Budget(),
        allowed_subtask_agents=frozenset({"explore"}) if delegation_enabled else frozenset(),
        capability_flags={
            "delegation": delegation_enabled,
            "todo_write": True,
        },
        subtask_agent_directory=(("explore", ""),) if delegation_enabled else (),
    )

    base = Message(role="user", content=[TextBlock(text=_BASE_GOAL)])
    history: list[Message] = [base]
    if already_spawned:
        history.append(
            Message(role="assistant", content=[_SPAWN_HISTORY_BLOCK])
        )

    task = Task(task_id="t-reminders")
    task.runtime.messages = list(history)
    task.state.todos = list(todos)
    task.context.compaction_thrashing = compaction_thrashing

    view = inputs.composer.compose(task)
    dynamic = next(s for s in view.segments if s.name == "dynamic_suffix")
    return [
        {
            "role": message.role,
            "origin": getattr(message, "origin", None),
            "text": "\n".join(
                block.text
                for block in message.content
                if isinstance(block, TextBlock)
            ),
        }
        for message in dynamic.content
    ]


#: Representative folded states, label → the four state knobs. Each label pins a
#: distinct combination: all three reminders together (order), each reminder in
#: isolation, and the two suppression paths (finished checklist; spawn already
#: landed).
_STATES: dict[str, dict[str, object]] = {
    # All three fire — pins the relative order todo -> delegation -> read.
    "all_three": {
        "todos": _TODOS,
        "delegation_enabled": True,
        "already_spawned": False,
        "compaction_thrashing": True,
    },
    # Only the unfinished-todos reminder (delegation off, no thrashing).
    "todos_only": {
        "todos": _TODOS,
        "delegation_enabled": False,
        "already_spawned": False,
        "compaction_thrashing": False,
    },
    # Only the delegation nudge (no todos, no thrashing, not yet spawned).
    "delegation_only": {
        "todos": [],
        "delegation_enabled": True,
        "already_spawned": False,
        "compaction_thrashing": False,
    },
    # Only the read-suggestion reminder (thrashing latched, nothing else).
    "read_only": {
        "todos": [],
        "delegation_enabled": False,
        "already_spawned": False,
        "compaction_thrashing": True,
    },
    # Suppression: delegation offered but a spawn already landed => no nudge;
    # nothing else fires => the dynamic suffix is the base turn alone.
    "suppressed": {
        "todos": [],
        "delegation_enabled": True,
        "already_spawned": True,
        "compaction_thrashing": False,
    },
}


@pytest.mark.parametrize("label", sorted(_STATES))
def test_composer_reminders_golden(label: str) -> None:
    """The composed dynamic suffix (base turn + fired reminders, in order)
    matches the golden for this folded state."""
    payload = stable_json(_dynamic_suffix_payload(**_STATES[label]))  # type: ignore[arg-type]
    assert_snapshot(f"composer_reminders_{label}.txt", payload)


def test_all_three_relative_order_is_todo_delegation_read() -> None:
    """Explicit order assertion, independent of the golden bytes.

    Priorities are what put the three in the order todo → delegation → read; a
    priority edit that re-orders them should fail as an ordering error here,
    not merely as an unreadable byte diff in the golden.
    """
    suffix = _dynamic_suffix_payload(**_STATES["all_three"])  # type: ignore[arg-type]
    # Base turn first (author-neutral), then exactly three system reminders.
    assert suffix[0]["origin"] is None
    reminders = suffix[1:]
    assert [m["origin"] for m in reminders] == ["system", "system", "system"]
    assert "todo list" in reminders[0]["text"]
    assert "spawn_subagent" in reminders[1]["text"]
    assert "reading strategy" in reminders[2]["text"]
