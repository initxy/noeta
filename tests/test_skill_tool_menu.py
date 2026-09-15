"""The ``skill`` tool's schema is the model's only view of what skills exist.

It appears exactly when the capability is activated AND the workspace holds a
skill, so a workspace without skills causes zero schema drift. The enum is
sorted by name regardless of the order files landed on disk, which keeps the
schema bytes stable across machines and preserves the provider's prompt cache.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest

from tests._skill_fixtures import write_skill, write_skill_raw

from tests._session_inputs import default_factory_kwargs
from noeta.agent.registry import AgentRegistry
from noeta.agent.spec import (
    AgentSpec,
    BudgetSpec,
    ComponentRef,
)
from noeta.client.host import SdkHost
from noeta.execution.builder import (
    COMPACTION_OFF,
    build_session_inputs,
)
from noeta.runtime.governance import Budget
from noeta.builtins.skills.impl import (
    SKILL_TOOL,
    load_workspace_skills,
    make_skills_control_tool,
    skill_tool_schema,
)
from noeta.builtins.skills.impl.control_tool import (
    DEFAULT_MENU_BUDGET_TOKENS,
    MENU_DESCRIPTION_MAX_TOKENS,
    MENU_SHORT_SUMMARY_MAX_TOKENS,
    estimate_menu_tokens,
    fit_menu_to_budget,
    menu_keep_order,
    short_summary,
)
from noeta.builtins.skills.impl.indexer import SkillDescription, SkillRegistry
from noeta.builtins.skills.impl.wiring import merge_skill_registries
from noeta.policies.control_semantics import ControlTranslateContext
from noeta.protocols.decisions import TaskStatePatch
from noeta.protocols.task import Task, TaskState
from noeta.protocols.messages import (
    LLMResponse,
    Message,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
)
from noeta.storage.memory import (
    InMemoryContentStore,
    InMemoryDispatcher,
    InMemoryEventLog,
)
from noeta.testing.fake_llm import FakeLLMProvider
from noeta.runtime.shell_policy import ShellMode
from noeta.runtime.workspace import FsWriteMode


# ---------------------------------------------------------------------------
# Pure-schema tests — skill_tool_schema()
# ---------------------------------------------------------------------------


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def test_skill_schema_empty_menu_shape() -> None:
    """Empty menu still produces the one-parameter shape (single ``skill``)."""
    schema = skill_tool_schema(())
    assert schema["type"] == "function"
    assert schema["function"]["name"] == SKILL_TOOL
    params = schema["function"]["parameters"]
    assert params["required"] == ["skill"]
    prop = params["properties"]["skill"]
    assert prop["type"] == "string"
    # An empty enum is invalid in several provider schemas, so it is omitted.
    assert "enum" not in prop
    assert prop["description"] == "Name of the skill to activate."


def test_skill_schema_no_args_no_reason() -> None:
    """The tool takes a name and nothing else: extra parameters would invite
    the model to pass state the activation path has nowhere to put."""
    schema = skill_tool_schema((("alpha", "does things"),))
    props = schema["function"]["parameters"]["properties"]
    assert set(props.keys()) == {"skill"}


def test_skill_schema_nonempty_menu_enum_and_desc() -> None:
    """Menu entries populate the ``enum`` and append roster to description."""
    menu = (
        ("coder", "Writes Python code"),
        ("reviewer", "Finds bugs in code"),
    )
    schema = skill_tool_schema(menu)
    prop = schema["function"]["parameters"]["properties"]["skill"]
    assert prop["enum"] == ["coder", "reviewer"]
    desc = prop["description"]
    assert desc.startswith("Name of the skill to activate.")
    assert "Available: " in desc
    roster = desc.split("Available: ", 1)[1]
    assert "coder — Writes Python code" in roster
    assert "reviewer — Finds bugs in code" in roster


def test_skill_schema_bare_name_when_description_empty() -> None:
    """Skill with empty description renders as bare name (no `` — `` tail)."""
    menu = (
        ("named", "Has description"),
        ("anonymous", ""),
    )
    schema = skill_tool_schema(menu)
    prop = schema["function"]["parameters"]["properties"]["skill"]
    assert prop["enum"] == ["named", "anonymous"]
    roster = prop["description"].split("Available: ", 1)[1]
    assert "named — Has description" in roster
    assert "anonymous" in roster
    assert "anonymous — " not in roster


def test_skill_schema_deterministic_bytes_for_same_input() -> None:
    """The schema feeds a hashed prompt prefix, so identical input must give
    byte-identical output."""
    menu = (("a", "first"), ("b", "second"))
    assert _canonical(skill_tool_schema(menu)) == _canonical(
        skill_tool_schema(menu)
    )


# ---------------------------------------------------------------------------
# build_session_inputs integration — flag on/off, empty/non-empty registry
# ---------------------------------------------------------------------------


def _build_composer_schemas(
    ws: Path, *, skill_invocation_enabled: bool
) -> list[dict[str, Any]]:
    """Call ``build_session_inputs`` and return composer control schemas."""
    content_store = InMemoryContentStore()
    inputs = build_session_inputs(
        **default_factory_kwargs(),
        workspace_dir=ws,
        system_prompt="you are helpful",
        allowed_tools=frozenset({"read"}),
        content_store=content_store,
        model="stub-model",
        compaction=COMPACTION_OFF,
        budget=Budget(),
        capability_flags={"skill_invocation": skill_invocation_enabled},
        # The fs write/shell knobs ride plugin_config under the plugin's name.
        plugin_config={
            "fs": {"write_mode": FsWriteMode.DRY_RUN, "shell_mode": ShellMode.OFF},
        },
    )
    return list(inputs.composer._control_action_schemas)


def _find_skill_schema(schemas: list[dict[str, Any]]) -> dict[str, Any] | None:
    for s in schemas:
        if (
            isinstance(s, dict)
            and s.get("function", {}).get("name") == SKILL_TOOL
        ):
            return s
    return None


def test_flag_off_no_skill_schema_even_with_skills(tmp_path: Path) -> None:
    """Flag disabled → skill tool absent from composer, even with skills."""
    ws = tmp_path / "ws"
    ws.mkdir()
    write_skill(ws, "coder", "Writes code")
    schemas = _build_composer_schemas(ws, skill_invocation_enabled=False)
    assert _find_skill_schema(schemas) is None


def test_flag_on_empty_registry_no_skill_schema(tmp_path: Path) -> None:
    """Flag on but no skills on disk → skill tool absent."""
    ws = tmp_path / "ws"
    ws.mkdir()
    schemas = _build_composer_schemas(ws, skill_invocation_enabled=True)
    assert _find_skill_schema(schemas) is None


def test_flag_on_with_skills_renders_sorted_menu(tmp_path: Path) -> None:
    """Registry non-empty + flag on → enum sorted by name, descriptions present."""
    ws = tmp_path / "ws"
    ws.mkdir()
    # Written out of order on purpose so the sort has something to do.
    write_skill(ws, "zeta", "last letter")
    write_skill(ws, "alpha", "first letter")
    write_skill(ws, "beta", "")  # empty description
    schemas = _build_composer_schemas(ws, skill_invocation_enabled=True)
    schema = _find_skill_schema(schemas)
    assert schema is not None
    prop = schema["function"]["parameters"]["properties"]["skill"]
    assert prop["enum"] == ["alpha", "beta", "zeta"]
    desc = prop["description"]
    assert "alpha — first letter" in desc
    assert "zeta — last letter" in desc
    assert "beta" in desc
    assert "beta — " not in desc


def test_menu_built_from_registry_not_caller(tmp_path: Path) -> None:
    """The menu derives from the loaded registry, never from a caller
    argument, and the flag rides the generic ``capability_flags`` bag — the
    builder's signature must stay free of skill-specific vocabulary or the
    kernel starts knowing what a skill is."""
    import inspect

    sig = inspect.signature(build_session_inputs)
    assert "skill_menu" not in sig.parameters
    assert "skill_invocation_enabled" not in sig.parameters
    assert "capability_flags" in sig.parameters


# ---------------------------------------------------------------------------
# SdkHost — the spec's "skill_invocation" activation drives the flag
# ---------------------------------------------------------------------------


def _stub_provider() -> FakeLLMProvider:
    from noeta.protocols.messages import LLMResponse, TextBlock

    return FakeLLMProvider(
        responses=[
            LLMResponse(
                stop_reason="end_turn",
                content=[TextBlock(text="ok")],
                usage=Usage(uncached=1, output=1),
                raw={"id": "r1"},
            )
        ]
    )


def _make_host(workspace: Path) -> SdkHost:
    dispatcher = InMemoryDispatcher()
    event_log = InMemoryEventLog(lease_validator=dispatcher)
    content_store = InMemoryContentStore()
    return SdkHost(
        event_log=event_log,
        content_store=content_store,
        dispatcher=dispatcher,
        provider=_stub_provider(),
        model="stub-model",
        workspace_dir=workspace,
        registry=AgentRegistry(),
    )


def _spec(skill_invocation: bool) -> AgentSpec:
    return AgentSpec(
        name="main",
        instructions="you are helpful",
        policy=ComponentRef("react", "1"),
        composer=ComponentRef("three_segment", "v3"),
        tools=(),
        default_budget=BudgetSpec(),
        plugins=("skill_invocation",) if skill_invocation else (),
        metadata={},
    )


def _skill_schema_from_engine(engine: Any) -> dict[str, Any] | None:
    composer = engine._composer
    for s in composer._control_action_schemas:
        if (
            isinstance(s, dict)
            and s.get("function", {}).get("name") == SKILL_TOOL
        ):
            return s
    return None


def test_sdkhost_capability_on_preserves_schema_with_skills(
    tmp_path: Path,
) -> None:
    """The activation on the AgentSpec reaches the composer schema through
    ``_build_engine``, so a spec is enough to configure the menu."""
    ws = tmp_path / "ws"
    ws.mkdir()
    write_skill(ws, "coder", "Writes code")
    write_skill(ws, "reviewer", "")

    host = _make_host(ws)
    spec = _spec(skill_invocation=True)
    host.registry.add(spec)
    engine = host._build_engine(
        spec,
        "stub-model",
        delegation_enabled=False,
        allowed_subtask_agents=frozenset(),
        ask_user_question_enabled=False,
        policy_wrapper=None,
    )

    schema = _skill_schema_from_engine(engine)
    assert schema is not None, "skill schema missing when capability is on"
    prop = schema["function"]["parameters"]["properties"]["skill"]
    assert prop["enum"] == ["coder", "reviewer"]
    assert "coder — Writes code" in prop["description"]
    assert "reviewer" in prop["description"]
    assert "reviewer — " not in prop["description"]


def test_sdkhost_capability_off_masks_schema_even_with_skills(
    tmp_path: Path,
) -> None:
    """Without the activation the schema stays absent even though skills are
    on disk — the workspace's contents must never override the spec."""
    ws = tmp_path / "ws"
    ws.mkdir()
    write_skill(ws, "coder", "Writes code")

    host = _make_host(ws)
    spec = _spec(skill_invocation=False)
    host.registry.add(spec)
    engine = host._build_engine(
        spec,
        "stub-model",
        delegation_enabled=False,
        allowed_subtask_agents=frozenset(),
        ask_user_question_enabled=False,
        policy_wrapper=None,
    )

    assert _skill_schema_from_engine(engine) is None


