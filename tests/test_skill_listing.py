"""The skill listing: the roster out of the ``skill`` schema, into residents.

``docs/implementation-specs/skill-listing-as-resident.md``, acceptance
criteria 1–7, driven through the production ``SdkHost`` / ``InteractionDriver``
assembly, plus the unit surface of ``skills.impl.listing``:

1. the ``skill`` schema is the same bytes for every task and every turn;
2. a task's opening turn records one ``skill_listing:roster`` pre-loop, and it
   renders in ``semi_stable``;
3. a skill installed mid-task is announced by one ``added-<k>`` resident at
   its anchor, nothing earlier moves; a removal or an edit records nothing;
4. a fresh host over the same store composes byte-identical messages and
   announces only what no recorded listing names;
5. a compaction that covers an anchored listing re-hangs it after the summary;
6. a task that carries no listing resident gets ``roster`` at its next goal,
   anchored there;
7. a child task gets its own listing exactly when it activates
   ``skill_invocation``.
"""

from __future__ import annotations

import dataclasses
import json
import shutil
from pathlib import Path
from typing import Any, Optional

from noeta.context.composer import COMPOSER_VERSION, _SUMMARY_FRAME
from noeta.core.fold import fold
from noeta.execution.reminders import RecallView, ResidentActivation
from noeta.policies.control_semantics import SPAWN_SUBAGENT_TOOL
from noeta.protocols.canonical import to_canonical_bytes
from noeta.protocols.errors import ContentNotFound
from noeta.protocols.events import CompactedPayload
from noeta.protocols.messages import (
    LLMResponse,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
)
from noeta.protocols.task import TaskState
from noeta.runtime.shell_policy import ShellMode
from noeta.runtime.workspace import FsWriteMode
from noeta.storage.memory import InMemoryContentStore
from noeta.testing.fake_llm import FakeLLMProvider
from noeta.builtins.skills.impl import SKILL_TOOL
from noeta.builtins.skills.impl.control_tool import short_summary
from noeta.builtins.skills.impl.listing import (
    ROSTER_SLOT,
    SKILL_LISTING_KIND,
    SkillRoster,
    announced_skill_names,
    build_listing_init_hook,
    decode_listing,
    encode_listing,
    render_listing,
    skill_listing_content_kind,
    skill_listing_provider,
)

from tests._sdk_session import (
    make_driver,
    make_host,
    make_registry,
    preset_spec,
    runner_main_spec,
)
from tests._skill_fixtures import (
    LISTING_ADDED_HEADER,
    LISTING_ROSTER_HEADER,
    CapturingRecorder,
    listing_entries,
    listing_texts,
    write_skill,
)


# ===========================================================================
# Unit: body encoding, rendering, the init hook, the provider
# ===========================================================================


def test_encode_decode_round_trip_and_canonical_bytes() -> None:
    lines = (("alpha", "- alpha: first"), ("中文", "- 中文: 技能"))
    raw = encode_listing(lines)
    assert decode_listing(raw) == (("alpha", "中文"), ("- alpha: first", "- 中文: 技能"))
    # Canonical: same roster, same bytes; non-ASCII stays literal UTF-8.
    assert encode_listing(list(lines)) == raw
    assert "中文".encode("utf-8") in raw
    assert json.loads(raw) == {
        "lines": ["- alpha: first", "- 中文: 技能"],
        "names": ["alpha", "中文"],
    }
    assert decode_listing(encode_listing(())) == ((), ())


def test_render_listing_headers() -> None:
    assert render_listing("roster", ["- a: x", "- b"]) == (
        f"{LISTING_ROSTER_HEADER}\n- a: x\n- b"
    )
    assert render_listing("added-3", ["- c"]) == f"{LISTING_ADDED_HEADER}\n- c"


def _resolver(bodies: dict[str, bytes]) -> Any:
    def resolve(kind: str, name: str) -> bytes:
        assert kind == SKILL_LISTING_KIND
        if name not in bodies:
            raise ContentNotFound(name)
        return bodies[name]

    return resolve


