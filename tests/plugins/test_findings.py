"""ReportFindings: Claude Code's schema, a count for the model, and the
findings as structured data in a notice for front ends."""

from __future__ import annotations

from typing import Any

from noeta.core.tool import ToolContext
from noeta.plugins.findings import MAX_FINDINGS, SCHEMA, ReportFindingsTool


def _ctx(records: list[tuple[str, str, Any]]) -> ToolContext:
    return ToolContext(task_id="t", call_id="c", env=None,  # type: ignore[arg-type]
                       state={}, record=lambda e, k, d: records.append((e, k, d)))


def test_schema_matches_claude_codes_shape() -> None:
    assert SCHEMA["required"] == ["findings"] and SCHEMA["additionalProperties"] is False
    item = SCHEMA["properties"]["findings"]["items"]
    assert SCHEMA["properties"]["findings"]["maxItems"] == MAX_FINDINGS == 32
    assert item["required"] == ["file", "summary", "failure_scenario"]
    assert set(item["properties"]) == {"file", "line", "summary", "short_summary",
                                       "failure_scenario", "category", "verdict", "outcome"}
    assert item["properties"]["short_summary"]["maxLength"] == 60
    assert item["properties"]["category"]["maxLength"] == 40
    assert SCHEMA["properties"]["level"]["enum"] == ["low", "medium", "high", "xhigh", "max"]


async def test_findings_reach_front_ends_as_data_and_the_model_gets_a_count() -> None:
    records: list[tuple[str, str, Any]] = []
    finding = {"file": "a.py", "line": 3, "summary": "Off by one.", "failure_scenario": "n=0",
               "short_summary": "off by one"}
    out = await ReportFindingsTool().run({"level": "high", "findings": [finding]},
                                         _ctx(records))
    assert out.content[0].text == "Recorded 1 finding."  # type: ignore[union-attr]
    [(ext, kind, data)] = records
    assert (ext, kind, data["kind"]) == ("core", "notice", "findings")
    assert data["data"] == {"level": "high", "findings": [finding]}
    assert "a.py:3 — off by one" in data["text"]


async def test_no_findings_and_malformed_ones() -> None:
    records: list[tuple[str, str, Any]] = []
    out = await ReportFindingsTool().run({"findings": []}, _ctx(records))
    assert out.content[0].text == "Recorded no findings."  # type: ignore[union-attr]
    bad = await ReportFindingsTool().run({"findings": [{"file": "a.py"}]}, _ctx(records))
    assert bad.is_error


async def test_values_outside_the_schema_are_refused() -> None:
    ok = {"file": "a.py", "summary": "s", "failure_scenario": "f"}
    for call in ({"level": "huge", "findings": [ok]},
                 {"findings": [{**ok, "short_summary": "x" * 61}]},
                 {"findings": [{**ok, "category": "x" * 41}]},
                 {"findings": [{**ok, "verdict": "MAYBE"}]},
                 {"findings": [{**ok, "outcome": "done"}]},
                 {"findings": [{**ok, "line": "3"}]},
                 {"findings": [{**ok, "severity": "high"}]}):
        records: list[tuple[str, str, Any]] = []
        out = await ReportFindingsTool().run(call, _ctx(records))
        assert out.is_error and records == [], call