# ---------------------------------------------------------------------------
# D11 — disable-model-invocation, and the per-skill roster budget
# ---------------------------------------------------------------------------


def _write_skill_raw(ws: Path, name: str, frontmatter: str, body: str) -> None:
    skill_dir = ws / ".noeta" / "skills" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\n{frontmatter}---\n{body}", encoding="utf-8"
    )


def test_disable_model_invocation_skill_is_absent_from_the_menu(
    tmp_path: Path,
) -> None:
    """(a) first half — a skill declaring ``disable-model-invocation: true``
    never enters the model's menu, while its neighbours still do."""
    ws = tmp_path / "ws"
    ws.mkdir()
    write_skill(ws, "visible", "on the menu")
    _write_skill_raw(
        ws,
        "hidden",
        "description: user-invoked only\ndisable-model-invocation: true\n",
        "Body of the hidden skill.\n",
    )
    schemas = _build_composer_schemas(ws, skill_invocation_enabled=True)
    schema = _find_skill_schema(schemas)
    assert schema is not None
    prop = schema["function"]["parameters"]["properties"]["skill"]
    assert prop["enum"] == ["visible"]
    assert "hidden" not in prop["description"]


def test_disable_model_invocation_skill_still_preloads_and_renders(
    tmp_path: Path,
) -> None:
    """(a) second half — the same skill stays in the Registry, so the host
    preload channel (``Options.skills`` / a seed activation, both of which land
    as ``TaskStatePatch(activate_skills=...)``) loads it and it renders
    normally. Off the menu is not out of the workspace."""
    ws = tmp_path / "ws"
    ws.mkdir()
    _write_skill_raw(
        ws,
        "hidden",
        "description: user-invoked only\ndisable-model-invocation: true\n",
        "Body of the hidden skill.\n",
    )
    inputs = build_session_inputs(
        **default_factory_kwargs(),
        workspace_dir=ws,
        system_prompt="you are helpful",
        allowed_tools=frozenset({"read"}),
        content_store=InMemoryContentStore(),
        model="stub-model",
        compaction=COMPACTION_OFF,
        budget=Budget(),
        capability_flags={"skill_invocation": True},
        plugin_config={
            "fs": {"write_mode": FsWriteMode.DRY_RUN, "shell_mode": ShellMode.OFF},
        },
    )
    # The tool is not grown at all here: the only skill on disk is off-menu.
    assert _find_skill_schema(list(inputs.composer._control_action_schemas)) is None

    task = Task(task_id="t-hidden", status="running", state=TaskState(goal="g"))
    TaskStatePatch(activate_skills=["hidden"]).apply(task.state)
    view = inputs.composer.compose(task)

    semi_stable = next(s for s in view.segments if s.name == "semi_stable")
    text = " ".join(
        b.text
        for m in semi_stable.content
        for b in m.content
        if isinstance(b, TextBlock)
    )
    assert "Activated skill: hidden" in text
    assert "Body of the hidden skill." in text


