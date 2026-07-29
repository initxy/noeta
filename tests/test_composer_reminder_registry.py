"""Track B (D8) — the compose-time reminder registry.

Beyond the byte-identity characterization
(``tests/test_composer_reminders_characterization.py``, which pins that the
three built-in reminders survive the migration unchanged), this module pins the
new mechanism:

* the registry orders by ``(priority, name)`` and rejects duplicate names;
* the loader-resolved base (microkernel M2: the ``reminders`` built-in's specs,
  via ``noeta.client.parts.default_reminder_specs``) IS the three built-ins in
  the byte-identical order;
* a **plugin reminder renders at the dynamic-suffix tail**, after the built-ins,
  when wired through the builder's ``extra_reminders`` seam;
* the **stable-prefix hash is unchanged across steps** with reminders active —
  reminders only ever touch the volatile dynamic suffix, never the cached prefix.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from tests._session_inputs import default_factory_kwargs
from noeta.builtins.reminders.impl import BUILTIN_REMINDER_PRIORITIES
from noeta.client.parts import default_reminder_specs
from noeta.context.reminders import (
    ReminderRegistry,
    ReminderSpec,
    ReminderView,
)
from noeta.execution.builder import COMPACTION_OFF, build_session_inputs
from noeta.runtime.governance import Budget
from noeta.protocols.messages import Message, TextBlock
from noeta.protocols.task import Task
from noeta.storage.memory import InMemoryContentStore


# ---------------------------------------------------------------------------
# Registry mechanics
# ---------------------------------------------------------------------------


def test_registry_orders_by_priority_then_name() -> None:
    """Specs compose in ``(priority, name)`` order regardless of insertion order."""

    def make(tag: str):
        def render(view: ReminderView) -> str:
            return tag

        return render

    registry = ReminderRegistry(
        [
            ReminderSpec("z", 300, make("z")),
            ReminderSpec("b", 100, make("b")),
            ReminderSpec("a", 100, make("a")),
            ReminderSpec("m", 200, make("m")),
        ]
    )
    assert [s.name for s in registry.specs()] == ["a", "b", "m", "z"]
    assert registry.render_all(ReminderView()) == ["a", "b", "m", "z"]


def test_registry_rejects_duplicate_name() -> None:
    with pytest.raises(ValueError, match="duplicate reminder"):
        ReminderRegistry(
            [
                ReminderSpec("dup", 1, lambda v: "x"),
                ReminderSpec("dup", 2, lambda v: "y"),
            ]
        )


def test_render_all_skips_none() -> None:
    """A render returning ``None`` contributes nothing this compose."""
    registry = ReminderRegistry(
        [
            ReminderSpec("on", 1, lambda v: "shown"),
            ReminderSpec("off", 2, lambda v: None),
        ]
    )
    assert registry.render_all(ReminderView()) == ["shown"]


def test_default_specs_are_the_three_builtins() -> None:
    """The loader-resolved base carries exactly the three built-ins,
    todo->delegation->read (microkernel M2: resolved from the ``reminders``
    manifest, never statically imported)."""
    registry = ReminderRegistry(default_reminder_specs())
    assert [s.name for s in registry.specs()] == [
        "unfinished-todos",
        "delegation-nudge",
        "read-suggestion",
    ]
    # priorities keep that order and match the declared table
    assert [s.priority for s in registry.specs()] == [
        BUILTIN_REMINDER_PRIORITIES["unfinished-todos"],
        BUILTIN_REMINDER_PRIORITIES["delegation-nudge"],
        BUILTIN_REMINDER_PRIORITIES["read-suggestion"],
    ]


def test_default_specs_append_extras_by_priority() -> None:
    """A high-priority extra interleaves after the built-ins by its priority."""
    extra = ReminderSpec("plugin-note", 999, lambda v: "NOTE")
    registry = ReminderRegistry((*default_reminder_specs(), extra))
    assert [s.name for s in registry.specs()][-1] == "plugin-note"


# ---------------------------------------------------------------------------
# Composer integration — plugin reminder at the tail; stable prefix unchanged
# ---------------------------------------------------------------------------


def _session(extra_reminders=()):
    workspace = Path(tempfile.mkdtemp(prefix="reminder_registry_"))
    return build_session_inputs(
        **default_factory_kwargs(),
        workspace_dir=workspace,
        system_prompt="You are main.",
        allowed_tools=frozenset({"read"}),
        content_store=InMemoryContentStore(),
        model="stub-model",
        compaction=COMPACTION_OFF,
        budget=Budget(),
        todo_write_enabled=True,
        extra_reminders=tuple(extra_reminders),
    )


def _dynamic_texts(view) -> list[str]:
    dynamic = next(s for s in view.segments if s.name == "dynamic_suffix")
    return [
        "\n".join(b.text for b in m.content if isinstance(b, TextBlock))
        for m in dynamic.content
    ]


def _task_with_todos() -> Task:
    task = Task(task_id="t-reminder")
    task.runtime.messages = [
        Message(role="user", content=[TextBlock(text="Do the task.")])
    ]
    task.state.todos = [{"id": "1", "content": "Step one", "status": "pending"}]
    return task


def test_plugin_reminder_renders_at_dynamic_tail() -> None:
    """A plugin reminder (wired via ``extra_reminders``) renders at the very tail
    of the dynamic suffix, after the built-in reminders."""
    plugin_text = "PLUGIN REMINDER: check the deadline."
    inputs = _session(
        extra_reminders=[ReminderSpec("plugin-note", 999, lambda v: plugin_text)]
    )
    view = inputs.composer.compose(_task_with_todos())
    texts = _dynamic_texts(view)

    # base turn, then the todo reminder, then the plugin reminder LAST.
    assert texts[0] == "Do the task."
    assert "todo list" in texts[1]
    assert texts[-1] == plugin_text


def test_plugin_reminder_absent_is_byte_identical() -> None:
    """No extras ⇒ the dynamic suffix is exactly the built-in-only output."""
    base = _session().composer.compose(_task_with_todos())
    with_empty = _session(extra_reminders=()).composer.compose(_task_with_todos())
    assert _dynamic_texts(base) == _dynamic_texts(with_empty)
    # and the built-in-only tail carries no plugin note
    assert all("PLUGIN" not in t for t in _dynamic_texts(base))


def test_stable_prefix_hash_unchanged_across_steps_with_reminders() -> None:
    """Reminders touch only the dynamic suffix — the stable-prefix hash is
    identical across steps even as reminders fire and change."""
    inputs = _session(
        extra_reminders=[ReminderSpec("plugin-note", 999, lambda v: "NOTE")]
    )
    composer = inputs.composer

    # Step 1: one unfinished todo.
    t1 = _task_with_todos()
    v1 = composer.compose(t1)

    # Step 2: more history + a different todo set + thrashing latched, so the
    # dynamic suffix (and its hash) changes while the prefix must not.
    t2 = _task_with_todos()
    t2.runtime.messages = [
        Message(role="user", content=[TextBlock(text="Do the task.")]),
        Message(role="assistant", content=[TextBlock(text="working")]),
    ]
    t2.state.todos = [
        {"id": "1", "content": "Step one", "status": "in_progress"},
        {"id": "2", "content": "Step two", "status": "pending"},
    ]
    t2.context.compaction_thrashing = True
    v2 = composer.compose(t2)

    def _hash(view, name: str) -> str:
        return next(s for s in view.segments if s.name == name).segment_hash

    # Stable prefix (and semi-stable) hashes are byte-stable across steps.
    assert _hash(v1, "stable_prefix") == _hash(v2, "stable_prefix")
    assert _hash(v1, "semi_stable") == _hash(v2, "semi_stable")
    # The dynamic suffix genuinely changed (reminders active + different state).
    assert _hash(v1, "dynamic_suffix") != _hash(v2, "dynamic_suffix")
    # The plugin reminder is present at the tail on both steps.
    assert _dynamic_texts(v1)[-1] == "NOTE"
    assert _dynamic_texts(v2)[-1] == "NOTE"