def _texts(rendered: Any) -> list[str]:
    return [m.content[0].text for m in rendered.messages]


def test_renderer_orders_roster_then_added_by_number() -> None:
    """``roster`` first, then ``added-<k>`` by ``k`` — ``added-10`` after
    ``added-2``, which string order would get wrong."""
    spec = skill_listing_content_kind()
    assert spec.kind == SKILL_LISTING_KIND
    bodies = {
        "roster": encode_listing((("a", "- a"),)),
        "added-2": encode_listing((("b", "- b"),)),
        "added-10": encode_listing((("c", "- c"),)),
    }
    rendered = spec.renderer(["added-10", "roster", "added-2"], _resolver(bodies))
    assert _texts(rendered) == [
        f"{LISTING_ROSTER_HEADER}\n- a",
        f"{LISTING_ADDED_HEADER}\n- b",
        f"{LISTING_ADDED_HEADER}\n- c",
    ]
    assert rendered.selected_skills == []
    assert all(m.role == "user" for m in rendered.messages)


def test_renderer_skips_missing_and_empty_bodies() -> None:
    spec = skill_listing_content_kind()
    bodies = {"roster": encode_listing((("a", "- a"),)), "added-1": encode_listing(())}
    rendered = spec.renderer(["roster", "added-0", "added-1"], _resolver(bodies))
    assert _texts(rendered) == [f"{LISTING_ROSTER_HEADER}\n- a"]
    assert spec.renderer(["added-0"], _resolver({})).messages == []


def test_init_hook_records_roster_activate_once_and_nothing_when_empty() -> None:
    store = InMemoryContentStore()
    lines = (("a", "- a: x"),)
    rec = CapturingRecorder()
    build_listing_init_hook(lines, store)(rec)
    assert len(rec.records) == 1
    kind, name, ref, refresh = rec.records[0]
    assert (kind, name, refresh) == (SKILL_LISTING_KIND, "roster", False)
    assert store.get(ref) == encode_listing(lines)

    empty = CapturingRecorder()
    build_listing_init_hook((), store)(empty)
    assert empty.records == []


def _put(store: InMemoryContentStore, lines: tuple[tuple[str, str], ...]) -> str:
    return store.put(encode_listing(lines), media_type="application/json").hash


def _view(task_id: Optional[str], active: dict[str, dict[str, str]]) -> RecallView:
    state = TaskState(goal="g")
    state.active_content = active
    return RecallView(
        task_id=task_id, message=(), task_state=state, workspace_path=None
    )


def _slots(lines: Optional[tuple[tuple[str, str], ...]]) -> Any:
    roster = SkillRoster()
    if lines is not None:
        roster.note_built(lines)
    table = {("t1", ROSTER_SLOT): roster}
    return lambda task_id, name: table.get((task_id, name))


_AB = (("a", "- a: x"), ("b", "- b: y"))


def test_announced_names_union_every_active_listing() -> None:
    store = InMemoryContentStore()
    active = {
        "roster": _put(store, (("a", "- a"),)),
        "added-0": _put(store, (("b", "- b"), ("c", "- c"))),
        "added-1": "f" * 64,  # bytes gone from the store: contributes nothing
    }
    assert announced_skill_names(active, store) == frozenset({"a", "b", "c"})
    assert announced_skill_names({}, store) == frozenset()


def test_provider_silent_without_roster_resident_slot_or_newcomer() -> None:
    store = InMemoryContentStore()
    roster_hash = _put(store, (("a", "- a: x"),))
    with_roster = {SKILL_LISTING_KIND: {"roster": roster_hash}}
    # No roster resident yet: the init hook records that one, not the provider.
    assert skill_listing_provider(_slots(_AB), store)(_view("t1", {})) == ()
    # No slot bound for the task, or a slot no build has filled.
    assert skill_listing_provider(lambda t, n: None, store)(_view("t1", with_roster)) == ()
    assert skill_listing_provider(_slots(None), store)(_view("t1", with_roster)) == ()
    # No task id at this seam.
    assert skill_listing_provider(_slots(_AB), store)(_view(None, with_roster)) == ()
    # Nothing joined — and a removal (the slot lost ``a``) is not announced.
    assert skill_listing_provider(_slots((("a", "- a: x"),)), store)(
        _view("t1", with_roster)
    ) == ()
    assert skill_listing_provider(_slots(()), store)(_view("t1", with_roster)) == ()
    # An edited summary of an announced skill is not announced either.
    assert skill_listing_provider(_slots((("a", "- a: CHANGED"),)), store)(
        _view("t1", with_roster)
    ) == ()


