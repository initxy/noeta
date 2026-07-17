"""Phase 4.5 I4 — multi-file sequential `edit` UX +
per-edit reporting + honest non-atomic semantics.

These loops exercise the library-reachable `CodeSessionResult`
contract (`files_changed`, `failed_edits`, `to_json()`). The
human-readable summary rendering (`_format_summary` /
`_group_files_changed`) lived only in the operator CLI and was
removed in the three-layer split, so the prose-grouping / prose-parity
assertions are gone; the machine-readable `to_json()` list remains the
contract downstream tooling reads.

Acceptance loops:

* **Happy path**: a fake-LLM scripts three `edit` calls
  against three different files; the resulting `files_changed` carries
  three applied entries and the workspace shows all three writes on
  disk.
* **Same-file sequence**: two `edit` calls touching the same
  file land as two `files_changed` rows so machine consumers see the
  full sequence.
* **Partial-failure path** (the architect's "honest semantics" pin):
  three `edit` calls; the second is constructed with an
  `old` that matches **twice** in the file (forcing `success=False`
  per Phase 4 B5). Assert:
  * the first edit applied (file bytes changed on disk).
  * the failed edit appears in `result.failed_edits` with the
    structured row shape the architect pinned (`tool` / `path` /
    `success=False` / `reason` / `summary` / `call_id`).
  * the third edit also applied — no implicit abort.
* **JSON contract**: `failed_edits` rows round-trip through
  `result.to_json()` with the pinned row shape, both for one failure
  and for multiple.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tests._read_models.result import (
    CodeSessionResult,
    _collect_failed_edits,
    _collect_files_changed,
    _last_selected_skills,
    _last_shell_result,
)
from noeta.protocols.messages import LLMResponse, TextBlock, ToolUseBlock, Usage
from noeta.testing.fake_llm import FakeLLMProvider
from noeta.tools.fs import FsWriteMode, ShellMode

from tests._sdk_session import make_driver, make_host, make_registry, runner_main_spec


def _make_workspace(tmp_path: Path, files: dict[str, str]) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    for rel, body in files.items():
        target = ws / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body)
    return ws


def _tool_call(call_id: str, name: str, args: dict[str, Any]) -> LLMResponse:
    return LLMResponse(
        stop_reason="tool_use",
        content=[ToolUseBlock(call_id=call_id, tool_name=name, arguments=args)],
        usage=Usage(uncached=1, output=1),
        raw={"id": call_id},
    )


def _end_turn(text: str = "done") -> LLMResponse:
    return LLMResponse(
        stop_reason="end_turn",
        content=[TextBlock(text=text)],
        usage=Usage(uncached=1, output=1),
        raw={"id": "end"},
    )


def _run(
    workspace: Path, responses: list[LLMResponse]
) -> CodeSessionResult:
    """Drive a one-shot SDK session and project the deleted
    ``CodeSessionRunner._build_result`` shape off the durable EventLog.

    ``require_approval_tools=()`` keeps the edits applying without approval (the
    SDK host's default permission_mode would otherwise gate the write family),
    matching the old ``CodeSessionConfig`` one-shot apply behaviour."""
    host = make_host(
        make_registry(runner_main_spec("main")),
        workspace_dir=workspace,
        provider=FakeLLMProvider(responses=responses),
        model="gpt-test",
        multi_turn=False,
        write_mode=FsWriteMode.APPLY,
        shell_mode=ShellMode.OFF,
        require_approval_tools=(),
    )
    out = make_driver(host).start(goal="multi-file edits", agent="main")
    events = host.event_log.read(out.task_id)
    cs = host.content_store
    return CodeSessionResult(
        task_id=out.task_id,
        status=out.status,
        events=len(events),
        selected_skills=_last_selected_skills(events, cs),
        files_changed=_collect_files_changed(events, cs),
        failed_edits=_collect_failed_edits(events, cs),
        last_shell=_last_shell_result(events, cs),
    )


# ---------------------------------------------------------------------------
# Happy path: three files
# ---------------------------------------------------------------------------


def test_three_file_edit_sequence_all_applied(tmp_path: Path) -> None:
    workspace = _make_workspace(
        tmp_path,
        {"a.py": "alpha\n", "b.py": "beta\n", "c.py": "gamma\n"},
    )
    responses = [
        _tool_call("e1", "edit", {"path": "a.py", "old": "alpha", "new": "A1"}),
        _tool_call("e2", "edit", {"path": "b.py", "old": "beta", "new": "B1"}),
        _tool_call("e3", "edit", {"path": "c.py", "old": "gamma", "new": "C1"}),
        _end_turn(),
    ]
    result = _run(workspace, responses)
    assert result.status == "terminal"
    assert (workspace / "a.py").read_text() == "A1\n"
    assert (workspace / "b.py").read_text() == "B1\n"
    assert (workspace / "c.py").read_text() == "C1\n"
    assert len(result.files_changed) == 3
    assert {c["path"] for c in result.files_changed} == {"a.py", "b.py", "c.py"}
    assert all(c["applied"] is True for c in result.files_changed)
    assert result.failed_edits == ()


# ---------------------------------------------------------------------------
# Same-file: two edits to one path both land as machine rows
# ---------------------------------------------------------------------------


def test_same_file_two_edits_both_recorded(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path, {"x.py": "one\ntwo\n"})
    responses = [
        _tool_call("e1", "edit", {"path": "x.py", "old": "one", "new": "ONE"}),
        _tool_call("e2", "edit", {"path": "x.py", "old": "two", "new": "TWO"}),
        _end_turn(),
    ]
    result = _run(workspace, responses)
    assert result.status == "terminal"
    # Both edits applied on disk.
    assert (workspace / "x.py").read_text() == "ONE\nTWO\n"
    # files_changed carries two rows (one per tool call) so machine
    # consumers see the full sequence — the human summary collapses
    # them by path, but that grouping lives in the (deleted) operator
    # CLI and is no longer library-reachable.
    assert len(result.files_changed) == 2
    assert all(c["path"] == "x.py" and c["applied"] for c in result.files_changed)


# ---------------------------------------------------------------------------
# Partial-failure: no rollback, no implicit abort
# ---------------------------------------------------------------------------


def test_partial_failure_does_not_abort_or_roll_back(tmp_path: Path) -> None:
    workspace = _make_workspace(
        tmp_path,
        {
            "a.py": "alpha\n",
            # "two" appears TWICE → edit refuses (B5 unique match).
            "b.py": "two\ntwo\n",
            "c.py": "gamma\n",
        },
    )
    responses = [
        _tool_call("e1", "edit", {"path": "a.py", "old": "alpha", "new": "A1"}),
        _tool_call("e2", "edit", {"path": "b.py", "old": "two", "new": "TWO"}),
        _tool_call("e3", "edit", {"path": "c.py", "old": "gamma", "new": "C1"}),
        _end_turn(),
    ]
    result = _run(workspace, responses)
    assert result.status == "terminal"

    # First edit applied — no rollback on later failure.
    assert (workspace / "a.py").read_text() == "A1\n"
    # Second edit refused — file untouched.
    assert (workspace / "b.py").read_text() == "two\ntwo\n"
    # Third edit ALSO applied — no implicit abort on partial failure.
    assert (workspace / "c.py").read_text() == "C1\n"

    # files_changed has two applied rows (a.py + c.py).
    applied_paths = [c["path"] for c in result.files_changed if c["applied"]]
    assert set(applied_paths) == {"a.py", "c.py"}

    # failed_edits carries the architect-pinned row shape.
    assert len(result.failed_edits) == 1
    failed = result.failed_edits[0]
    assert failed["tool"] == "edit"
    assert failed["path"] == "b.py"
    assert failed["success"] is False
    # `reason` is the structured tail; `summary` keeps the verbatim
    # EventLog summary. Both surface the must-be-unique guidance.
    assert "matches 2 times" in failed["reason"]
    assert "must be unique" in failed["reason"]
    assert failed["summary"].startswith("edit: ")
    assert isinstance(failed["call_id"], str) and failed["call_id"]


# ---------------------------------------------------------------------------
# Machine-readable JSON shape + prose parity
# ---------------------------------------------------------------------------


def test_to_json_round_trips_failed_edits_field(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path, {"b.py": "two\ntwo\n"})
    responses = [
        _tool_call("e1", "edit", {"path": "b.py", "old": "two", "new": "TWO"}),
        _end_turn(),
    ]
    result = _run(workspace, responses)
    blob = result.to_json()
    # Field is present even when there are zero failures (see other
    # test). Here we have one failure that must round-trip.
    assert "failed_edits" in blob
    assert isinstance(blob["failed_edits"], list)
    assert len(blob["failed_edits"]) == 1
    row = blob["failed_edits"][0]
    for required in ("tool", "path", "success", "reason", "summary", "call_id"):
        assert required in row, row
    assert row["success"] is False
    assert row["tool"] == "edit"
    assert row["path"] == "b.py"
    # JSON survives a full encode/decode round trip — the architect's
    # tooling contract.
    re_decoded = json.loads(json.dumps(blob))
    assert re_decoded["failed_edits"] == blob["failed_edits"]


def test_to_json_failed_edits_empty_list_when_no_failures(tmp_path: Path) -> None:
    workspace = _make_workspace(tmp_path, {"a.py": "alpha\n"})
    responses = [
        _tool_call("e1", "edit", {"path": "a.py", "old": "alpha", "new": "A1"}),
        _end_turn(),
    ]
    result = _run(workspace, responses)
    blob = result.to_json()
    # The machine-readable field is always present, empty on a clean run.
    assert blob["failed_edits"] == []


def test_multiple_failed_edits_all_recorded(tmp_path: Path) -> None:
    """Two non-unique-match edits both surface as machine-readable
    ``failed_edits`` rows — the JSON list IS the contract downstream
    tooling reads (the human-summary prose parity check lived in the
    deleted operator CLI and is no longer library-reachable)."""
    workspace = _make_workspace(
        tmp_path,
        {
            "b.py": "two\ntwo\n",
            "d.py": "four\nfour\n",
        },
    )
    responses = [
        _tool_call("e1", "edit", {"path": "b.py", "old": "two", "new": "TWO"}),
        _tool_call("e2", "edit", {"path": "d.py", "old": "four", "new": "FOUR"}),
        _end_turn(),
    ]
    result = _run(workspace, responses)
    blob = result.to_json()
    assert len(blob["failed_edits"]) == 2
    assert {row["path"] for row in blob["failed_edits"]} == {"b.py", "d.py"}
    for row in blob["failed_edits"]:
        assert row["success"] is False
        assert row["tool"] == "edit"