class _FlagCtx:
    """Minimal ``ControlToolBuildContext`` stand-in: the mount reads one flag."""

    def flag(self, name: str) -> bool:
        return name == "skill_invocation"


def test_hidden_skill_cannot_be_named_by_the_model(tmp_path: Path) -> None:
    """The translate validates against the SAME set the enum was built from, so
    a model that guesses the name gets a recoverable error rather than a
    back door into an off-menu skill."""
    ws = tmp_path / "ws"
    ws.mkdir()
    write_skill(ws, "visible", "on the menu")
    _write_skill_raw(
        ws,
        "hidden",
        "description: user-invoked only\ndisable-model-invocation: true\n",
        "b\n",
    )
    registry = load_workspace_skills(ws)
    assert set(registry.names()) == {"hidden", "visible"}

    mount = make_skills_control_tool(registry)(_FlagCtx())
    assert mount is not None
    response = LLMResponse(
        stop_reason="tool_use",
        content=[
            ToolUseBlock(
                call_id="c1", tool_name=SKILL_TOOL, arguments={"skill": "hidden"}
            )
        ],
    )
    decision = mount.translate(
        ControlTranslateContext(
            response=response,
            assistant_message=Message(role="assistant", content=list(response.content)),
            assistant_thinking=(),
            content_store=None,
        )
    )
    assert decision is not None
    assert decision.patch is None
    block = decision.messages_after[0].content[0]
    assert isinstance(block, ToolResultBlock)
    assert "unknown skill 'hidden'" in block.output
    assert "available: visible" in block.output


