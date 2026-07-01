"""Resource governance: per-session concurrency cap +
output-size cap surfaced as a ``truncated`` flag (replay/deref-consistent).

Two axes, both runtime accelerators that never perturb replay bytes:

* **Concurrency cap** — ``max_jobs_per_session`` (default 8). ``spawn`` counts
  the session root's currently-RUNNING jobs; the (cap+1)th is **rejected**
  (NOT queued) with a clear refusal the model can act on, records NO
  ``BackgroundShellStarted`` event, and starts no process. After one of the
  running jobs reaches terminal (kill / natural exit), a fresh spawn is
  accepted again.
* **Output cap** — the watcher already tail-truncates the off-ledger buffer to
  ``output_cap``; issue 07 surfaces a ``truncated: bool`` on ``poll`` and on the
  ``BackgroundShellPolled`` / ``BackgroundShellExited`` payloads (default False
  + canonical-omit so old recordings stay byte-identical). The snapshot ``put``
  stores the already-truncated buffer, so a deref / replay reads exactly the
  truncated tail — proven byte-equal here.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any

import pytest

from noeta.protocols.canonical import to_canonical_bytes
from noeta.protocols.events import (
    BackgroundShellExitedPayload,
    BackgroundShellPolledPayload,
)
from noeta.protocols.tool import ToolContext
from noeta.runtime.background_shell import (
    DEFAULT_MAX_BACKGROUND_JOBS_PER_SESSION,
    ProcessRegistry,
)
from noeta.storage.memory import InMemoryContentStore, InMemoryEventLog
from noeta.tools.fs import ShellMode, ShellPollTool, ShellRunTool, WorkspaceRoot


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _py(code: str) -> list[str]:
    return [sys.executable, "-c", code]


def _ws(tmp_path: Path) -> WorkspaceRoot:
    ws = tmp_path / "ws"
    ws.mkdir()
    return WorkspaceRoot.from_path(ws)


def _spawn_blocker(reg: ProcessRegistry, tmp_path: Path, task_id: str = "t-1") -> dict[str, Any]:
    """Spawn a process that blocks (reads stdin) so it stays RUNNING until killed."""
    return reg.spawn(
        argv=_py("import sys; sys.stdin.read()"),
        cwd=tmp_path,
        env={},
        command="python blocker",
        spawned_by_task_id=task_id,
        trace_id="tr",
    )


def _await_terminal(reg: ProcessRegistry, job_id: str, timeout_s: float = 5.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        state = reg.poll(job_id)
        if state["status"] in ("exited", "killed"):
            return state
        time.sleep(0.01)
    raise AssertionError(f"job {job_id} did not reach terminal within {timeout_s}s")


# ---------------------------------------------------------------------------
# Concurrency cap (per-session, reject — do not queue)
# ---------------------------------------------------------------------------


def test_spawn_up_to_cap_succeeds(tmp_path: Path) -> None:
    log = InMemoryEventLog()
    store = InMemoryContentStore()
    reg = ProcessRegistry(event_log=log, content_store=store, max_jobs_per_session=3)
    jobs = [_spawn_blocker(reg, tmp_path) for _ in range(3)]
    assert all("job_id" in j and not j.get("rejected") for j in jobs)
    # exactly 3 Started events on the session stream, no rejection trace
    started = [e for e in log.read("t-1") if e.type == "BackgroundShellStarted"]
    assert len(started) == 3
    for j in jobs:
        reg.kill(j["job_id"])
        _await_terminal(reg, j["job_id"])


def test_spawn_over_cap_is_rejected_no_process_no_event(tmp_path: Path) -> None:
    log = InMemoryEventLog()
    store = InMemoryContentStore()
    reg = ProcessRegistry(event_log=log, content_store=store, max_jobs_per_session=2)
    ok = [_spawn_blocker(reg, tmp_path) for _ in range(2)]
    # The (cap+1)th is REJECTED (not queued): a refusal dict, no job_id, no ref.
    rejected = _spawn_blocker(reg, tmp_path)
    assert rejected.get("rejected") is True
    assert "job_id" not in rejected
    assert "ref" not in rejected
    assert "reason" in rejected and "2" in rejected["reason"]  # mentions cap/count
    # Exactly the accepted jobs recorded a Started; the reject left no trace.
    started = [e for e in log.read("t-1") if e.type == "BackgroundShellStarted"]
    assert len(started) == 2
    for j in ok:
        reg.kill(j["job_id"])
        _await_terminal(reg, j["job_id"])


def test_spawn_accepted_again_after_one_terminal(tmp_path: Path) -> None:
    log = InMemoryEventLog()
    store = InMemoryContentStore()
    reg = ProcessRegistry(event_log=log, content_store=store, max_jobs_per_session=2)
    a = _spawn_blocker(reg, tmp_path)
    b = _spawn_blocker(reg, tmp_path)
    assert _spawn_blocker(reg, tmp_path).get("rejected") is True  # at cap
    # Kill one → its status flips to terminal → a fresh spawn is accepted.
    reg.kill(a["job_id"])
    _await_terminal(reg, a["job_id"])
    c = _spawn_blocker(reg, tmp_path)
    assert "job_id" in c and not c.get("rejected")
    for j in (b, c):
        reg.kill(j["job_id"])
        _await_terminal(reg, j["job_id"])


def test_concurrency_cap_is_per_session_not_global(tmp_path: Path) -> None:
    """The cap counts RUNNING jobs under ONE session root — a different session
    root has its own budget (jobs keyed by session root, issue 04)."""
    log = InMemoryEventLog()
    store = InMemoryContentStore()
    reg = ProcessRegistry(event_log=log, content_store=store, max_jobs_per_session=1)
    a = _spawn_blocker(reg, tmp_path, task_id="sess-A")
    # sess-A is full, but sess-B has its own budget.
    assert _spawn_blocker(reg, tmp_path, task_id="sess-A").get("rejected") is True
    b = _spawn_blocker(reg, tmp_path, task_id="sess-B")
    assert "job_id" in b and not b.get("rejected")
    for j, sid in ((a, "sess-A"), (b, "sess-B")):
        reg.kill(j["job_id"])
        _await_terminal(reg, j["job_id"])


def test_default_cap_is_eight() -> None:
    assert DEFAULT_MAX_BACKGROUND_JOBS_PER_SESSION == 8


def test_shell_run_tool_surfaces_cap_rejection(tmp_path: Path) -> None:
    """ShellRunTool turns the registry's reject into a clean ToolResult
    failure the model can act on (kill one first), not a crash."""
    log = InMemoryEventLog()
    store = InMemoryContentStore()
    reg = ProcessRegistry(event_log=log, content_store=store, max_jobs_per_session=1)
    ws = _ws(tmp_path)
    tool = ShellRunTool(workspace=ws, mode=ShellMode.ARBITRARY)
    ctx = ToolContext(
        artifact_store=store,
        background_runner=reg,
        metadata={"task_id": "t-cap", "trace_id": "tr"},
    )
    first = tool.invoke({"command": "sleep 30", "run_in_background": True}, ctx)
    assert first.success
    second = tool.invoke({"command": "sleep 30", "run_in_background": True}, ctx)
    assert second.success is False
    assert "too many background jobs" in second.summary
    assert "shell_kill" in second.summary
    reg.kill(first.output["job_id"])
    _await_terminal(reg, first.output["job_id"])


# ---------------------------------------------------------------------------
# Output cap → truncated surfaced + replay/deref-consistent
# ---------------------------------------------------------------------------


def test_truncated_false_when_under_cap(tmp_path: Path) -> None:
    log = InMemoryEventLog()
    store = InMemoryContentStore()
    reg = ProcessRegistry(event_log=log, content_store=store, output_cap=64 * 1024)
    out = reg.spawn(
        argv=_py("print('small output')"),
        cwd=tmp_path,
        env={},
        command="python small",
        spawned_by_task_id="t-small",
        trace_id="tr",
    )
    final = _await_terminal(reg, out["job_id"])
    assert final["truncated"] is False
    exited = next(e for e in log.read("t-small") if e.type == "BackgroundShellExited")
    # default-False omitted from canonical bytes ⇒ byte-identical to pre-07.
    assert exited.payload.truncated in (False, None)


def test_truncated_true_and_stored_bytes_are_the_tail(tmp_path: Path) -> None:
    log = InMemoryEventLog()
    store = InMemoryContentStore()
    cap = 4096
    reg = ProcessRegistry(event_log=log, content_store=store, output_cap=cap)
    # Emit far more than the cap; the buffer keeps only the most-recent tail.
    out = reg.spawn(
        argv=_py(
            "import sys\n"
            "for i in range(20000): sys.stdout.write('%06d\\n' % i)\n"
        ),
        cwd=tmp_path,
        env={},
        command="python chatty",
        spawned_by_task_id="t-trunc",
        trace_id="tr",
    )
    final = _await_terminal(reg, out["job_id"])
    assert final["truncated"] is True
    body = store.get(_ref_from_json(final["ref"]))
    assert len(body) <= cap
    # The TAIL survives (last line present, first line gone).
    assert b"019999\n" in body
    assert b"000000\n" not in body
    # poll's truncated flag + the Exited payload agree.
    exited = next(e for e in log.read("t-trunc") if e.type == "BackgroundShellExited")
    assert exited.payload.truncated is True
    # Replay/deref consistency: the snapshot put the truncated buffer, so the
    # bytes behind the recorded final_ref ARE the truncated tail, verbatim.
    assert store.get(exited.payload.final_ref) == body


def test_polled_payload_carries_truncated(tmp_path: Path) -> None:
    log = InMemoryEventLog()
    store = InMemoryContentStore()
    cap = 4096
    reg = ProcessRegistry(event_log=log, content_store=store, output_cap=cap)
    out = reg.spawn(
        argv=_py("import sys\nfor i in range(20000): sys.stdout.write('%06d\\n' % i)\n"),
        cwd=tmp_path,
        env={},
        command="python chatty",
        spawned_by_task_id="t-poll-trunc",
        trace_id="tr",
    )
    _await_terminal(reg, out["job_id"])
    p = reg.poll(out["job_id"])
    assert p["truncated"] is True
    polled = [e for e in log.read("t-poll-trunc") if e.type == "BackgroundShellPolled"]
    assert polled and polled[-1].payload.truncated is True


# ---------------------------------------------------------------------------
# Canonical-byte safety: a default-False truncated is OMITTED from the bytes
# (so pre-07 recordings stay byte-identical), a True one is present.
# ---------------------------------------------------------------------------


def test_truncated_default_omitted_from_canonical_bytes() -> None:
    from noeta.protocols.values import ContentRef

    ref = ContentRef(hash="h", size=0, media_type="text/plain")
    # The pre-07 payload shape == the default-truncated payload bytes.
    polled_default = BackgroundShellPolledPayload(job_id="j", ref=ref, offset=0)
    assert b"truncated" not in to_canonical_bytes(polled_default)
    exited_default = BackgroundShellExitedPayload(
        job_id="j", exit_code=0, final_ref=ref, summary="ok"
    )
    assert b"truncated" not in to_canonical_bytes(exited_default)
    # A True flag DOES enter the bytes (the model must learn truncation happened).
    polled_trunc = BackgroundShellPolledPayload(
        job_id="j", ref=ref, offset=0, truncated=True
    )
    assert b"truncated" in to_canonical_bytes(polled_trunc)


# ---------------------------------------------------------------------------
# HostConfig wiring — the cap flows from HostConfig → SdkHost → ProcessRegistry.
# ---------------------------------------------------------------------------


def test_sdkhost_threads_cap_into_registry(tmp_path: Path) -> None:
    """The SdkHost field flows into the ProcessRegistry it builds, so a
    configured cap actually rejects an over-cap spawn end-to-end."""
    from noeta.client.host import SdkHost
    from noeta.storage.memory import InMemoryDispatcher

    host = SdkHost(
        event_log=InMemoryEventLog(),
        content_store=InMemoryContentStore(),
        dispatcher=InMemoryDispatcher(),
        provider=_StubProvider(),
        workspace_dir=tmp_path,
        max_background_jobs_per_session=1,
    )
    reg = host._process_registry  # noqa: SLF001
    assert reg is not None
    a = _spawn_blocker(reg, tmp_path)
    assert "job_id" in a and not a.get("rejected")
    over = _spawn_blocker(reg, tmp_path)
    assert over.get("rejected") is True
    reg.kill(a["job_id"])
    _await_terminal(reg, a["job_id"])


class _StubProvider:
    """Minimal LLMProvider stand-in so SdkHost can be constructed in a unit
    test (we only exercise its ProcessRegistry, never a turn)."""

    def complete(self, *_a: Any, **_kw: Any) -> Any:  # pragma: no cover - unused
        raise AssertionError("no LLM round-trip in this test")


def _ref_from_json(j: dict[str, Any]):
    from noeta.protocols.values import ContentRef

    return ContentRef(hash=j["hash"], size=j["size"], media_type=j["media_type"])