def test_provider_announces_only_newcomers_with_the_next_number() -> None:
    store = InMemoryContentStore()
    active = {
        SKILL_LISTING_KIND: {
            "roster": _put(store, (("a", "- a: x"),)),
            "added-0": _put(store, (("b", "- b: y"),)),
            "added-1": _put(store, (("c", "- c"),)),
        }
    }
    current = (("a", "- a: x"), ("b", "- b: y"), ("c", "- c"), ("d", "- d: new"))
    out = skill_listing_provider(_slots(current), store)(_view("t1", active))
    assert len(out) == 1
    activation = out[0]
    assert isinstance(activation, ResidentActivation)
    assert (activation.kind, activation.name) == (SKILL_LISTING_KIND, "added-2")
    assert activation.refresh is False
    assert activation.media_type == "application/json"
    assert decode_listing(activation.body) == (("d",), ("- d: new",))
    # The first addition after the roster is ``added-0``.
    only_roster = {SKILL_LISTING_KIND: {"roster": active[SKILL_LISTING_KIND]["roster"]}}
    first = skill_listing_provider(_slots(_AB), store)(_view("t1", only_roster))
    assert [a.name for a in first] == ["added-0"]
    assert decode_listing(first[0].body)[0] == ("b",)


# ===========================================================================
# Host-level: the production assembly
# ===========================================================================


def _end(text: str = "done") -> LLMResponse:
    return LLMResponse(
        stop_reason="end_turn",
        content=[TextBlock(text=text)],
        usage=Usage(uncached=1, output=1),
        raw={"id": text},
    )


def _call(call_id: str, name: str, args: dict[str, Any]) -> LLMResponse:
    return LLMResponse(
        stop_reason="tool_use",
        content=[ToolUseBlock(call_id=call_id, tool_name=name, arguments=args)],
        usage=Usage(uncached=1, output=1),
        raw={"id": call_id},
    )


def _host(
    ws: Path,
    responses: list[LLMResponse],
    *,
    spec: Any = None,
    registry: Any = None,
    multi_turn: bool = True,
    **knobs: Any,
):
    provider = FakeLLMProvider(responses=list(responses))
    host = make_host(
        registry or make_registry(spec or runner_main_spec("main")),
        workspace_dir=ws,
        provider=provider,
        model="stub-model",
        multi_turn=multi_turn,
        write_mode=FsWriteMode.DRY_RUN,
        shell_mode=ShellMode.OFF,
        require_approval_tools=(),
        **knobs,
    )
    return host, make_driver(host), provider


def _ws(tmp_path: Path, *skills: tuple[str, str]) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    for name, desc in skills:
        write_skill(ws, name, desc)
    return ws


def _skill_schema(request: Any) -> Optional[str]:
    for tool in request.tools:
        if (tool.get("name") or tool["function"]["name"]) == SKILL_TOOL:
            return json.dumps(tool, sort_keys=True)
    return None


def _recorded(host: Any, task_id: str) -> list[tuple[str, int]]:
    """``(resident name, event index)`` of every ``skill_listing`` record."""
    return [
        (e.payload.name, i)
        for i, e in enumerate(host.event_log.read(task_id))
        if e.type == "ContextContentRecorded" and e.payload.kind == SKILL_LISTING_KIND
    ]


def _names(host: Any, task_id: str) -> list[str]:
    return [name for name, _ in _recorded(host, task_id)]


