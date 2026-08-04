"""Skill ``allowed-tools`` enforcement (unit level).

Covers the conservative parser, the recognised model-visible tool vocabulary,
and the `PermissionGuard` enforcement decision driven by
`GuardContext.active_skills`. End-to-end harness + replay live in
`test_code_skill_enforcement.py`.
"""

from __future__ import annotations

import logging

import noeta.builtins.governance.impl.permission as permission_mod
from noeta.builtins.governance.impl.permission import PermissionGuard
from noeta.runtime.governance import PermissionPolicy
from noeta.builtins.skills.impl.allowed_tools import (
    RECOGNIZED_TOOL_NAMES,
    parse_allowed_tools as _parse_allowed_tools,
    resolve_skill_allowed_tools,
)
from noeta.protocols.decisions import ToolCall
from noeta.protocols.hooks import GuardContext, ProposedToolCall, Verdict


def test_kernel_guard_carries_no_claude_vocab() -> None:
    """The Claude→Noeta alias map + parser live in noeta-sdk; the kernel
    guard carries no product tool vocabulary (mechanism-vs-material)."""
    assert not hasattr(permission_mod, "_CLAUDE_TO_NOETA_TOOL")
    assert not hasattr(permission_mod, "_parse_allowed_tools")
    assert not hasattr(permission_mod, "_alias_to_noeta")


def _check(
    guard: PermissionGuard, tool_name: str, active_skills: tuple[str, ...]
) -> Verdict:
    action = ProposedToolCall(
        call=ToolCall(tool_name=tool_name, arguments={}, call_id="c1")
    )
    ctx = GuardContext(task_id="t", active_skills=active_skills)
    return guard.check(action, ctx).verdict


def _guard(
    *,
    raw: tuple[tuple[str, str], ...],
    mode: str = "approval",
) -> PermissionGuard:
    # The SDK resolves the raw (skill, allowed-tools-string) pairs into
    # neutral noeta tool-name grants BEFORE they reach the kernel guard.
    return PermissionGuard(
        PermissionPolicy(
            skill_tool_enforcement=mode,  # type: ignore[arg-type]
            skill_allowed_tools=resolve_skill_allowed_tools(raw),
        ),
        tools={},
    )


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------


def test_parse_inline_list() -> None:
    assert _parse_allowed_tools("[Read, Glob, Grep, Bash]") == frozenset(
        {"Read", "Glob", "Grep", "Bash"}
    )


def test_parse_bare_comma_list() -> None:
    assert _parse_allowed_tools("Read, Bash") == frozenset({"Read", "Bash"})


def test_parse_empty_list_is_empty_grant() -> None:
    assert _parse_allowed_tools("[]") == frozenset()
    assert _parse_allowed_tools("") == frozenset()


def test_parse_whitespace_trimmed() -> None:
    assert _parse_allowed_tools("[  Read ,  Bash ]") == frozenset(
        {"Read", "Bash"}
    )


def test_parse_malformed_returns_none_not_widened() -> None:
    # quoted / colon / nested / space-in-token forms we won't read →
    # None (fail-safe), NEVER a non-empty "all tools" set.
    assert _parse_allowed_tools('["Read"]') is None
    assert _parse_allowed_tools("Read: true") is None
    assert _parse_allowed_tools("{Read, Bash}") is None
    assert _parse_allowed_tools("Read Bash") is None


def test_parse_argument_spec_yields_bare_name(
    caplog,  # type: ignore[no-untyped-def]
) -> None:
    """(c) ``Bash(git:*)`` grants ``Bash`` and says out loud that the
    argument-level spec is not what gates the call."""
    caplog.set_level(
        logging.WARNING, logger="noeta.builtins.skills.impl.allowed_tools"
    )
    assert _parse_allowed_tools("[Bash(git:*)]", skill="s") == frozenset(
        {"Bash"}
    )
    assert any(
        "argument-level" in r.getMessage() and "not enforced" in r.getMessage()
        for r in caplog.records
    )


def test_parse_argument_spec_comma_does_not_split_the_entry() -> None:
    """A comma inside a spec belongs to the spec, not to the list."""
    assert _parse_allowed_tools(
        "[Bash(git status:*, git diff:*), Read]"
    ) == frozenset({"Bash", "Read"})


# ---------------------------------------------------------------------------
# recognition set — the model-visible vocabulary a session can mount
# ---------------------------------------------------------------------------


def test_recognition_set_covers_the_mountable_surface() -> None:
    """The set is "every model-visible tool name a session can mount", not the
    six fs names it started as: a real Claude Code skill routinely names
    ``WebFetch`` / ``TodoWrite``, and a browser or memory session mounts more."""
    assert {
        "Read",
        "Glob",
        "Grep",
        "Edit",
        "Write",
        "Bash",
        "BashOutput",
        "KillShell",
        "WebFetch",
        "WebSearch",
        "TodoWrite",
        "AskUserQuestion",
        "Task",
        "skill",
        "run_skill_script",
        "open_app",
        "memory_write",
        "memory_read",
        "memory_search",
        "memory_archive",
        "browser_navigate",
        "browser_click",
        "browser_type",
        "browser_extract",
        "browser_screenshot",
    } == set(RECOGNIZED_TOOL_NAMES)


