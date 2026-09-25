"""The memory index resident is frozen per task; changes ride a delta note.

2026-09-25: the init hook records the index first-write-wins
(``refresh=False``), so a ``memory_write`` mid-task never rewrites the
cached prefix, and the ``turn_intake`` delta note
(``memory_index_delta_provider``) names what changed since the task's
snapshot — computed from recorded state (the snapshot bytes at the
resident's active hash) against the live store.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

from noeta.builtins.memory.impl import (
    MEMORY_INDEX_NAME,
    MEMORY_KIND,
    memory_index_delta_provider,
)
from noeta.builtins.memory.impl.index import (
    DEFAULT_INDEX_BUDGET_TOKENS,
    render_memory_index_text,
)
from noeta.builtins.memory.impl.index_delta import (
    INDEX_DELTA_MAX_BYTES,
    INDEX_DELTA_PREFIX,
    format_index_delta,
    index_delta_items,
)
from noeta.builtins.memory.impl.store import (
    MEMORY_WRITE_TOOL_NAME,
    load_memory_store,
)
from noeta.execution.reminders import RecallView
from noeta.protocols.messages import Message, TextBlock
from noeta.storage.memory import InMemoryContentStore

from tests.test_memory_wiring import (
    _end_response,
    _memory_session,
    _ref_for,
    _seed_memory,
)


def _page(root: Path, name: str, description: str, body: str, updated: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / f"{name}.md").write_text(
        f"---\ndescription: {description}\nupdated: {updated}\n---\n{body}\n",
        encoding="utf-8",
    )


def _snapshot(root: Path, content_store: InMemoryContentStore) -> Any:
    """A task state whose index resident is the store's index as of now."""
    entries, updated = load_memory_store(root=root).index_snapshot()
    body = render_memory_index_text(
        entries, budget_tokens=DEFAULT_INDEX_BUDGET_TOKENS, updated=updated
    ).encode("utf-8")
    ref = content_store.put(body, media_type="text/markdown")
    return SimpleNamespace(active_content={MEMORY_KIND: {MEMORY_INDEX_NAME: ref.hash}})


def _note(
    root: Path,
    content_store: InMemoryContentStore,
    state: Any,
    history: tuple[Message, ...] = (),
) -> Optional[str]:
    provider = memory_index_delta_provider(load_memory_store(root=root), content_store)
    view = RecallView(
        task_id="t",
        message=(TextBlock(text="next goal"),),
        task_state=state,
        workspace_path=None,
        visible_history=history,
    )
    out = tuple(provider(view))
    if not out:
        return None
    (reminder,) = out
    assert reminder.origin == "system"
    return reminder.text


# ---------------------------------------------------------------------------
# The resident stays put
# ---------------------------------------------------------------------------


def test_index_resident_bytes_are_stable_across_a_write(tmp_path: Path) -> None:
    from noeta.core.fold import fold
    from noeta.protocols.messages import LLMResponse, ToolUseBlock, Usage

    ws = tmp_path / "ws"
    ws.mkdir()
    mem = tmp_path / "mem"
    _seed_memory(mem)
    write_call = LLMResponse(
        stop_reason="tool_use",
        content=[
            ToolUseBlock(
                call_id="mw1",
                tool_name=MEMORY_WRITE_TOOL_NAME,
                arguments={"name": "release-steps", "text": "Tag, build, publish.\n"},
            )
        ],
        usage=Usage(uncached=1, output=1),
        raw={"id": "w"},
    )
    host, driver = _memory_session(
        ws,
        [_end_response("one"), write_call, _end_response("saved"), _end_response("three")],
        memory=True,
        mem_dir=mem,
        multi_turn=True,
    )
    first = driver.start(goal="hello", agent="main")
    seed_hash = fold(host.event_log, host.content_store, first.task_id).state.active_content[
        MEMORY_KIND
    ][MEMORY_INDEX_NAME]
    index_text = host.content_store.get(_ref_for(host, seed_hash)).decode("utf-8")
    driver.send_goal(first.task_id, goal="save the release steps")
    turn_two = len(host.provider.received_requests)
    driver.send_goal(first.task_id, goal="and now")
    requests = host.provider.received_requests
    # Every request of the task carries the same index bytes.
    for request in requests:
        texts = ["".join(b.text for b in m.content if hasattr(b, "text")) for m in request.messages]
        assert index_text in texts
    # Turn three's request extends turn two's last one: the prefix did not move.
    before = requests[turn_two - 1].messages
    after = requests[turn_two].messages
    assert after[: len(before)] == before
    folded = fold(host.event_log, host.content_store, first.task_id)
    assert folded.state.active_content[MEMORY_KIND][MEMORY_INDEX_NAME] == seed_hash
    notes = [
        "".join(b.text for b in m.content if isinstance(b, TextBlock))
        for m in folded.runtime.messages
        if m.origin == "system"
    ]
    assert f"{INDEX_DELTA_PREFIX}+release-steps (Tag, build, publish.)" in notes


# ---------------------------------------------------------------------------
# What the note lists
# ---------------------------------------------------------------------------


