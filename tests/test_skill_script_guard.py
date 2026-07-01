"""Phase 4.5 Issue E — `run_skill_script` always-approval guard invariant.

Proves the `PermissionGuard` E precheck: a ``skill_script_tools`` call can
only ever resolve to ``deny`` (fail-closed) or ``require_approval`` —
**never** ``allow`` — and that this does **not** depend on
``require_approval_tools`` wiring (the architect's central requirement).
"""

from __future__ import annotations

from noeta.guards.permission import PermissionGuard, PermissionPolicy
from noeta.protocols.decisions import ToolCall
from noeta.protocols.hooks import GuardContext, ProposedToolCall, Verdict


SCRIPT = ("s", "scripts/x.sh")


def _guard(*, require_approval_tools: frozenset[str] = frozenset()) -> PermissionGuard:
    return PermissionGuard(
        PermissionPolicy(
            skill_script_tools=frozenset({"run_skill_script"}),
            skill_scripts=frozenset({SCRIPT}),
            require_approval_tools=require_approval_tools,
        ),
        tools={},
    )


def _verdict(
    guard: PermissionGuard, *, skill: object, relpath: object, active: tuple[str, ...]
) -> Verdict:
    action = ProposedToolCall(
        call=ToolCall(
            tool_name="run_skill_script",
            arguments={"skill": skill, "relpath": relpath},
            call_id="c1",
        )
    )
    return guard.check(action, GuardContext(task_id="t", active_skills=active)).verdict


def test_active_discovered_requires_approval() -> None:
    g = _guard()
    assert _verdict(g, skill="s", relpath="scripts/x.sh", active=("s",)) is Verdict.REQUIRE_APPROVAL


def test_require_approval_even_with_empty_require_approval_tools() -> None:
    # The always-approval invariant is the E precheck, NOT require_approval_tools.
    g = _guard(require_approval_tools=frozenset())
    assert _verdict(g, skill="s", relpath="scripts/x.sh", active=("s",)) is Verdict.REQUIRE_APPROVAL


def test_non_active_skill_denied() -> None:
    g = _guard()
    assert _verdict(g, skill="s", relpath="scripts/x.sh", active=("other",)) is Verdict.DENY


def test_undiscovered_script_denied() -> None:
    g = _guard()
    assert _verdict(g, skill="s", relpath="scripts/ghost.sh", active=("s",)) is Verdict.DENY


def test_missing_or_empty_args_denied() -> None:
    g = _guard()
    assert _verdict(g, skill="", relpath="scripts/x.sh", active=("s",)) is Verdict.DENY
    assert _verdict(g, skill="s", relpath="", active=("s",)) is Verdict.DENY
    assert _verdict(g, skill=None, relpath="scripts/x.sh", active=("s",)) is Verdict.DENY
    assert _verdict(g, skill="s", relpath=123, active=("s",)) is Verdict.DENY


def test_script_tool_never_returns_allow() -> None:
    """Exhaustively: across active/non-active/discovered/undiscovered/
    malformed, the script tool resolves to deny or require_approval only."""
    g = _guard()
    cases = [
        ("s", "scripts/x.sh", ("s",)),
        ("s", "scripts/x.sh", ()),
        ("s", "scripts/ghost.sh", ("s",)),
        ("", "scripts/x.sh", ("s",)),
        ("other", "scripts/x.sh", ("s", "other")),
    ]
    for skill, rel, active in cases:
        v = _verdict(g, skill=skill, relpath=rel, active=active)
        assert v in (Verdict.DENY, Verdict.REQUIRE_APPROVAL)  # never ALLOW


def test_skill_script_independent_of_b_allowed_tools() -> None:
    """`run_skill_script` is gated only by the E precheck (active skill +
    discovered + approval) — it is NOT allowed/denied by a skill's
    `allowed-tools` (Issue B). Two separate lines."""
    guard = PermissionGuard(
        PermissionPolicy(
            skill_script_tools=frozenset({"run_skill_script"}),
            skill_scripts=frozenset({SCRIPT}),
            # B enforcement ON, active skill grants only [Read]:
            skill_tool_enforcement="approval",
            skill_allowed_tools=(("s", frozenset({"read_file"})),),
        ),
        tools={},
    )
    ctx = GuardContext(task_id="t", active_skills=("s",))

    def _v(tool: str, args: dict[str, object]) -> Verdict:
        return guard.check(
            ProposedToolCall(call=ToolCall(tool_name=tool, arguments=args, call_id="c")),
            ctx,
        ).verdict

    # run_skill_script → E require_approval, unaffected by B's [Read] grant.
    assert _v("run_skill_script", {"skill": "s", "relpath": "scripts/x.sh"}) is Verdict.REQUIRE_APPROVAL
    # B still governs ordinary tools: read_file granted, write gated.
    assert _v("read_file", {"path": "x"}) is Verdict.ALLOW
    assert _v("write", {"path": "x"}) is Verdict.REQUIRE_APPROVAL


def test_non_script_tool_unaffected() -> None:
    # a plain tool is not gated by the skill-script branch.
    g = _guard()
    action = ProposedToolCall(
        call=ToolCall(tool_name="read_file", arguments={"path": "x"}, call_id="c2")
    )
    v = g.check(action, GuardContext(task_id="t", active_skills=("s",))).verdict
    assert v is Verdict.ALLOW
