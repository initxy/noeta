"""Golden snapshot of a preset's **composed View** — the assembled prompt.

The composer-side companion to ``test_prompt_snapshot.py``. That suite pins the
*static* spec inputs (``spec.instructions`` + the tool schemas) without ever
running the composer, so it cannot see drift introduced by the **assembly**
step itself:

* **control-tool schema injection** — ``spawn_subagent`` / ``todo_write`` /
  ``ask_user_question`` (and friends) are NOT executable workspace tools; they
  are appended to ``View.provider_tool_schemas`` after the real tools
  (``noeta.execution.builder._run_control_tool_mounts``, the dual-priority mount
  loop). A flag flip or a re-ordering there is invisible to the static-spec
  snapshot and visible here.
* **three-segment assembly** — the system prompt becomes ``stable_prefix``, the
  content channel renders ``semi_stable``, the message stream becomes
  ``dynamic_suffix``, and each segment carries a ``segment_hash``. A change to
  how segments are cut or hashed shows up as a golden diff.

``main`` and ``explore`` are composed through the path a host actually runs:
a :class:`~noeta.client.Client` built from ``main_options()`` with memory
mounted, driven by a fake provider for one turn (``explore`` is the sub-agent
main spawns in that turn). The View is the one the composer hands the Policy
for each task's first step, so the golden locks the real default prefix —
memory tools, ``skill``, ``RecallHistory`` and the rest included — not a hand
assembled subset. ``plan`` keeps the direct
``noeta.execution.builder.build_session_inputs`` path (one user message, no
residents). The composer never calls the LLM, so the assembly is exercised
without a network round-trip.

Determinism, and why the golden needs no normalization:

* ``model="stub-model"`` keeps the catalog-driven compaction OFF, so no
  per-model token math enters the bytes.
* The workspace-environment resident (absolute workspace path, platform,
  date) is switched off (``HostConfig.environment_enabled=False``) and ``HOME``
  points at an empty temp directory, so no per-call path, clock or user-level
  skill leaks into the View; the memory store is an empty temp directory.
* The View is serialized with ``to_canonical`` (the deterministic, key-sorted,
  object-id-free encoder used everywhere else), so no Python object ids /
  addresses / timestamps reach the golden.

Coverage is the whole model-visible composed surface: the complete three
segments (content + per-segment hash) plus the full ``provider_tool_schemas``
list, control-tool schemas included.

Re-pin (regenerate goldens) with one command::

    UPDATE_SNAPSHOTS=1 uv run pytest \\
        tests/test_composer_view_snapshot.py -q -p no:cacheprovider
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from tests._session_inputs import default_factory_kwargs
from noeta.agent.spec import agent_activates
from noeta.client import Client
from noeta.client.host_config import HostConfig
from noeta.context import composer as composer_module
from noeta.presets import main_options
from noeta.protocols.messages import LLMRequest, LLMResponse, ToolUseBlock, Usage
from noeta.protocols.view import View
from noeta.testing.fake_llm import FakeLLMProvider
from noeta.execution.builder import COMPACTION_OFF, build_session_inputs
from noeta.runtime.governance import Budget
from noeta.presets import official_specs
from noeta.protocols.canonical import to_canonical
from noeta.protocols.messages import Message, TextBlock
from noeta.protocols.task import Task
from noeta.storage.memory import InMemoryContentStore

from tests._snapshot import assert_snapshot, stable_json


# ``general-purpose`` is not snapshotted: its tool set is identical to
# ``main``'s and it injects no control schemas, so its View adds no assembly
# coverage the others don't already give.

# The fixed minimal Task fed to every composer: one fixed goal carried as a
# single user message. Constant so the dynamic_suffix bytes are stable.
_FIXED_GOAL = "Fixed goal: say hi"


#: The prompt main hands the ``explore`` sub-agent in the driven turn.
_EXPLORE_PROMPT = "Fixed explore prompt: list the files"


def _view_payload(preset: str, view: View) -> dict[str, object]:
    return {
        "preset": preset,
        # The complete three segments: content (canonicalized — no object ids)
        # and the per-segment hash.
        "segments": [
            {
                "name": segment.name,
                "segment_hash": segment.segment_hash,
                "content": [to_canonical(message) for message in segment.content],
            }
            for segment in view.segments
        ],
        # The full provider tool surface: real executable tools followed by the
        # injected control-action schemas (spawn_subagent / todo_write / ...).
        "provider_tool_schemas": view.provider_tool_schemas,
    }


def _end(text: str) -> LLMResponse:
    return LLMResponse(
        stop_reason="end_turn",
        content=[TextBlock(text=text)],
        usage=Usage(uncached=1, output=1),
    )


def _client_views(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[View, View]:
    """Drive one turn of the official main recipe through ``Client`` and
    return the first composed View of main and of the explore sub-agent it
    spawns."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    workspace = tmp_path / "ws"
    workspace.mkdir()
    # One fixed workspace skill, so the ``skill`` control tool (offered only
    # when a skill exists) and its roster are part of the locked prefix.
    skill_dir = workspace / ".noeta" / "skills" / "fixed-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: fixed-skill\ndescription: Use when the user asks for the "
        "fixed workflow.\n---\n\n# Fixed skill\n\nStep one.\n"
    )
    memory = tmp_path / "memory"
    memory.mkdir()

    views: list[tuple[str, View]] = []
    original = composer_module.ThreeSegmentComposer.compose

    def _recording_compose(self, task):  # type: ignore[no-untyped-def]
        view = original(self, task)
        views.append((task.task_id, view))
        return view

    monkeypatch.setattr(
        composer_module.ThreeSegmentComposer, "compose", _recording_compose
    )

    main_steps = iter(
        [
            LLMResponse(
                stop_reason="tool_use",
                content=[
                    ToolUseBlock(
                        call_id="spawn-1",
                        tool_name="Task",
                        arguments={
                            "description": "List files",
                            "prompt": _EXPLORE_PROMPT,
                            "subagent_type": "explore",
                        },
                    )
                ],
                usage=Usage(uncached=1, output=1),
            ),
            _end("main done"),
        ]
    )

    def _responder(req: LLMRequest) -> LLMResponse:
        first_user = "".join(
            b.text
            for m in req.messages
            if m.role == "user" and m.origin is None
            for b in m.content
            if isinstance(b, TextBlock)
        )
        if _EXPLORE_PROMPT in first_user and not any(
            isinstance(b, ToolUseBlock) for m in req.messages for b in m.content
        ):
            return _end("explore done")
        return next(main_steps)

    client = Client(
        main_options(),
        provider=FakeLLMProvider(responder=_responder),
        workspace_dir=workspace,
        model="stub-model",
        host_config=HostConfig(
            memory_dir=memory,
            global_memory_dir=memory,
            environment_enabled=False,
        ),
    )
    try:
        root = client.start(goal=_FIXED_GOAL).task_id
    finally:
        client.shutdown()

    main_view = next(v for tid, v in views if tid == root)
    explore_view = next(v for tid, v in views if tid != root)
    return main_view, explore_view