def test_oversized_description_is_truncated_in_the_roster(tmp_path: Path) -> None:
    """(d) One skill's description is capped at 384 estimated tokens in the
    roster — 1,536 characters of ASCII, 384 of Chinese.

    Every description concatenates into ONE property description that sits
    inside the stable-prefix hash, so an unbounded ``description:`` is unbounded
    prompt on every turn. The cap is in tokens so a Chinese summary cannot cost
    four times an English one.
    """
    ws = tmp_path / "ws"
    ws.mkdir()
    write_skill(ws, "verbose", "x" * 5000)
    write_skill(ws, "exact", "y" * 1536)
    write_skill(ws, "chinese", "中" * 1024)
    write_skill(ws, "terse", "short one")

    schemas = _build_composer_schemas(ws, skill_invocation_enabled=True)
    schema = _find_skill_schema(schemas)
    assert schema is not None
    desc = schema["function"]["parameters"]["properties"]["skill"]["description"]
    roster = desc.split("Available: ", 1)[1]
    entries = dict(
        entry.split(" — ", 1) for entry in roster.split("; ") if " — " in entry
    )
    assert MENU_DESCRIPTION_MAX_TOKENS == 384
    assert len(entries["verbose"]) == 1536
    assert entries["verbose"].startswith("x" * 100)
    assert entries["verbose"].endswith("(truncated)")
    assert entries["chinese"].startswith("中" * 100)
    assert entries["chinese"].endswith("(truncated)")
    assert estimate_menu_tokens(entries["chinese"]) <= MENU_DESCRIPTION_MAX_TOKENS
    assert len(entries["chinese"]) < 400
    # A description inside the cap is untouched.
    assert entries["exact"] == "y" * 1536
    assert entries["terse"] == "short one"


def test_arguments_placeholder_excised_from_the_roster(tmp_path: Path) -> None:
    """The menu is a model-visible surface: a description carrying
    ``$ARGUMENTS`` (a Claude Code-ism) has the token excised, same rule as the
    activation render — Noeta has no argument channel to substitute from."""
    ws = tmp_path / "ws"
    ws.mkdir()
    write_skill(ws, "argy", "Summarize $ARGUMENTS into a report")

    schemas = _build_composer_schemas(ws, skill_invocation_enabled=True)
    schema = _find_skill_schema(schemas)
    assert schema is not None
    desc = schema["function"]["parameters"]["properties"]["skill"]["description"]
    assert "$ARGUMENTS" not in desc
    assert "Summarize into a report" in desc


# ---------------------------------------------------------------------------
# The roster budget — a total cap on the menu: short summaries, then name-only
# ---------------------------------------------------------------------------

def _roster_entries(schema: dict[str, Any]) -> dict[str, str]:
    """``name → summary`` as the rendered roster shows them (``""`` = bare name)."""
    desc = schema["function"]["parameters"]["properties"]["skill"]["description"]
    roster = desc.split("Available: ", 1)[1]
    out: dict[str, str] = {}
    for entry in roster.split("; "):
        name, _, summary = entry.partition(" — ")
        out[name] = summary
    return out


def _registry(*skills: tuple[str, str, int]) -> SkillRegistry:
    """A synthetic registry of ``(name, description, priority)`` rows."""
    return SkillRegistry(
        {
            name: SkillDescription(
                name=name, description=desc, body="b", priority=priority
            )
            for name, desc, priority in skills
        }
    )


def _schema_for(registry: SkillRegistry, **kwargs: Any) -> dict[str, Any]:
    mount = make_skills_control_tool(registry, **kwargs)(_FlagCtx())
    assert mount is not None
    return mount.schema


def test_estimate_counts_cjk_characters_one_each() -> None:
    """A Chinese summary of N characters costs about N tokens, not N/4 — the
    kernel's ``chars/4`` would make a Chinese roster look four times cheaper
    than it is."""
    chinese = "飞书审批：查询和处理审批待办"
    assert estimate_menu_tokens(chinese) == len(chinese)
    assert estimate_menu_tokens("Writes Python code for you") == 7
    assert estimate_menu_tokens("") == 0
    # Mixed text is the sum of both parts.
    assert estimate_menu_tokens("abcd" + "中文") == 1 + 2