def _compose(host: Any, task_id: str) -> Any:
    task = fold(host.event_log, host.content_store, task_id)
    return host.resolve_engine(task)._composer.compose(task), task


def _segment(view: Any, name: str) -> Any:
    return next(s for s in view.segments if s.name == name)


def _segment_texts(view: Any, name: str) -> list[str]:
    return [
        "".join(getattr(b, "text", "") or "" for b in m.content)
        for m in _segment(view, name).content
    ]


def _canonical_messages(view: Any) -> bytes:
    return to_canonical_bytes([s.content for s in view.segments])


# --- 1 ---------------------------------------------------------------------


def test_skill_schema_bytes_never_move(tmp_path: Path) -> None:
    """Two tasks with different rosters, and one task across an install, a
    removal and an edit, all see one ``skill`` schema."""
    ws = _ws(tmp_path, ("alpha", "the first skill"))
    host, driver, provider = _host(ws, [_end(f"t{i}") for i in range(6)])
    one = driver.start(goal="one", agent="main")
    write_skill(ws, "beta", "installed")
    driver.start(goal="two", agent="main")  # a second task, a bigger roster
    driver.send_goal(one.task_id, goal="after install")
    write_skill(ws, "beta", "edited summary")
    driver.send_goal(one.task_id, goal="after edit")
    shutil.rmtree(ws / ".noeta" / "skills" / "alpha")
    driver.send_goal(one.task_id, goal="after removal")

    schemas = {_skill_schema(r) for r in provider.received_requests}
    assert len(provider.received_requests) == 5
    assert len(schemas) == 1 and None not in schemas
    assert "alpha" not in next(iter(schemas)) and "beta" not in next(iter(schemas))


# --- 2 ---------------------------------------------------------------------


def test_opening_turn_records_one_roster_preloop_in_semi_stable(
    tmp_path: Path,
) -> None:
    ws = _ws(tmp_path, ("zeta", "last"), ("alpha", "first"), ("bare", ""))
    host, driver, provider = _host(ws, [_end()])
    out = driver.start(goal="go", agent="main")

    records = _recorded(host, out.task_id)
    assert [name for name, _ in records] == ["roster"]
    events = list(host.event_log.read(out.task_id))
    first_llm = next(i for i, e in enumerate(events) if e.type == "LLMRequestStarted")
    assert records[0][1] < first_llm  # pre-loop
    assert events[records[0][1]].payload.policy == "pinned"

    expected = (
        f"{LISTING_ROSTER_HEADER}\n- alpha: first\n- bare\n- zeta: last"
    )
    view, task = _compose(host, out.task_id)
    assert task.context.content_anchors[f"{SKILL_LISTING_KIND}:roster"] == 0
    assert expected in _segment_texts(view, "semi_stable")
    assert expected not in _segment_texts(view, "dynamic_suffix")
    # ...and that is what the model saw on its first request.
    assert listing_texts(provider.received_requests[0].messages) == [expected]


def test_over_budget_roster_records_the_rank_decided_degrade(tmp_path: Path) -> None:
    """Over budget, rank decides which summary stays full — once, in the
    recorded roster."""
    ws = _ws(tmp_path, *((n, n[0] * 400) for n in ("aaa", "bbb", "ccc")))
    host, driver, provider = _host(
        ws,
        [_end()],
        skill_menu_rank_resolver=lambda tid: {"bbb": 1.0},
        plugin_config_overrides={"skills": {"menu_budget_tokens": 160}},
    )
    driver.start(goal="go", agent="main")
    assert listing_entries(provider.received_requests[0].messages) == {
        "aaa": short_summary("a" * 400),
        "bbb": "b" * 400,
        "ccc": short_summary("c" * 400),
    }


# --- 3 ---------------------------------------------------------------------