def _compose_view_payload(preset: str) -> dict[str, object]:
    """Build ``preset``'s composer through the real assembly path and compose a
    fixed minimal Task, returning the stable serialization of the View.

    Wiring mirrors ``official_specs()[preset]``'s activation tuple so the
    control-tool schema injection matches what the live session would emit
    (delegation / todo_write / ask_user_question / skill_invocation flags + the
    spawnable sub-agent directory). ``model="stub-model"`` keeps compaction off
    and the tool schemas free of any provider-edit drop.
    """
    spec = official_specs()[preset]
    allowed = frozenset(t.name for t in spec.tools)

    # A throwaway temp workspace: nothing is read from it (no skills/memory)
    # and its absolute path never reaches the composed bytes, so the golden
    # needs no path normalization.
    workspace = Path(tempfile.mkdtemp(prefix="composer_view_snapshot_"))
    content_store = InMemoryContentStore()

    inputs = build_session_inputs(
        **default_factory_kwargs(),
        workspace_dir=workspace,
        system_prompt=spec.instructions,
        allowed_tools=allowed,
        content_store=content_store,
        model="stub-model",
        compaction=COMPACTION_OFF,
        budget=Budget(),
        allowed_subtask_agents=frozenset(spec.spawnable),
        capability_flags={
            "delegation": agent_activates(spec, "delegation"),
            "todo_write": agent_activates(spec, "todo_write"),
            "ask_user_question": agent_activates(spec, "ask_user_question"),
            "skill_invocation": agent_activates(spec, "skill_invocation"),
        },
        # The spawn_subagent control schema embeds the sub-agent directory
        # (name + description). Descriptions are pinned by test_prompt_snapshot;
        # here we pass empty descriptions so this golden stays focused on the
        # injection + ordering, not the descriptive prose.
        subtask_agent_directory=tuple((name, "") for name in spec.spawnable),
    )

    task = Task(task_id="t-fixed")
    task.runtime.messages = [
        Message(role="user", content=[TextBlock(text=_FIXED_GOAL)])
    ]
    view = inputs.composer.compose(task)
    return _view_payload(preset, view)


@pytest.mark.parametrize("preset", ("plan",))
def test_composed_view_snapshot(preset: str) -> None:
    """The preset's composed View (three segments + provider tool schemas,
    control schemas included) matches its golden."""
    payload = stable_json(_compose_view_payload(preset))
    assert_snapshot(f"composer_view_{preset}.txt", payload)


def test_client_composed_views_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """main's and explore's first composed View, as a ``Client`` running the
    official recipe builds them, match their goldens — the real default
    prefix, not a hand-assembled tool subset."""
    main_view, explore_view = _client_views(tmp_path, monkeypatch)
    assert_snapshot(
        "composer_view_main.txt", stable_json(_view_payload("main", main_view))
    )
    assert_snapshot(
        "composer_view_explore.txt",
        stable_json(_view_payload("explore", explore_view)),
    )