def test_under_budget_roster_is_byte_identical_to_the_unbudgeted_one() -> None:
    """Below the budget nothing changes: today's rosters keep their bytes."""
    registry = _registry(("alpha", "first", 100), ("beta", "second", 100))
    tight = _schema_for(registry, menu_budget_tokens=DEFAULT_MENU_BUDGET_TOKENS)
    loose = _schema_for(registry, menu_budget_tokens=10_000_000)
    assert _canonical(tight) == _canonical(loose)
    assert _roster_entries(tight) == {"alpha": "first", "beta": "second"}


def test_over_budget_shortens_every_summary_and_keeps_the_top_ranked_one_full(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Past the budget every skill first gets its short summary; the leftover
    upgrades summaries back to full from the top of the keep order. The
    ``enum`` still lists every skill, and the operator gets one warning naming
    the three counts and the knob."""
    registry = _registry(
        ("aaa", "x" * 400, 100),
        ("bbb", "y" * 400, 100),
        ("ccc", "z" * 400, 100),
    )
    # Names ~6 tokens, three short summaries 24 each, one upgrade to the full
    # 400-char summary 76 more: 160 pays for exactly one upgrade.
    with caplog.at_level(logging.WARNING, logger="noeta.builtins.skills"):
        schema = _schema_for(
            registry, menu_budget_tokens=160, menu_rank={"ccc": 5.0}
        )
    prop = schema["function"]["parameters"]["properties"]["skill"]
    assert prop["enum"] == ["aaa", "bbb", "ccc"]
    assert _roster_entries(schema) == {
        "aaa": short_summary("x" * 400),
        "bbb": short_summary("y" * 400),
        "ccc": "z" * 400,
    }
    assert (
        "of 3 skills, 1 keep a full summary, 2 shortened, 0 listed by name only"
        in caplog.text
    )
    assert "menu_budget_tokens" in caplog.text


def test_name_only_starts_when_the_short_summaries_overflow(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When even the short forms do not fit, the bottom of the keep order is
    listed by name only — and then no skill keeps a full summary."""
    registry = _registry(
        ("aaa", "x" * 400, 100),
        ("bbb", "y" * 400, 100),
        ("ccc", "z" * 400, 100),
    )
    with caplog.at_level(logging.WARNING, logger="noeta.builtins.skills"):
        schema = _schema_for(registry, menu_budget_tokens=60, menu_rank={"ccc": 5.0})
    prop = schema["function"]["parameters"]["properties"]["skill"]
    assert prop["enum"] == ["aaa", "bbb", "ccc"]
    assert _roster_entries(schema) == {
        "aaa": short_summary("x" * 400),
        "bbb": "",
        "ccc": short_summary("z" * 400),
    }
    assert "0 keep a full summary, 2 shortened, 1 listed by name only" in caplog.text


def test_short_summary_is_the_first_sentence_clipped_to_the_cap() -> None:
    """The short form keeps what an author puts first: a summary within the
    cap is unchanged, a longer one keeps its first sentence (ASCII or CJK
    sentence end), and a first sentence still too long is clipped with ``…``."""
    assert MENU_SHORT_SUMMARY_MAX_TOKENS == 24
    assert short_summary("Short one. Really.") == "Short one. Really."
    assert (
        short_summary("Operate TCE via bytedcli. " + "long " * 50)
        == "Operate TCE via bytedcli."
    )
    assert (
        short_summary("用费曼技巧把复杂的东西讲透。" + "核心" * 30)
        == "用费曼技巧把复杂的东西讲透。"
    )
    # A dot inside a token (a version number) is not a sentence end.
    assert short_summary("Version 3.5 " + "adds things " * 20).endswith("…")
    for long_first in ("x" * 400, "飞" * 100 + "。"):
        clipped = short_summary(long_first)
        assert clipped.endswith("…")
        assert estimate_menu_tokens(clipped) <= MENU_SHORT_SUMMARY_MAX_TOKENS
        assert long_first.startswith(clipped[:-1])


def test_depth_pass_waits_until_every_skill_has_a_short_summary() -> None:
    """A leftover too small for one skill's short summary never pays for
    another skill's upgrade: a full summary never sits beside a bare name."""
    # Full is 27 tokens, its first sentence 21: the upgrade costs only 6.
    cheap_upgrade = "中" * 20 + "。" + "文" * 6
    entries = (("aaa", cheap_upgrade), ("zzz", "z" * 400))
    keep_order = ("aaa", "zzz")
    names = estimate_menu_tokens("aaa; ") + estimate_menu_tokens("zzz; ")
    aaa_short = estimate_menu_tokens(
        f"aaa — {short_summary(cheap_upgrade)}; "
    ) - estimate_menu_tokens("aaa; ")
    zzz_short = estimate_menu_tokens(
        f"zzz — {short_summary('z' * 400)}; "
    ) - estimate_menu_tokens("zzz; ")

    # Room for aaa's short form plus 10: zzz's short form (24) does not fit,
    # so aaa stays short although its 6-token upgrade would.
    tight = fit_menu_to_budget(entries, keep_order, names + aaa_short + 10)
    assert tight == {"aaa": short_summary(cheap_upgrade), "zzz": ""}
    # Once zzz's short form fits too, the same 10 pays for aaa's upgrade.
    roomy = fit_menu_to_budget(entries, keep_order, names + aaa_short + zzz_short + 10)
    assert roomy == {"aaa": cheap_upgrade, "zzz": short_summary("z" * 400)}


def test_a_measured_roster_profile_lists_every_skill_with_a_summary() -> None:
    """The case that motivated the short step: 67 skills with ~120-token
    summaries (half English, half Chinese) at the 2000-token budget of a
    200k window. Before it, 50 of them were listed by name only."""
    rows = []
    for i in range(67):
        if i % 2:
            desc = (
                f"Operates platform {i} through its command line: list, search, "
                "get, create, update and delete resources in every region. "
                + "Use when a task mentions it. " * 12
            )
        else:
            desc = f"飞书平台{i}的操作：查询、创建、更新、删除资源，管理权限与成员。" + "适用于相关任务。" * 12
        rows.append((f"skill-{i:02d}", desc, 100))
    registry = _registry(*rows)
    schema = _schema_for(registry, menu_budget_tokens=2000)
    entries = _roster_entries(schema)
    assert len(entries) == 67
    assert all(entries.values())


def test_under_budget_logs_no_warning(caplog: pytest.LogCaptureFixture) -> None:
    registry = _registry(("aaa", "short", 100))
    with caplog.at_level(logging.WARNING, logger="noeta.builtins.skills"):
        _schema_for(registry, menu_budget_tokens=1000)
    assert "over budget" not in caplog.text


def test_keep_order_rank_beats_tier_beats_priority_beats_name(
    tmp_path: Path,
) -> None:
    """The keep order is host rank (desc), then merge tier (workspace-local
    first), then frontmatter ``priority`` (asc), then name."""
    borrowed = tmp_path / "borrowed"
    workspace = tmp_path / "ws"
    write_skill_raw(
        borrowed, "low-tier", "---\nname: low-tier\ndescription: d\n---\nb\n"
    )
    write_skill_raw(
        borrowed,
        "ranked",
        "---\nname: ranked\ndescription: d\npriority: 900\n---\nb\n",
    )
    write_skill_raw(
        workspace / ".noeta" / "skills",
        "prio-10",
        "---\nname: prio-10\ndescription: d\npriority: 10\n---\nb\n",
    )
    write_skill_raw(
        workspace / ".noeta" / "skills",
        "prio-50-b",
        "---\nname: prio-50-b\ndescription: d\npriority: 50\n---\nb\n",
    )
    write_skill_raw(
        workspace / ".noeta" / "skills",
        "prio-50-a",
        "---\nname: prio-50-a\ndescription: d\npriority: 50\n---\nb\n",
    )
    registry = load_workspace_skills(workspace, lower_skill_dirs=[borrowed])
    assert registry.tier_of("low-tier") == 0
    assert registry.tier_of("prio-10") == 1

    order = menu_keep_order(registry, registry.names(), {"ranked": 1.0})
    assert order == ("ranked", "prio-10", "prio-50-a", "prio-50-b", "low-tier")
    # No rank: tier decides first, then priority, then name.
    assert menu_keep_order(registry, registry.names()) == (
        "prio-10",
        "prio-50-a",
        "prio-50-b",
        "low-tier",
        "ranked",
    )


def test_merge_stamps_the_overlay_one_tier_above_the_base() -> None:
    base = _registry(("a", "d", 100), ("shadowed", "old", 100))
    overlay = _registry(("shadowed", "new", 100), ("b", "d", 100))
    merged = merge_skill_registries(base, overlay)
    assert merged.tier_of("a") == 0
    assert merged.tier_of("b") == 1
    # A clash takes the overlay's description AND its tier.
    assert merged.get("shadowed").description == "new"
    assert merged.tier_of("shadowed") == 1
    # A registry built directly has no tiers to report.
    assert base.tier_of("a") == 0 and base.tier_of("missing") == 0


def test_fit_is_greedy_so_a_shorter_later_summary_can_still_fit() -> None:
    """Claude Code's greedy: a summary that does not fit is skipped, but a
    later, shorter one in the keep order still gets in."""
    entries = (("big", "x" * 400), ("small", "y" * 20), ("tiny", "z" * 4))
    keep_order = ("big", "small", "tiny")
    # Baseline (names + separators) ~6 tokens; big's short form ≈ 24,
    # small ≈ 6, tiny ≈ 2 (both within the cap, so their own short form).
    fitted = fit_menu_to_budget(entries, keep_order, budget_tokens=20)
    assert fitted == {"big": "", "small": "y" * 20, "tiny": "z" * 4}


def test_fit_drops_every_summary_when_names_alone_exceed_the_budget() -> None:
    entries = tuple((f"skill-{i:03d}", "summary") for i in range(50))
    fitted = fit_menu_to_budget(entries, [n for n, _ in entries], 10)
    assert fitted == {n: "" for n, _ in entries}
    # Nothing to drop when nothing has a summary.
    bare = tuple((n, "") for n, _ in entries)
    assert fit_menu_to_budget(bare, [n for n, _ in bare], 10) == dict(bare)


def test_menu_bytes_do_not_depend_on_rank_when_under_budget() -> None:
    """Rank only decides which summaries survive; it never reorders the
    name-sorted menu, so an under-budget roster is rank-independent."""
    registry = _registry(("alpha", "first", 100), ("beta", "second", 100))
    plain = _schema_for(registry, menu_budget_tokens=1000)
    ranked = _schema_for(registry, menu_budget_tokens=1000, menu_rank={"beta": 9})
    assert _canonical(plain) == _canonical(ranked)


def test_session_pack_reads_budget_and_rank_from_its_config(
    tmp_path: Path,
) -> None:
    """``plugin_config["skills"]["menu_budget_tokens"]`` and ``["menu_rank"]``
    reach the mount: the top-ranked skill keeps its full summary, the rest are
    shortened."""
    ws = tmp_path / "ws"
    ws.mkdir()
    write_skill(ws, "aaa", "a" * 400)
    write_skill(ws, "bbb", "b" * 400)
    write_skill(ws, "ccc", "c" * 400)
    inputs = build_session_inputs(
        **default_factory_kwargs(),
        workspace_dir=ws,
        system_prompt="you are helpful",
        allowed_tools=frozenset({"read"}),
        content_store=InMemoryContentStore(),
        model="stub-model",
        compaction=COMPACTION_OFF,
        budget=Budget(),
        capability_flags={"skill_invocation": True},
        plugin_config={
            "fs": {"write_mode": FsWriteMode.DRY_RUN, "shell_mode": ShellMode.OFF},
            "skills": {"menu_budget_tokens": 160, "menu_rank": {"bbb": 2, "aaa": 1}},
        },
    )
    schema = _find_skill_schema(list(inputs.composer._control_action_schemas))
    assert schema is not None
    entries = _roster_entries(schema)
    assert entries == {
        "aaa": short_summary("a" * 400),
        "bbb": "b" * 400,
        "ccc": short_summary("c" * 400),
    }


@pytest.mark.parametrize(
    "bad",
    [{"menu_budget_tokens": 0}, {"menu_budget_tokens": "2000"}, {"menu_budget_tokens": True}],
)
def test_session_pack_rejects_a_bad_menu_budget(tmp_path: Path, bad: dict) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    write_skill(ws, "aaa", "summary")
    with pytest.raises(ValueError, match="menu_budget_tokens"):
        build_session_inputs(
            **default_factory_kwargs(),
            workspace_dir=ws,
            system_prompt="you are helpful",
            allowed_tools=frozenset({"read"}),
            content_store=InMemoryContentStore(),
            model="stub-model",
            compaction=COMPACTION_OFF,
            budget=Budget(),
            capability_flags={"skill_invocation": True},
            plugin_config={
                "fs": {"write_mode": FsWriteMode.DRY_RUN, "shell_mode": ShellMode.OFF},
                "skills": bad,
            },
        )


@pytest.mark.parametrize(
    "bad",
    [
        {"menu_rank": ["aaa"]},
        {"menu_rank": {"aaa": "high"}},
        {"menu_rank": {1: 2}},
        {"menu_rank": {"aaa": float("nan")}},
        {"menu_rank": {"aaa": float("inf")}},
    ],
)
def test_session_pack_rejects_a_bad_menu_rank(tmp_path: Path, bad: dict) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    write_skill(ws, "aaa", "summary")
    with pytest.raises(ValueError, match="menu_rank"):
        build_session_inputs(
            **default_factory_kwargs(),
            workspace_dir=ws,
            system_prompt="you are helpful",
            allowed_tools=frozenset({"read"}),
            content_store=InMemoryContentStore(),
            model="stub-model",
            compaction=COMPACTION_OFF,
            budget=Budget(),
            capability_flags={"skill_invocation": True},
            plugin_config={
                "fs": {"write_mode": FsWriteMode.DRY_RUN, "shell_mode": ShellMode.OFF},
                "skills": bad,
            },
        )


def test_lower_dirs_are_stamped_by_scope_tier_not_by_directory(
    tmp_path: Path,
) -> None:
    """``lower_skill_tiers`` stamps each lower dir with its SCOPE's tier: two
    dirs of one scope share it, and the workspace pack sits one above the
    highest. Without the argument each dir is its own tier (a direct caller
    with one dir per scope gets the same answer)."""
    builtin = tmp_path / "builtin"
    borrowed = tmp_path / "borrowed"
    glob = tmp_path / "global"
    workspace = tmp_path / "ws"
    write_skill(builtin, "first-party", "d")
    write_skill(borrowed, "borrowed-one", "d")
    write_skill(glob, "global-one", "d")
    write_skill(workspace, "local-one", "d")
    registry = load_workspace_skills(
        workspace,
        lower_skill_dirs=[builtin, borrowed, glob],
        lower_skill_tiers=[0, 0, 2],
    )
    assert registry.tier_of("first-party") == 0
    assert registry.tier_of("borrowed-one") == 0
    assert registry.tier_of("global-one") == 2
    assert registry.tier_of("local-one") == 3
    # Same scope ⇒ the keep order falls through to priority, then name.
    assert menu_keep_order(registry, ("borrowed-one", "first-party")) == (
        "borrowed-one",
        "first-party",
    )
    # The default: one tier per directory.
    per_dir = load_workspace_skills(workspace, lower_skill_dirs=[builtin, borrowed])
    assert per_dir.tier_of("first-party") == 0
    assert per_dir.tier_of("borrowed-one") == 1
    assert per_dir.tier_of("local-one") == 2
    with pytest.raises(ValueError, match="lower_skill_tiers"):
        load_workspace_skills(
            workspace, lower_skill_dirs=[builtin], lower_skill_tiers=[0, 0]
        )


def test_session_pack_keeps_builtin_plugin_and_borrowed_packs_on_one_tier(
    tmp_path: Path,
) -> None:
    """Over budget, a borrowed (``extra_skill_dirs``) skill does NOT outrank
    the host's own built-in pack: both are the lowest scope, so the keep
    order falls to name. A global-tier skill outranks both."""
    ws = tmp_path / "ws"
    ws.mkdir()
    builtin = tmp_path / "builtin"
    borrowed = tmp_path / "borrowed"
    glob = tmp_path / "global"
    write_skill(builtin, "aaa", "a" * 400)
    write_skill(borrowed, "bbb", "b" * 400)
    write_skill(glob, "ccc", "c" * 400)

    def roster(skills_cfg: dict[str, Any]) -> dict[str, str]:
        inputs = build_session_inputs(
            **default_factory_kwargs(),
            workspace_dir=ws,
            system_prompt="you are helpful",
            allowed_tools=frozenset({"read"}),
            content_store=InMemoryContentStore(),
            model="stub-model",
            compaction=COMPACTION_OFF,
            budget=Budget(),
            capability_flags={"skill_invocation": True},
            plugin_config={
                "fs": {"write_mode": FsWriteMode.DRY_RUN, "shell_mode": ShellMode.OFF},
                "skills": {"menu_budget_tokens": 160, **skills_cfg},
            },
        )
        schema = _find_skill_schema(list(inputs.composer._control_action_schemas))
        assert schema is not None
        return _roster_entries(schema)

    # Built-in + borrowed only: one tier, name order ⇒ ``aaa`` keeps its full
    # summary although the borrowed dir was folded later.
    assert roster(
        {"builtin_skills_dirs": [builtin], "extra_skill_dirs": [borrowed]}
    ) == {"aaa": "a" * 400, "bbb": short_summary("b" * 400)}
    # A global-tier skill outranks both.
    assert roster(
        {
            "builtin_skills_dirs": [builtin],
            "extra_skill_dirs": [borrowed],
            "global_skills_dir": glob,
        }
    ) == {
        "aaa": short_summary("a" * 400),
        "bbb": short_summary("b" * 400),
        "ccc": "c" * 400,
    }


def test_fit_charges_every_entry_even_outside_the_keep_order() -> None:
    """A partial keep order cannot smuggle summaries past the budget: names
    it leaves out are fitted last; duplicates in the keep order count once;
    duplicate entries are a caller error."""
    entries = tuple((f"s{i}", "x" * 400) for i in range(10))
    fitted = fit_menu_to_budget(entries, ["s0", "s0"], budget_tokens=10)
    assert fitted == {n: "" for n, _ in entries}
    # With room for exactly one short summary the keep order's pick gets it
    # and the left-out names come after it.
    short = short_summary("x" * 400)
    names = sum(estimate_menu_tokens(f"{n}; ") for n, _ in entries)
    one = names + estimate_menu_tokens(f"s7 — {short}; ") - estimate_menu_tokens("s7; ")
    fitted = fit_menu_to_budget(entries, ["s7"], budget_tokens=one)
    assert fitted["s7"] == short
    assert [n for n, summary in fitted.items() if not summary] == [
        n for n, _ in entries if n != "s7"
    ]
    with pytest.raises(ValueError, match="duplicate"):
        fit_menu_to_budget((("a", "x"), ("a", "y")), ["a"], budget_tokens=100)


def test_estimate_counts_the_cjk_blocks_between_kana_and_ideographs() -> None:
    """Bopomofo, Hangul Jamo, enclosed CJK and CJK compatibility characters
    sit in blocks the first cut skipped; every one is one token."""
    for ch in ("\u3105", "\u1100", "\u3131", "\u3231", "\u3300", "\u4e2d", "\u30a2"):
        assert estimate_menu_tokens(ch * 8) == 8, hex(ord(ch))
    assert estimate_menu_tokens("abcdefgh") == 2