def test_install_appends_one_added_resident_and_nothing_earlier_moves(
    tmp_path: Path,
) -> None:
    ws = _ws(tmp_path, ("alpha", "the first skill"))
    host, driver, provider = _host(
        ws,
        [
            _end("t0"),
            _end("t1"),
            _end("t2"),
            _call("sk", SKILL_TOOL, {"skill": "alpha"}),
            _end("t3"),
        ],
    )
    started = driver.start(goal="first", agent="main")
    tid = started.task_id
    view_1, _ = _compose(host, tid)

    write_skill(ws, "beta", "installed mid-task")
    driver.send_goal(tid, goal="second")
    assert _names(host, tid) == ["roster", "added-0"]
    first, second = provider.received_requests[:2]
    # Every earlier message keeps its bytes; the announcement is appended.
    assert second.messages[: len(first.messages)] == first.messages
    added_text = f"{LISTING_ADDED_HEADER}\n- beta: installed mid-task"
    tail = [
        "".join(getattr(b, "text", "") for b in m.content)
        for m in second.messages[len(first.messages):]
    ]
    assert added_text in tail
    assert listing_texts(second.messages) == [
        f"{LISTING_ROSTER_HEADER}\n- alpha: the first skill",
        added_text,
    ]
    # Rendered at its anchor in the dynamic suffix; semi_stable unchanged.
    view_2, task_2 = _compose(host, tid)
    anchor = task_2.context.content_anchors[f"{SKILL_LISTING_KIND}:added-0"]
    assert anchor > 0
    assert added_text in _segment_texts(view_2, "dynamic_suffix")
    assert added_text not in _segment_texts(view_2, "semi_stable")
    assert _segment(view_2, "semi_stable").segment_hash == _segment(
        view_1, "semi_stable"
    ).segment_hash

    # Edit, then remove: nothing recorded, the listing keeps its bytes.
    write_skill(ws, "beta", "a rewritten summary")
    shutil.rmtree(ws / ".noeta" / "skills" / "alpha")
    driver.send_goal(tid, goal="third")
    third = provider.received_requests[2]
    assert _names(host, tid) == ["roster", "added-0"]
    assert listing_texts(third.messages) == listing_texts(second.messages)

    # Calling the removed skill gets the translate's recoverable answer.
    driver.send_goal(tid, goal="fourth")
    task = fold(host.event_log, host.content_store, tid)
    errors = [
        b.error or ""
        for m in task.runtime.messages
        for b in m.content
        if isinstance(b, ToolResultBlock) and b.call_id == "sk"
    ]
    assert len(errors) == 1
    assert "unknown skill 'alpha'" in errors[0]
    assert "available: beta" in errors[0]
    assert "alpha" not in task.state.active_skills
    assert _names(host, tid) == ["roster", "added-0"]


# --- 4 ---------------------------------------------------------------------


def test_restart_composes_identical_messages_and_announces_only_the_absent(
    tmp_path: Path,
) -> None:
    """A fresh host over the same store: the usage rank moved (the model used
    ``ccc``), yet the composed messages are byte-identical, and the next goal
    announces only the skill no recorded listing names."""
    db = str(tmp_path / "store.sqlite")
    ws = _ws(tmp_path, *((n, n[0] * 400) for n in ("aaa", "bbb", "ccc")))
    budget = {"skills": {"menu_budget_tokens": 160}}
    first, first_driver, _ = _host(
        ws,
        [_call("sk", SKILL_TOOL, {"skill": "ccc"}), _end("t0")],
        sqlite_path=db,
        plugin_config_overrides=budget,
    )
    tid = first_driver.start(goal="use ccc", agent="main").task_id
    before, _ = _compose(first, tid)
    # Unranked at task start: name order keeps ``aaa`` full.
    assert listing_entries(_segment(before, "semi_stable").content)["aaa"] == "a" * 400

    second, second_driver, provider = _host(
        ws, [_end("t1")], sqlite_path=db, plugin_config_overrides=budget
    )
    after, task = _compose(second, tid)
    # The fresh host's own fit now ranks ``ccc`` first...
    slot = second._task_locals.peek(tid, ROSTER_SLOT)
    assert dict(slot.lines)["ccc"] == "- ccc: " + "c" * 400
    # ...but the composed messages are the recorded ones, byte for byte.
    assert _canonical_messages(after) == _canonical_messages(before)

    write_skill(ws, "ddd", "installed while down")
    second_driver.send_goal(tid, goal="again")
    assert _names(second, tid) == ["roster", "added-0"]
    assert listing_texts(provider.received_requests[0].messages)[1:] == [
        f"{LISTING_ADDED_HEADER}\n- ddd: installed while down"
    ]


