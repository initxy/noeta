"""The scout prompts and the shell allowlist may not drift apart.

``explore.md`` / ``plan.md`` tell a read-only sub-agent which shell commands to
reach for. Under ``permission_mode="default"`` every ``Bash`` call outside
``DEFAULT_SHELL_RULES`` suspends the task for human approval — so a prompt that
recommends a command the allowlist does not carry turns routine investigation
into an approval storm nobody asked for, and a sub-agent has no human to ask.

Two halves, both pinned here:

* every command the prompts *name* is allowlisted (parsed out of the prompt
  text itself, so re-wording the parenthetical re-runs the check);
* a representative real invocation of each — flags included — passes the
  **wired** approval predicate the ``SdkHost`` builds, not a reconstruction of
  it.

Rewriting a prompt to name a new program is therefore a two-file change: the
prompt and the rule table.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable, Mapping

import pytest

from noeta.agent.registry import AgentRegistry
from noeta.builtins.governance.impl.permission import PermissionGuard
from noeta.client.host import SdkHost
from noeta.client.options import Options, compile_options
from noeta.client.parts import default_shell_rules
from noeta.presets import official_specs
from noeta.protocols.messages import LLMResponse, TextBlock, Usage
from noeta.runtime.shell_policy import build_allowlist, command_in_allowlist
from noeta.storage.memory import (
    InMemoryContentStore,
    InMemoryDispatcher,
    InMemoryEventLog,
)
from noeta.testing.fake_llm import FakeLLMProvider


#: The sentence both scout prompts carry, naming the read-only programs the
#: fs tools cannot cover. Parsing it (rather than hard-coding the list) is what
#: keeps this test honest when the prompt is re-worded.
_BASH_CLAUSE = re.compile(
    r"Use `Bash` ONLY for read-only work they cannot cover \(([^)]*)\)"
)

#: One realistic invocation per named program — the shapes the prompts' own
#: guidance produces, flags and paths included. Every one must run silently.
_REPRESENTATIVE_INVOCATIONS: tuple[str, ...] = (
    "ls",
    "ls -la packages",
    "find . -name shell_rules.py",
    "find packages -type f -name *.md",
    "git status",
    "git status --short",
    "git log",
    "git log --oneline -n 20",
    "git log -5 --stat",
    "git log -p packages/noeta-sdk",
    "git log --name-only --no-merges",
    "git diff",
    "git diff --stat",
    "git diff -- packages/noeta-sdk",
)

#: Commands that must STILL need sign-off — the allowlist widening is bounded.
_STILL_GATED: tuple[str, ...] = (
    # ``--ext-diff`` is the one ``git log`` flag that can invoke a
    # repo-configured external program per file.
    "git log --ext-diff",
    "git log --format=%H",
    "git commit -m wip",
    "git add .",
    "rm -rf /",
    "cat packages/noeta-sdk/pyproject.toml",
    "head -n 5 CONTEXT.md",
    "tail -n 5 CONTEXT.md",
)


def _scout_prompt(preset: str) -> str:
    return official_specs()[preset].instructions


def _named_commands(preset: str) -> tuple[str, ...]:
    """The programs the preset's prompt tells the model it may shell out to."""
    match = _BASH_CLAUSE.search(_scout_prompt(preset))
    assert match is not None, (
        f"{preset}.md no longer carries the read-only Bash clause this test "
        f"parses; update the regex together with the prompt"
    )
    return tuple(part.strip() for part in match.group(1).split(",") if part.strip())


# ---------------------------------------------------------------------------
# 1. Prompt text vs. the rule table
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("preset", ["explore", "plan"])
def test_scout_prompt_names_only_allowlisted_commands(preset: str) -> None:
    rules = build_allowlist((), base_rules=default_shell_rules())
    for command in _named_commands(preset):
        assert command_in_allowlist(command, rules), (
            f"{preset}.md recommends {command!r}, which is not in "
            f"DEFAULT_SHELL_RULES — every such call would suspend for approval"
        )


def test_both_scout_prompts_name_the_same_commands() -> None:
    """explore and plan are the same read-only contract; a divergence is a
    sync bug, and this is what keeps the two lists reviewed together."""
    assert _named_commands("explore") == _named_commands("plan")


@pytest.mark.parametrize("preset", ["explore", "plan"])
def test_scout_prompts_no_longer_route_reads_through_the_shell(preset: str) -> None:
    """D8: reading and searching go through ``Read`` / ``Glob`` / ``Grep``.
    ``cat`` / ``head`` / ``tail`` are neither allowlisted nor needed."""
    named = _named_commands(preset)
    for banned in ("cat", "head", "tail"):
        assert banned not in named


# ---------------------------------------------------------------------------
# 2. The wired approval predicate
# ---------------------------------------------------------------------------


def _conditional_approval(
    tmp_path: Path,
) -> Callable[[str, Mapping[str, Any]], bool]:
    """The live per-call shell gate an ``SdkHost`` builds under ``default``.

    Reached through the real ``_build_engine`` wiring (the same route
    ``test_sdk_host_knobs`` uses) rather than by re-composing the allowlist, so
    a change to how the host assembles its rules is visible here.
    """
    spec, _ = compile_options(
        Options(
            system_prompt="You are a read-only scout.",
            allowed_tools=("Bash",),
            permission_mode="default",
        )
    )
    registry = AgentRegistry()
    registry.add(spec)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    dispatcher = InMemoryDispatcher()
    host = SdkHost(
        event_log=InMemoryEventLog(lease_validator=dispatcher),
        content_store=InMemoryContentStore(),
        dispatcher=dispatcher,
        provider=FakeLLMProvider(
            responses=[
                LLMResponse(
                    stop_reason="end_turn",
                    content=[TextBlock(text="ok")],
                    usage=Usage(uncached=1, output=1),
                    raw={"id": "r1"},
                )
            ]
        ),
        model="stub-model",
        workspace_dir=workspace,
        registry=registry,
        permission_mode="default",
    )
    engine = host._build_engine(
        spec,
        "stub-model",
        delegation_enabled=False,
        allowed_subtask_agents=frozenset(),
        ask_user_question_enabled=False,
        policy_wrapper=None,
    )
    guard = next(
        entry.guard
        for entry in engine._hooks._guards
        if isinstance(entry.guard, PermissionGuard)
    )
    predicate = guard._policy.conditional_approval
    assert predicate is not None
    return predicate


def test_recommended_invocations_need_no_approval(tmp_path: Path) -> None:
    """Every shape the scout prompts' guidance produces runs silently."""
    needs_approval = _conditional_approval(tmp_path)
    for command in _REPRESENTATIVE_INVOCATIONS:
        assert needs_approval("Bash", {"command": command}) is False, command


def test_state_changing_and_unbounded_commands_still_need_approval(
    tmp_path: Path,
) -> None:
    """The ``git log`` widening is bounded: an execution-capable flag, a
    write-side subcommand, and the read commands D8 removed from the prompts
    all still route through HITL."""
    needs_approval = _conditional_approval(tmp_path)
    for command in _STILL_GATED:
        assert needs_approval("Bash", {"command": command}) is True, command