def test_note_lists_a_created_and_a_redescribed_page_not_a_body_rewrite(
    tmp_path: Path,
) -> None:
    mem = tmp_path / "mem"
    _page(mem, "deploy-notes", "How we deploy", "Smoke tests.", "2026-09-01")
    _page(mem, "style", "House style", "Pure functions.", "2026-09-01")
    content = InMemoryContentStore()
    state = _snapshot(mem, content)
    _page(mem, "release-steps", "Release steps", "Tag, build.", "2026-09-20")
    _page(mem, "deploy-notes", "Deploy runbook", "Smoke tests.", "2026-09-10")
    _page(mem, "style", "House style", "Pure functions; no globals.", "2026-09-25")
    assert _note(mem, content, state) == (
        f"{INDEX_DELTA_PREFIX}+release-steps (Release steps), ~deploy-notes"
    )


def test_note_names_a_removed_page(tmp_path: Path) -> None:
    mem = tmp_path / "mem"
    _page(mem, "deploy-notes", "How we deploy", "Smoke tests.", "2026-09-01")
    _page(mem, "old", "Old page", "Stale.", "2026-01-01")
    content = InMemoryContentStore()
    state = _snapshot(mem, content)
    (mem / "old.md").unlink()
    assert _note(mem, content, state) == f"{INDEX_DELTA_PREFIX}-old"


def test_note_is_absent_when_nothing_changed(tmp_path: Path) -> None:
    mem = tmp_path / "mem"
    _page(mem, "deploy-notes", "How we deploy", "Smoke tests.", "2026-09-01")
    content = InMemoryContentStore()
    state = _snapshot(mem, content)
    assert _note(mem, content, state) is None
    # No index resident at all (an empty store at seed): nothing to compare.
    assert _note(mem, content, SimpleNamespace(active_content={})) is None


def test_note_is_not_repeated_while_the_same_note_is_visible(tmp_path: Path) -> None:
    mem = tmp_path / "mem"
    _page(mem, "deploy-notes", "How we deploy", "Smoke tests.", "2026-09-01")
    content = InMemoryContentStore()
    state = _snapshot(mem, content)
    _page(mem, "release-steps", "Release steps", "Tag.", "2026-09-20")
    text = _note(mem, content, state)
    assert text is not None
    said = Message(role="user", content=[TextBlock(text=text)], origin="system")
    assert _note(mem, content, state, (said,)) is None
    # A further change is news again.
    _page(mem, "zeta", "Zeta", "z", "2026-09-21")
    assert _note(mem, content, state, (said,)) == (
        f"{INDEX_DELTA_PREFIX}+zeta (Zeta), +release-steps (Release steps)"
    )


def test_note_is_capped_and_drops_the_oldest_changes() -> None:
    items = tuple(f"+page-{i:03d} (a description of page {i})" for i in range(40))
    text = format_index_delta(items)
    assert text is not None
    assert len(text.encode("utf-8")) <= INDEX_DELTA_MAX_BYTES
    assert text.startswith(f"{INDEX_DELTA_PREFIX}+page-000 ")
    kept = text.count("+page-")
    assert text.endswith(f", and {40 - kept} more")
    assert "+page-039" not in text
    assert format_index_delta(()) is None


def test_items_order_newest_first_and_skip_name_only_lines() -> None:
    snapshot = "\n".join(
        [
            "Long-term memory index.",
            "",
            "- kept",  # listed by name only (over-budget snapshot)
            "- same: unchanged",
        ]
    )
    entries = (
        ("a-new", "first", "", ""),
        ("b-new", "", "", ""),
        ("kept", "a new description", "", ""),
        ("same", "unchanged", "", ""),
    )
    updated = {"a-new": "2026-09-01", "b-new": "2026-09-02"}
    assert index_delta_items(snapshot, entries, updated) == ("+b-new", "+a-new (first)")
    # A snapshot that dropped entries cannot tell a new page from an old one
    # it left out, so it claims no ``+``.
    dropped = snapshot + "\n2 older memories are not listed here; 'memory_search' finds them."
    assert index_delta_items(dropped, entries, updated) == ()


# ---------------------------------------------------------------------------
# Resume
# ---------------------------------------------------------------------------


def test_resume_from_the_log_gives_the_same_note(tmp_path: Path) -> None:
    """A fresh process folds the task from the log and reads the same store:
    the snapshot is the recorded bytes, so the note is the same line."""
    from noeta.core.fold import fold
    from noeta.protocols.messages import LLMResponse, ToolUseBlock, Usage

    ws = tmp_path / "ws"
    ws.mkdir()
    mem = tmp_path / "mem"
    _seed_memory(mem)
    write_call = LLMResponse(
        stop_reason="tool_use",
        content=[
            ToolUseBlock(
                call_id="mw1",
                tool_name=MEMORY_WRITE_TOOL_NAME,
                arguments={"name": "release-steps", "text": "Tag, build, publish.\n"},
            )
        ],
        usage=Usage(uncached=1, output=1),
        raw={"id": "w"},
    )
    host, driver = _memory_session(
        ws,
        [write_call, _end_response("saved"), _end_response("done")],
        memory=True,
        mem_dir=mem,
        multi_turn=True,
    )
    first = driver.start(goal="save the release steps", agent="main")
    before = fold(host.event_log, host.content_store, first.task_id)
    live = _note(mem, host.content_store, before.state)
    driver.send_goal(first.task_id, goal="anything at all")
    recorded = [
        "".join(b.text for b in m.content if isinstance(b, TextBlock))
        for m in fold(host.event_log, host.content_store, first.task_id).runtime.messages
        if m.origin == "system"
    ]
    assert live is not None and live in recorded
    # Resume: a fresh fold of the same log, a fresh store handle, a fresh
    # provider — the same line.
    resumed = fold(host.event_log, host.content_store, first.task_id)
    assert _note(mem, host.content_store, resumed.state) == live