# --- 5 ---------------------------------------------------------------------


def _compact(host: Any, task_id: str, boundary: int) -> None:
    host.event_log.emit(
        task_id=task_id,
        type="Compacted",
        payload=CompactedPayload(
            summary_ref=host.content_store.put(
                to_canonical_bytes("SUMMARY"), media_type="application/json"
            ),
            boundary_count=boundary,
            replaced_count=boundary,
            composer_version=COMPOSER_VERSION,
        ),
    )


def test_compaction_rehangs_the_added_resident_after_the_summary(
    tmp_path: Path,
) -> None:
    """A pre-loop ``roster`` stays in ``semi_stable`` (it was never in the
    covered history); ``added-0`` re-hangs right after the summary."""
    ws = _ws(tmp_path, ("alpha", "first"))
    host, driver, _ = _host(ws, [_end("t0"), _end("t1")])
    tid = driver.start(goal="first", agent="main").task_id
    write_skill(ws, "beta", "second")
    driver.send_goal(tid, goal="second")
    task = fold(host.event_log, host.content_store, tid)
    anchor = task.context.content_anchors[f"{SKILL_LISTING_KIND}:added-0"]
    _compact(host, tid, len(task.runtime.messages))
    assert anchor < len(task.runtime.messages)

    view, _ = _compose(host, tid)
    dynamic = _segment_texts(view, "dynamic_suffix")
    assert dynamic[0] == f"{_SUMMARY_FRAME}\n\nSUMMARY"
    assert dynamic[1] == f"{LISTING_ADDED_HEADER}\n- beta: second"
    assert f"{LISTING_ROSTER_HEADER}\n- alpha: first" in _segment_texts(
        view, "semi_stable"
    )


def test_compaction_rehangs_both_anchored_residents_roster_first(
    tmp_path: Path,
) -> None:
    """A roster recorded mid-task (empty at seed) and a later ``added-0``,
    both covered by the summary: both re-hang after it, roster first."""
    ws = _ws(tmp_path)
    host, driver, _ = _host(ws, [_end("t0"), _end("t1"), _end("t2")])
    tid = driver.start(goal="no skills yet", agent="main").task_id
    write_skill(ws, "alpha", "first")
    driver.send_goal(tid, goal="second")
    write_skill(ws, "beta", "second")
    driver.send_goal(tid, goal="third")
    assert _names(host, tid) == ["roster", "added-0"]
    task = fold(host.event_log, host.content_store, tid)
    _compact(host, tid, len(task.runtime.messages))

    view, _ = _compose(host, tid)
    dynamic = _segment_texts(view, "dynamic_suffix")
    assert dynamic[:3] == [
        f"{_SUMMARY_FRAME}\n\nSUMMARY",
        f"{LISTING_ROSTER_HEADER}\n- alpha: first",
        f"{LISTING_ADDED_HEADER}\n- beta: second",
    ]
    assert not listing_texts(_segment(view, "semi_stable").content)


# --- 6 ---------------------------------------------------------------------


def _anchored_roster_checks(host: Any, first: Any, second: Any, tid: str) -> None:
    """The roster recorded at the second goal (``second`` is that goal's
    request, ``first`` the one before): the tool schema appears once, the
    roster is anchored past the first assistant message in the dynamic suffix,
    and every earlier message keeps its bytes."""
    assert _skill_schema(first) is None and _skill_schema(second) is not None
    assert _names(host, tid) == ["roster"]
    assert second.messages[: len(first.messages)] == first.messages
    roster = f"{LISTING_ROSTER_HEADER}\n- alpha: first"
    assert listing_texts(second.messages) == [roster]
    view, task = _compose(host, tid)
    assert task.context.content_anchors[f"{SKILL_LISTING_KIND}:roster"] > 0
    assert roster in _segment_texts(view, "dynamic_suffix")
    assert not listing_texts(_segment(view, "semi_stable").content)