def test_recognition_set_matches_the_live_tool_names() -> None:
    """Pinned against the tool classes themselves, so a rename on the tool
    surface cannot silently turn a valid grant into a dropped one."""
    from noeta.builtins.fs.impl.read import GlobTool, GrepTool, ReadFileTool
    from noeta.builtins.fs.impl.edit import ReplaceTextTool, WriteFileTool
    from noeta.builtins.fs.impl.shell import (
        ShellKillTool,
        ShellPollTool,
        ShellRunTool,
    )
    from noeta.builtins.web.impl.fetch import WebFetchTool
    from noeta.builtins.web.impl.search import WebSearchTool

    for cls in (
        ReadFileTool,
        GlobTool,
        GrepTool,
        ReplaceTextTool,
        WriteFileTool,
        ShellRunTool,
        ShellPollTool,
        ShellKillTool,
        WebFetchTool,
        WebSearchTool,
    ):
        assert cls.name in RECOGNIZED_TOOL_NAMES


def test_guard_unrecognized_name_grants_nothing() -> None:
    g = _guard(raw=(("s", "[Bogus]"),))
    # 's' active, declared but every entry dropped → nothing granted.
    assert _check(g, "Read", ("s",)) is Verdict.REQUIRE_APPROVAL


def test_unrecognized_entry_is_dropped_and_the_rest_stands() -> None:
    """Per-name fail-safe (D11): ``[Read, Bogus]`` keeps ``Read`` and drops
    ``Bogus``. This deliberately replaces the old whole-declaration-empty rule,
    which gated every legitimate call of any skill that named one tool this
    build does not mount — a harsher penalty than the typo it caught."""
    g = _guard(raw=(("s", "[Read, Bogus]"),))
    assert _check(g, "Read", ("s",)) is Verdict.ALLOW
    # ...and enforcement stays ON: the dropped name grants nothing.
    assert _check(g, "Write", ("s",)) is Verdict.REQUIRE_APPROVAL
    g_deny = _guard(raw=(("s", "[Read, Bogus]"),), mode="deny")
    assert _check(g_deny, "Write", ("s",)) is Verdict.DENY


def test_current_vocabulary_names_are_granted() -> None:
    """(b) ``allowed-tools: [Read, WebFetch]`` grants both — the whole point of
    replacing the six-entry map."""
    g = _guard(raw=(("s", "[Read, WebFetch]"),))
    assert _check(g, "Read", ("s",)) is Verdict.ALLOW
    assert _check(g, "WebFetch", ("s",)) is Verdict.ALLOW
    assert _check(g, "Write", ("s",)) is Verdict.REQUIRE_APPROVAL


def test_argument_spec_grant_reaches_the_guard() -> None:
    """(c) end of the chain: ``Bash(git:*)`` gates on ``Bash``, and the spec
    itself is not a second, narrower gate here."""
    g = _guard(raw=(("s", "[Bash(git:*)]"),))
    assert _check(g, "Bash", ("s",)) is Verdict.ALLOW
    assert _check(g, "Read", ("s",)) is Verdict.REQUIRE_APPROVAL


# ---------------------------------------------------------------------------
# guard enforcement
# ---------------------------------------------------------------------------


def test_granted_tool_allows_other_requires_approval() -> None:
    g = _guard(raw=(("s", "[Read]"),))
    assert _check(g, "Read", ("s",)) is Verdict.ALLOW
    assert _check(g, "Write", ("s",)) is Verdict.REQUIRE_APPROVAL


def test_deny_mode_fails_closed() -> None:
    g = _guard(raw=(("s", "[Read]"),), mode="deny")
    assert _check(g, "Read", ("s",)) is Verdict.ALLOW
    assert _check(g, "Write", ("s",)) is Verdict.DENY


def test_no_declaring_active_skill_enforcement_off() -> None:
    g = _guard(raw=(("s", "[Read]"),))
    # 's' is NOT active → no declaring active skill → enforcement off.
    assert _check(g, "Write", ("other",)) is Verdict.ALLOW
    assert _check(g, "Write", ()) is Verdict.ALLOW


def test_union_over_multiple_active_skills() -> None:
    g = _guard(raw=(("a", "[Read]"), ("b", "[Write]")))
    # union grants Read + write; Grep is outside → gated.
    assert _check(g, "Read", ("a", "b")) is Verdict.ALLOW
    assert _check(g, "Write", ("a", "b")) is Verdict.ALLOW
    assert _check(g, "Grep", ("a", "b")) is Verdict.REQUIRE_APPROVAL


def test_malformed_declaration_grants_nothing_enforcement_on() -> None:
    g = _guard(raw=(("s", "not-a-list: x"),))
    # declared (so enforcement ON) but parse failed → empty grant →
    # every tool gated.
    assert _check(g, "Read", ("s",)) is Verdict.REQUIRE_APPROVAL


def test_mode_off_never_gates() -> None:
    g = _guard(raw=(("s", "[Read]"),), mode="off")
    assert _check(g, "Write", ("s",)) is Verdict.ALLOW


def test_malformed_diagnostic_logged_once(caplog) -> None:  # type: ignore[no-untyped-def]
    # The single diagnostic fires once at SDK resolution time, not on every
    # guard tool check.
    caplog.set_level(logging.WARNING, logger="noeta.builtins.skills.impl.allowed_tools")
    g = _guard(raw=(("s", "bad: value"),))
    # Already resolved once; multiple checks must not re-log.
    _check(g, "read", ("s",))
    _check(g, "Write", ("s",))
    warnings = [
        r for r in caplog.records if "unparseable allowed-tools" in r.getMessage()
    ]
    assert len(warnings) == 1
