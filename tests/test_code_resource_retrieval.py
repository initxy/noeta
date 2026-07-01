"""Skill referenced-file disclosure through `noeta code`.

A workspace skill whose `SKILL.md` references a bundled resource gets its
absolute base directory surfaced at compose time (`Base directory for this
skill: <dir>`) so the model can `read` the resource on demand; the content
is never inlined.
"""

from __future__ import annotations

from pathlib import Path

from noeta.protocols.messages import LLMResponse, TextBlock, Usage
from noeta.testing.fake_llm import FakeLLMProvider
from noeta.tools.fs import FsWriteMode, ShellMode

from tests._sdk_session import make_driver, make_host, make_registry, runner_main_spec


_SKILL = """\
---
name: doc-skill
description: references a note
---
Before answering, read NOTE.md for the conventions.
"""


def _make_ws(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    skill = ws / ".noeta" / "skills" / "doc-skill"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(_SKILL, encoding="utf-8")
    (skill / "NOTE.md").write_text("CONVENTION: be terse.\n", encoding="utf-8")
    return ws


def _end(text: str) -> LLMResponse:
    return LLMResponse(
        stop_reason="end_turn",
        content=[TextBlock(text=text)],
        usage=Usage(uncached=1, output=1),
        raw={"id": "end"},
    )


def test_referenced_resource_listed_not_inlined(tmp_path: Path) -> None:
    import json

    ws = _make_ws(tmp_path)
    provider = FakeLLMProvider(responses=[_end("done")])
    host = make_host(
        make_registry(runner_main_spec("main")),
        workspace_dir=ws,
        provider=provider,
        model="gpt-test",
        multi_turn=False,
        write_mode=FsWriteMode.APPLY,
        shell_mode=ShellMode.OFF,
    )
    driver = make_driver(host)
    # ``extra_skills=("doc-skill",)`` → the driver's pre-loop ``activations`` (the
    # same workspace-skill activation the runner did at prepare()).
    out = driver.start(goal="do the thing", agent="main", activations=("doc-skill",))
    # the prompt the model saw surfaces the skill's absolute
    # base directory (so it can `read` NOTE.md on demand) + the body
    # verbatim, but never inlines the resource content.
    prompt = " ".join(
        b.text
        for m in provider.received_requests[0].messages
        for b in getattr(m, "content", [])
        if isinstance(b, TextBlock)
    )
    skill_dir = ws / ".noeta" / "skills" / "doc-skill"
    assert f"Base directory for this skill: {skill_dir}" in prompt
    assert "read NOTE.md for the conventions" in prompt  # body verbatim
    assert "CONVENTION: be terse." not in prompt
    # the plan provenance carries no force-inlined resource.
    events = host.event_log.read(out.task_id)
    plan_refs = [
        e.payload.plan_ref
        for e in events
        if e.type == "ContextPlanComposed"
    ]
    plan = json.loads(host.content_store.get(plan_refs[-1]).decode("utf-8"))
    assert plan["retrieved_resources"] == []
    assert "doc-skill" in plan["selected_skills"]