def test_task_without_a_listing_gets_roster_at_its_next_goal(
    tmp_path: Path,
) -> None:
    """A task recorded with no listing resident — here: a first process whose
    ``main`` did not activate ``skill_invocation`` (as a task recorded before
    the listing existed carries none) — resumed by a host whose ``main``
    does: the init hook records ``roster`` ahead of the next goal, anchored
    there, and the provider adds nothing beside it."""
    db = str(tmp_path / "store.sqlite")
    ws = _ws(tmp_path, ("alpha", "first"))
    old, old_driver, old_provider = _host(
        ws,
        [_end("t0")],
        spec=runner_main_spec("main", skill_invocation=False),
        sqlite_path=db,
    )
    tid = old_driver.start(goal="first", agent="main").task_id
    assert _names(old, tid) == []

    new, new_driver, new_provider = _host(ws, [_end("t1")], sqlite_path=db)
    new_driver.send_goal(tid, goal="second")
    _anchored_roster_checks(
        new, old_provider.received_requests[0], new_provider.received_requests[0], tid
    )


def test_roster_empty_at_seed_is_recorded_by_the_first_goal_that_finds_skills(
    tmp_path: Path,
) -> None:
    ws = _ws(tmp_path)
    host, driver, provider = _host(ws, [_end("t0"), _end("t1")])
    tid = driver.start(goal="first", agent="main").task_id
    assert _names(host, tid) == []
    write_skill(ws, "alpha", "first")
    driver.send_goal(tid, goal="second")
    _anchored_roster_checks(host, *provider.received_requests[:2], tid)


# --- 7 ---------------------------------------------------------------------


def test_child_task_gets_its_own_listing_only_with_skill_invocation(
    tmp_path: Path,
) -> None:
    ws = _ws(tmp_path, ("alpha", "first"))
    skilled = dataclasses.replace(
        preset_spec("general-purpose"), name="skilled", plugins=("skill_invocation",)
    )
    plain = dataclasses.replace(preset_spec("general-purpose"), name="plain", plugins=())
    main = runner_main_spec("main", delegation=True, spawnable=("skilled", "plain"))
    host, driver, provider = _host(
        ws,
        [
            _call("c1", SPAWN_SUBAGENT_TOOL, {"agent": "skilled", "goal": "a"}),
            _end("skilled done"),
            _call("c2", SPAWN_SUBAGENT_TOOL, {"agent": "plain", "goal": "b"}),
            _end("plain done"),
            _end("parent done"),
        ],
        registry=make_registry(main, skilled, plain),
        multi_turn=False,
    )
    out = driver.start(goal="delegate twice", agent="main")
    assert out.status == "terminal"
    children = [
        str(e.payload.subtask_id)
        for e in host.event_log.read(out.task_id)
        if e.type == "SubtaskSpawned"
    ]
    assert len(children) == 2
    agents = {}
    for child in children:
        created = next(
            e for e in host.event_log.read(child) if e.type == "TaskCreated"
        )
        agents[created.payload.agent_name] = child
    assert set(agents) == {"skilled", "plain"}
    assert _names(host, out.task_id) == ["roster"]
    assert _names(host, agents["skilled"]) == ["roster"]
    assert _names(host, agents["plain"]) == []

    # The child's request carried the tool and its own listing; the plain
    # child's carried neither.
    skilled_req, plain_req = provider.received_requests[1], provider.received_requests[3]
    assert _skill_schema(skilled_req) is not None
    assert listing_texts(skilled_req.messages) == [
        f"{LISTING_ROSTER_HEADER}\n- alpha: first"
    ]
    assert _skill_schema(plain_req) is None
    assert listing_texts(plain_req.messages) == []
