"""Phase 4.5 Issue B — skill `allowed-tools` enforcement (L2 unit).

Covers the conservative parser, the exact 1:1 Claude→Noeta alias map, and
the `PermissionGuard` enforcement decision driven by
`GuardContext.active_skills`. End-to-end harness + replay live in
`test_code_skill_enforcement.py`.
"""

from __future__ import annotations

import logging

import noeta.guards.permission as permission_mod
from noeta.guards.permission import (
    PermissionGuard,
    PermissionPolicy,
)
from noeta.policies.skill_tools import (
    CLAUDE_TO_NOETA_TOOL as _CLAUDE_TO_NOETA_TOOL,
    parse_allowed_tools as _parse_allowed_tools,
    resolve_skill_allowed_tools,
)
from noeta.protocols.decisions import ToolCall
from noeta.protocols.hooks import GuardContext, ProposedToolCall, Verdict


def test_kernel_guard_carries_no_claude_vocab() -> None:
    """The Claude→Noeta alias map + parser moved to noeta-sdk; the kernel
    guard must no longer carry any product tool vocabulary (mechanism-vs-material)."""
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


# ---------------------------------------------------------------------------
# alias map — no Claude name leaks into the Noeta namespace
# ---------------------------------------------------------------------------


def test_alias_map_is_exact_1to1() -> None:
    # Read maps to the renamed `read`; LS dropped (list_dir retired).
    assert _CLAUDE_TO_NOETA_TOOL == {
        "Read": "read",
        "Glob": "glob",
        "Grep": "grep",
        "Write": "write",
        "Edit": "edit",
        "Bash": "shell_run",
    }


def test_no_claude_name_appears_in_noeta_tool_set() -> None:
    claude_names = set(_CLAUDE_TO_NOETA_TOOL)
    noeta_names = set(_CLAUDE_TO_NOETA_TOOL.values())
    assert claude_names.isdisjoint(noeta_names)


def test_guard_unknown_claude_name_grants_nothing() -> None:
    g = _guard(raw=(("s", "[Bogus]"),))
    # 's' active, declared but maps to nothing → write gated.
    assert _check(g, "read", ("s",)) is Verdict.REQUIRE_APPROVAL


def test_partial_unknown_invalidates_whole_declaration() -> None:
    """P1 (architect): a single unknown token degrades the WHOLE grant to
    empty — `[Read, Bogus]` must NOT keep `read` allowed; a typo in
    a security-relevant grant gates everything until fixed."""
    g = _guard(raw=(("s", "[Read, Bogus]"),))
    assert _check(g, "read", ("s",)) is Verdict.REQUIRE_APPROVAL
    g_deny = _guard(raw=(("s", "[Read, Bogus]"),), mode="deny")
    assert _check(g_deny, "read", ("s",)) is Verdict.DENY


# ---------------------------------------------------------------------------
# guard enforcement
# ---------------------------------------------------------------------------


def test_granted_tool_allows_other_requires_approval() -> None:
    g = _guard(raw=(("s", "[Read]"),))
    assert _check(g, "read", ("s",)) is Verdict.ALLOW
    assert _check(g, "write", ("s",)) is Verdict.REQUIRE_APPROVAL


def test_deny_mode_fails_closed() -> None:
    g = _guard(raw=(("s", "[Read]"),), mode="deny")
    assert _check(g, "read", ("s",)) is Verdict.ALLOW
    assert _check(g, "write", ("s",)) is Verdict.DENY


def test_no_declaring_active_skill_enforcement_off() -> None:
    g = _guard(raw=(("s", "[Read]"),))
    # 's' is NOT active → no declaring active skill → enforcement off.
    assert _check(g, "write", ("other",)) is Verdict.ALLOW
    assert _check(g, "write", ()) is Verdict.ALLOW


def test_union_over_multiple_active_skills() -> None:
    g = _guard(raw=(("a", "[Read]"), ("b", "[Write]")))
    # union grants read + write; grep is outside → gated.
    assert _check(g, "read", ("a", "b")) is Verdict.ALLOW
    assert _check(g, "write", ("a", "b")) is Verdict.ALLOW
    assert _check(g, "grep", ("a", "b")) is Verdict.REQUIRE_APPROVAL


def test_malformed_declaration_grants_nothing_enforcement_on() -> None:
    g = _guard(raw=(("s", "not-a-list: x"),))
    # declared (so enforcement ON) but parse failed → empty grant →
    # every tool gated.
    assert _check(g, "read", ("s",)) is Verdict.REQUIRE_APPROVAL


def test_mode_off_never_gates() -> None:
    g = _guard(raw=(("s", "[Read]"),), mode="off")
    assert _check(g, "write", ("s",)) is Verdict.ALLOW


def test_malformed_diagnostic_logged_once(caplog) -> None:  # type: ignore[no-untyped-def]
    # The single diagnostic now fires at SDK resolution time (once), not on
    # every guard tool check.
    caplog.set_level(logging.WARNING, logger="noeta.policies.skill_tools")
    g = _guard(raw=(("s", "bad: value"),))
    # resolved once already; multiple checks must not re-log.
    _check(g, "read", ("s",))
    _check(g, "write", ("s",))
    warnings = [
        r for r in caplog.records if "unparseable allowed-tools" in r.getMessage()
    ]
    assert len(warnings) == 1
