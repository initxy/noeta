"""Workspace environment block — the always-on fourth content-channel tenant.

Reuses the generic content-channel mechanism,
adds kind="environment", policy=evolving, mirroring the instructions channel's
structure (noeta/context/environment.py + noeta/execution/environment.py). Unlike
instructions/memory it is ALWAYS registered + activated (a workspace always
exists), and it lives in semi_stable — NOT the system prompt — so its absolute
path never rotates the stable_prefix hash / busts prompt caching.

Coverage:

* Pure-function units — load_environment facts, renderer, hash.
* Channel E2E — record/activation, semi_stable rendering, View source label.
* Stable prefix untouched — the env block is semi_stable, not system prompt.
* Product wiring — the SDK host/driver records + renders the block by default.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest

import noeta.execution.environment as env_exec
from noeta.context.composer import RenderedSkills, ThreeSegmentComposer
from noeta.context.content_channel import ContentChannelRegistry
from noeta.context.environment import (
    ENVIRONMENT_DRIFT_POLICY,
    ENVIRONMENT_KIND,
    ENVIRONMENT_NAME,
    ENVIRONMENT_VERSION,
    EnvironmentSnapshot,
    build_environment_renderer,
    environment_content_hash,
    environment_content_kind,
    render_environment_text,
)
from noeta.core.engine import Engine
from noeta.core.fold import fold
from noeta.core.wiring import wire_default_observers
from noeta.execution.environment import load_environment, record_environment
from noeta.policies.stub import StubScriptedPolicy
from noeta.protocols.canonical import to_canonical_bytes
from noeta.protocols.decisions import FinishDecision
from noeta.protocols.messages import LLMResponse, TextBlock, Usage
from noeta.storage.memory import (
    InMemoryContentStore,
    InMemoryDispatcher,
    InMemoryEventLog,
)
from noeta.testing.fake_llm import FakeLLMProvider
from noeta.tools.fs import FsWriteMode, ShellMode

from tests._sdk_session import (
    make_driver,
    make_host,
    make_registry,
    official_registry as official_agent_registry,
    runner_main_spec,
)


_SAMPLE = EnvironmentSnapshot(
    workspace_display="/work/repo", is_git_repo=True, platform="darwin"
)


# ---------------------------------------------------------------------------
# 1. load_environment — impure fact capture
# ---------------------------------------------------------------------------


def test_load_reports_workspace_platform_and_git_true(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    snap = load_environment(tmp_path)
    assert snap.workspace_display == str(tmp_path)
    assert snap.is_git_repo is True
    assert snap.platform == sys.platform


def test_load_git_false_when_no_dot_git(tmp_path: Path) -> None:
    snap = load_environment(tmp_path)
    assert snap.is_git_repo is False


def test_load_git_true_for_gitlink_file(tmp_path: Path) -> None:
    # A worktree / submodule carries `.git` as a file, not a directory.
    (tmp_path / ".git").write_text("gitdir: /elsewhere\n", encoding="utf-8")
    assert load_environment(tmp_path).is_git_repo is True


def test_load_captures_branch_status_and_date_in_real_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Drive a deterministic capture without depending on the host's git
    # state: stub the git subprocess and clock the loader closes over.
    def fake_run(workspace_dir, args):
        if args[:2] == ["rev-parse", "--abbrev-ref"]:
            return "feature/x\n"
        if args[0] == "status":
            return " M a.py\n?? b.py\n"
        return ""

    monkeypatch.setattr(env_exec, "_run_git", fake_run)
    monkeypatch.setattr(env_exec, "_captured_date", lambda: "2026-06-25T10:00:00")
    (tmp_path / ".git").mkdir()

    snap = load_environment(tmp_path)
    assert snap.git_branch == "feature/x"
    assert snap.git_status == " M a.py\n?? b.py"
    assert snap.captured_date == "2026-06-25T10:00:00"


def test_load_leaves_git_fields_empty_when_not_a_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No `.git` → the loader must NOT spawn git at all; fields stay "".
    def boom(*a, **k):  # pragma: no cover - asserted not called
        raise AssertionError("git must not run for a non-repo")

    monkeypatch.setattr(env_exec, "_run_git", boom)
    snap = load_environment(tmp_path)
    assert snap.is_git_repo is False
    assert snap.git_branch == ""
    assert snap.git_status == ""


def test_git_status_is_truncated_to_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    big = "?? f\n" * 2000  # well over the 2KB cap
    monkeypatch.setattr(env_exec, "_run_git", lambda wd, args: big)
    out = env_exec._git_status(tmp_path)  # noqa: SLF001
    assert len(out.encode("utf-8")) <= env_exec._GIT_STATUS_MAX_BYTES  # noqa: SLF001


def test_run_git_returns_empty_on_failure(tmp_path: Path) -> None:
    # A git command that does not exist must degrade to "" not raise.
    assert env_exec._run_git(tmp_path, ["this-is-not-a-git-subcommand"]) == ""  # noqa: SLF001


# ---------------------------------------------------------------------------
# 2. render + hash (same pattern as instructions/memory)
# ---------------------------------------------------------------------------


def test_render_wraps_facts_in_tag_with_resolution_rule() -> None:
    text = render_environment_text(_SAMPLE)
    assert text.startswith("<workspace-environment>")
    assert "Working directory: /work/repo" in text
    assert "resolve relative to this directory" in text
    assert "Is a git repository: true" in text
    assert "Platform: darwin" in text
    assert text.rstrip().endswith("</workspace-environment>")


def test_render_git_false_lowercase() -> None:
    snap = EnvironmentSnapshot(
        workspace_display="/w", is_git_repo=False, platform="linux"
    )
    assert "Is a git repository: false" in render_environment_text(snap)


def test_render_includes_branch_status_date_when_present() -> None:
    snap = EnvironmentSnapshot(
        workspace_display="/work/repo",
        is_git_repo=True,
        platform="darwin",
        git_branch="main",
        git_status=" M a.py\n?? b.py",
        captured_date="2026-06-25T10:00:00",
    )
    text = render_environment_text(snap)
    assert "Git branch: main" in text
    assert "Git status:\n M a.py\n?? b.py" in text
    assert "Captured at: 2026-06-25T10:00:00" in text
    assert text.rstrip().endswith("</workspace-environment>")


def test_render_omits_empty_git_lines() -> None:
    # Non-git / capture-failed snapshot: the new lines must NOT render.
    snap = EnvironmentSnapshot(
        workspace_display="/w", is_git_repo=False, platform="linux"
    )
    text = render_environment_text(snap)
    assert "Git branch:" not in text
    assert "Git status:" not in text
    assert "Captured at:" not in text


def test_render_omits_only_the_empty_fields() -> None:
    # Branch present, status empty (clean tree), date present → status line
    # dropped, the other two kept.
    snap = EnvironmentSnapshot(
        workspace_display="/w",
        is_git_repo=True,
        platform="linux",
        git_branch="main",
        git_status="",
        captured_date="2026-06-25T10:00:00",
    )
    text = render_environment_text(snap)
    assert "Git branch: main" in text
    assert "Git status:" not in text
    assert "Captured at: 2026-06-25T10:00:00" in text


def test_environment_version_is_2() -> None:
    assert ENVIRONMENT_VERSION == "2"


def test_render_is_stable_for_same_snapshot() -> None:
    snap = EnvironmentSnapshot(
        workspace_display="/work/repo",
        is_git_repo=True,
        platform="darwin",
        git_branch="main",
        git_status=" M a.py",
        captured_date="2026-06-25T10:00:00",
    )
    assert render_environment_text(snap) == render_environment_text(snap)
    assert environment_content_hash(snap) == environment_content_hash(snap)


def test_hash_is_stable_and_tracks_content() -> None:
    assert environment_content_hash(_SAMPLE) == environment_content_hash(_SAMPLE)
    other = EnvironmentSnapshot(
        workspace_display="/other", is_git_repo=True, platform="darwin"
    )
    assert environment_content_hash(other) != environment_content_hash(_SAMPLE)
    rendered = render_environment_text(_SAMPLE).encode("utf-8")
    assert environment_content_hash(_SAMPLE) == hashlib.sha256(rendered).hexdigest()


def test_renderer_renders_user_message_only_when_active() -> None:
    renderer = build_environment_renderer(_SAMPLE)
    rendered = renderer([ENVIRONMENT_NAME])
    assert isinstance(rendered, RenderedSkills)
    assert len(rendered.messages) == 1
    assert rendered.messages[0].role == "user"
    assert "Working directory: /work/repo" in rendered.messages[0].content[0].text
    # Not active → nothing rendered.
    assert renderer([]).messages == []
    assert renderer(["something-else"]).messages == []


def test_kind_is_evolving_and_resolves_through_generic_seam() -> None:
    spec = environment_content_kind(_SAMPLE)
    assert spec.kind == ENVIRONMENT_KIND
    assert spec.policy == "evolving"
    assert spec.policy == ENVIRONMENT_DRIFT_POLICY
    resolve = ContentChannelRegistry([spec]).content_hashes()
    assert resolve(ENVIRONMENT_KIND, ENVIRONMENT_NAME) == (
        ENVIRONMENT_VERSION,
        environment_content_hash(_SAMPLE),
    )
    assert resolve(ENVIRONMENT_KIND, "other") is None
    assert resolve("skill", ENVIRONMENT_NAME) is None


# ---------------------------------------------------------------------------
# 3. Channel E2E — record/activate, semi_stable render, source label, stable prefix
# ---------------------------------------------------------------------------


def _runtime() -> tuple[InMemoryEventLog, InMemoryContentStore, InMemoryDispatcher]:
    disp = InMemoryDispatcher()
    log = InMemoryEventLog(lease_validator=disp)
    wire_default_observers(log, disp)
    return log, InMemoryContentStore(), disp


def _composer(cs: InMemoryContentStore) -> ThreeSegmentComposer:
    return ThreeSegmentComposer(
        system_prompt="env test agent",
        tools={},
        content_store=cs,
        content_renderers=ContentChannelRegistry([environment_content_kind(_SAMPLE)]),
    )


def _engine(log, cs, composer) -> Engine:
    return Engine(
        event_log=log,
        content_store=cs,
        composer=composer,
        policy=StubScriptedPolicy([FinishDecision(answer="done")]),
    )


def test_record_environment_emits_evolving_event_and_activates() -> None:
    log, cs, _disp = _runtime()
    engine = _engine(log, cs, _composer(cs))
    task = engine.create_task(goal="g", policy_name="scripted")

    task = record_environment(log, cs, task, snapshot=_SAMPLE)

    events = [e for e in log.read(task.task_id) if e.type == "ContextContentRecorded"]
    assert len(events) == 1
    payload = events[0].payload
    assert payload.kind == ENVIRONMENT_KIND
    assert payload.name == ENVIRONMENT_NAME
    assert payload.policy == "evolving"
    assert payload.content_hash == environment_content_hash(_SAMPLE)
    assert payload.version == ENVIRONMENT_VERSION
    assert task.state.active_content[ENVIRONMENT_KIND] == (ENVIRONMENT_NAME,)


def test_record_environment_first_only_and_noop_on_none() -> None:
    log, cs, _disp = _runtime()
    engine = _engine(log, cs, _composer(cs))
    task = engine.create_task(goal="g", policy_name="scripted")

    task = record_environment(log, cs, task, snapshot=None)
    assert not [
        e for e in log.read(task.task_id) if e.type == "ContextContentRecorded"
    ]

    task = record_environment(log, cs, task, snapshot=_SAMPLE)
    task = record_environment(log, cs, task, snapshot=_SAMPLE)
    assert (
        len([e for e in log.read(task.task_id) if e.type == "ContextContentRecorded"])
        == 1
    )


def test_compose_places_env_in_semi_stable_not_system_prompt() -> None:
    log, cs, _disp = _runtime()
    composer = _composer(cs)
    engine = _engine(log, cs, composer)
    task = engine.create_task(goal="g", policy_name="scripted")

    # Stable prefix hash BEFORE the env block is activated.
    baseline_stable = [
        s for s in composer.compose(task).segments if s.name == "stable_prefix"
    ][0].segment_hash

    task = record_environment(log, cs, task, snapshot=_SAMPLE)
    view = composer.compose(task)

    semi = [s for s in view.segments if s.name == "semi_stable"][0]
    assert len(semi.content) == 1
    body = semi.content[0].content[0].text
    assert body.startswith("<workspace-environment>")
    assert "Working directory: /work/repo" in body
    # Activating the env block must NOT touch the stable prefix (it is
    # semi_stable, never the system prompt) — so prompt caching is unaffected.
    stable = [s for s in view.segments if s.name == "stable_prefix"][0]
    assert stable.segment_hash == baseline_stable
    # Determinism: same ledger → byte-equal recompose.
    assert to_canonical_bytes(view.segments) == to_canonical_bytes(
        composer.compose(task).segments
    )


# ---------------------------------------------------------------------------
# 4. Product wiring — the SDK host/driver records + renders by default
# ---------------------------------------------------------------------------


def _end_response() -> LLMResponse:
    return LLMResponse(
        stop_reason="end_turn",
        content=[TextBlock(text="done")],
        usage=Usage(uncached=1, output=1),
        raw={"id": "end"},
    )


def _server_host(ws: Path, *, instructions_enabled: bool = False):
    """A real ``SdkHost`` over an in-memory runtime — the resident host the
    HTTP server's ``InteractionDriver`` drives (server task creation goes through
    ``driver.seed_start``, NOT a product runner ``prepare()``)."""
    from noeta.client import SdkHost
    from noeta.execution.driver import multi_turn_policy_wrapper

    dispatcher = InMemoryDispatcher()
    event_log = InMemoryEventLog(lease_validator=dispatcher)
    content_store = InMemoryContentStore()
    host = SdkHost(
        event_log=event_log,
        content_store=content_store,
        dispatcher=dispatcher,
        provider=FakeLLMProvider(responses=[_end_response()]),
        model="stub-model",
        workspace_dir=ws,
        write_mode=FsWriteMode.DRY_RUN,
        shell_mode=ShellMode.OFF,
        instructions_enabled=instructions_enabled,
        policy_wrapper=multi_turn_policy_wrapper,
        registry=official_agent_registry(),
        aliases={"default": "main"},
    )
    return host, event_log, content_store


def test_server_seed_start_records_environment(tmp_path: Path) -> None:
    # docs: the HTTP server's seed path (driver.seed_start) must record the
    # environment channel as part of the once-per-session open — a
    # server-created task previously emitted NO environment block.
    from noeta.execution.driver import InteractionDriver

    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / ".git").mkdir()
    host, event_log, content_store = _server_host(ws)
    driver = InteractionDriver(host)

    outcome = driver.start(goal="hello", agent="main")

    env_events = [
        e
        for e in event_log.read(outcome.task_id)
        if e.type == "ContextContentRecorded"
        and getattr(e.payload, "kind", "") == ENVIRONMENT_KIND
    ]
    assert len(env_events) == 1
    assert env_events[0].payload.policy == "evolving"
    assert env_events[0].payload.name == ENVIRONMENT_NAME

    folded = fold(event_log, content_store, outcome.task_id)
    assert folded.state.active_content.get(ENVIRONMENT_KIND) == (ENVIRONMENT_NAME,)


def test_server_seed_start_records_instructions_when_file_present(
    tmp_path: Path,
) -> None:
    # With instructions enabled and an AGENTS.md present, the same server seed
    # path must also record the instructions channel (parity with prepare()).
    from noeta.context.instructions import INSTRUCTIONS_KIND
    from noeta.execution.driver import InteractionDriver

    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "AGENTS.md").write_text("# project rules\nbe nice\n", encoding="utf-8")
    host, event_log, _cs = _server_host(ws, instructions_enabled=True)
    driver = InteractionDriver(host)

    outcome = driver.start(goal="hello", agent="main")

    recorded = [
        e
        for e in event_log.read(outcome.task_id)
        if e.type == "ContextContentRecorded"
    ]
    kinds = {getattr(e.payload, "kind", "") for e in recorded}
    assert ENVIRONMENT_KIND in kinds
    assert INSTRUCTIONS_KIND in kinds
    instr = [e for e in recorded if getattr(e.payload, "kind", "") == INSTRUCTIONS_KIND]
    assert len(instr) == 1
    assert instr[0].payload.name == "AGENTS.md"


def test_server_seed_start_skips_instructions_when_disabled(tmp_path: Path) -> None:
    # instructions_enabled off → no instructions event even if AGENTS.md exists
    # (byte-equal to a host that never configured a project instructions file),
    # while the always-on environment block still records.
    from noeta.context.instructions import INSTRUCTIONS_KIND
    from noeta.execution.driver import InteractionDriver

    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "AGENTS.md").write_text("# rules\n", encoding="utf-8")
    host, event_log, _cs = _server_host(ws, instructions_enabled=False)
    driver = InteractionDriver(host)

    outcome = driver.start(goal="hello", agent="main")

    kinds = {
        getattr(e.payload, "kind", "")
        for e in event_log.read(outcome.task_id)
        if e.type == "ContextContentRecorded"
    }
    assert ENVIRONMENT_KIND in kinds
    assert INSTRUCTIONS_KIND not in kinds


def test_product_session_records_and_renders_environment(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / ".git").mkdir()
    host = make_host(
        make_registry(runner_main_spec("main")),
        workspace_dir=ws,
        provider=FakeLLMProvider(responses=[_end_response()]),
        model="stub-model",
        multi_turn=False,
        write_mode=FsWriteMode.DRY_RUN,
        shell_mode=ShellMode.OFF,
    )
    out = make_driver(host).start(goal="hi", agent="main")
    assert out.status == "terminal"
    events = list(host.event_log.read(out.task_id))
    found = [
        e
        for e in events
        if e.type == "ContextContentRecorded"
        and getattr(e.payload, "kind", "") == ENVIRONMENT_KIND
    ]
    assert len(found) == 1
    assert found[0].payload.policy == "evolving"
    assert found[0].payload.name == ENVIRONMENT_NAME

    folded = fold(host.event_log, host.content_store, out.task_id)
    assert folded.state.active_content.get(ENVIRONMENT_KIND) == (
        ENVIRONMENT_NAME,
    )
    view = host.resolve_engine_for_agent(
        "main", model="stub-model"
    )._composer.compose(folded)  # noqa: SLF001
    semi = [s for s in view.segments if s.name == "semi_stable"][0]
    env_blocks = [
        block.text
        for msg in semi.content
        for block in msg.content
        if hasattr(block, "text") and "workspace-environment" in block.text
    ]
    assert len(env_blocks) == 1
    assert f"Working directory: {ws}" in env_blocks[0]
