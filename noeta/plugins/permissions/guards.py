"""Guards: lifetime budget caps, the repetition (loop) guard, and the
host's declarative PreToolUse rules. Each check is a pure function of its
configuration, the call, and numbers folded from the task's log."""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, replace
from fnmatch import fnmatchcase
from typing import Any, Literal

from noeta.core.types import Message, ToolUse

# ---------------------------------------------------------------- budget


@dataclass(frozen=True)
class Budget:
    """Caps on a task's whole life (one task is a whole conversation, so
    size them for the longest conversation you will fund). ``None`` = no
    cap. ``max_subtask_depth``: root depth is 0, so ``1`` lets the root
    spawn a child but denies that child a grandchild."""

    max_iterations: int | None = None
    max_tool_calls: int | None = None
    max_cost_usd: float | None = None
    max_spawned_subtasks: int | None = None
    max_subtask_depth: int | None = 3


@dataclass(frozen=True)
class Counters:
    """What the budget is checked against. ``iterations`` counts the model
    call that produced the call being checked."""

    iterations: int = 0
    tool_calls: int = 0
    cost_usd: float = 0.0
    spawned: int = 0
    depth: int = 0


def budget_verdict(b: Budget, c: Counters, *, spawns: bool) -> str | None:
    """The deny reason, or ``None``. Iterations compare with ``>`` (the
    in-flight model call is already counted); the rest with ``>=``."""
    if b.max_iterations is not None and c.iterations > b.max_iterations:
        return f"max_iterations={b.max_iterations} exceeded"
    if b.max_cost_usd is not None and c.cost_usd >= b.max_cost_usd:
        return f"max_cost_usd={b.max_cost_usd} reached"
    if b.max_tool_calls is not None and c.tool_calls >= b.max_tool_calls:
        return f"max_tool_calls={b.max_tool_calls} reached"
    if spawns:
        if b.max_spawned_subtasks is not None and c.spawned >= b.max_spawned_subtasks:
            return f"max_spawned_subtasks={b.max_spawned_subtasks} reached"
        if b.max_subtask_depth is not None and c.depth >= b.max_subtask_depth:
            return f"max_subtask_depth={b.max_subtask_depth} reached"
    return None


# ---------------------------------------------------------------- repetition

RepetitionAction = Literal["require_approval", "deny"]


@dataclass(frozen=True)
class RepetitionPolicy:
    """``threshold`` consecutive identical calls (counting the proposed one)
    trip ``action``; ``threshold <= 0`` is off. ``window`` bounds how many
    recent calls are looked at."""

    threshold: int = 3
    action: RepetitionAction = "require_approval"
    window: int = 8


def _key(use: ToolUse) -> tuple[str, str]:
    return use.name, json.dumps(use.input, sort_keys=True, separators=(",", ":"), default=str)


def recent_calls(messages: Sequence[Message], before: str, window: int) -> list[ToolUse]:
    """The last ``window`` tool calls of the conversation before call
    ``before`` (calls of the same reply that precede it count)."""
    out: list[ToolUse] = []
    found = False
    for m in reversed(messages):
        if m.role != "assistant":
            continue
        for use in reversed(m.tool_uses):
            if not found:
                if use.id == before:
                    found = True
                continue
            out.append(use)
            if len(out) >= window:
                return list(reversed(out))
        found = True  # an earlier reply: everything in it precedes the call
    return list(reversed(out))


def repetition_verdict(p: RepetitionPolicy, call: ToolUse,
                       history: Sequence[ToolUse]) -> tuple[RepetitionAction, str] | None:
    if p.threshold <= 0:
        return None
    key = _key(call)
    run = 0
    for prior in reversed(history):
        if _key(prior) != key:
            break
        run += 1
    consecutive = run + 1
    if consecutive < p.threshold:
        return None
    reason = (
        f"tool {call.name!r} called with identical arguments "
        f"{consecutive} times in a row (repetition threshold "
        f"{p.threshold}); suspected loop"
    )
    return p.action, reason


# ---------------------------------------------------------------- PreToolUse

#: Strings are cut to this before ``contains`` / ``regex`` run.
MATCH_STRING_CAP = 64 * 1024

HookAction = Literal["allow", "deny", "require_approval"]
_HOOK_ACTIONS = ("allow", "deny", "require_approval")
_MATCH_OPS = ("equals", "contains", "regex")

#: Tools that run a shell ``command`` like Bash: a rule on Bash matches them.
RUNS_BASH_COMMAND = frozenset({"Monitor"})


@dataclass(frozen=True)
class MatchArg:
    """A predicate on one argument. ``path`` is a tuple of object keys (no
    indexing). ``equals`` compares JSON values; ``contains`` / ``regex``
    apply to strings only. A regex given as ``value`` is compiled when the
    rule is built."""

    path: tuple[str, ...]
    op: Literal["equals", "contains", "regex"]
    value: Any = None
    pattern: re.Pattern[str] | None = None


@dataclass(frozen=True)
class PreToolUseRule:
    """``match_tool`` is an fnmatch pattern over the tool name (a rule on
    ``Bash`` also matches ``Monitor``); ``match_arg`` narrows it. The first
    matching rule decides: ``allow`` stops the scan, ``deny`` refuses,
    ``require_approval`` asks. Rules only tighten: ``allow`` never loosens
    another gate."""

    match_tool: str
    action: HookAction
    match_arg: MatchArg | None = None
    reason: str | None = None


def check_pre_rule(where: str, rule: PreToolUseRule) -> PreToolUseRule:
    """Validate one rule (raising ``ValueError``); compile a string regex."""
    if not isinstance(rule, PreToolUseRule):
        raise ValueError(f"{where} must be a PreToolUseRule; got {rule!r}")
    if not isinstance(rule.match_tool, str) or not rule.match_tool:
        raise ValueError(
            f"{where}.match_tool must be a non-empty tool-name pattern; got {rule.match_tool!r}")
    if rule.action not in _HOOK_ACTIONS:
        legal = ", ".join(sorted(_HOOK_ACTIONS))
        raise ValueError(f"{where}.action must be one of {{{legal}}}; got {rule.action!r}")
    ma = rule.match_arg
    if ma is None:
        return rule
    if not isinstance(ma, MatchArg):
        raise ValueError(f"{where}.match_arg must be a MatchArg; got {ma!r}")
    if (not isinstance(ma.path, tuple) or not ma.path
            or not all(isinstance(k, str) and k for k in ma.path)):
        raise ValueError(
            f"{where}.match_arg.path must be a non-empty tuple of argument keys (str); "
            f"got {ma.path!r}")
    if ma.op not in _MATCH_OPS:
        legal = ", ".join(sorted(_MATCH_OPS))
        raise ValueError(f"{where}.match_arg.op must be one of {{{legal}}}; got {ma.op!r}")
    if ma.op == "contains" and not isinstance(ma.value, str):
        raise ValueError(f"{where}.match_arg: op 'contains' needs a str value; got {ma.value!r}")
    if ma.op == "regex" and ma.pattern is None:
        if not isinstance(ma.value, str):
            raise ValueError(
                f"{where}.match_arg: op 'regex' needs a pattern (re.Pattern) or a str value; "
                f"got {ma.value!r}")
        try:
            pattern = re.compile(ma.value)
        except re.error as exc:
            raise ValueError(f"{where}.match_arg: invalid regex {ma.value!r}: {exc}") from exc
        return replace(rule, match_arg=replace(ma, pattern=pattern))
    return rule


def _resolve(args: Any, path: tuple[str, ...]) -> tuple[bool, Any]:
    cur = args
    for key in path:
        if not isinstance(cur, dict) or key not in cur:
            return False, None
        cur = cur[key]
    return True, cur


def _arg_matches(ma: MatchArg, args: dict[str, Any]) -> bool:
    found, value = _resolve(args, ma.path)
    if not found:
        return False
    if ma.op == "equals":
        return bool(value == ma.value)
    if not isinstance(value, str):
        return False
    capped = value[:MATCH_STRING_CAP]
    if ma.op == "contains":
        return isinstance(ma.value, str) and ma.value in capped
    return ma.pattern is not None and ma.pattern.search(capped) is not None


def pre_tool_use_verdict(rules: Sequence[PreToolUseRule],
                         call: ToolUse) -> tuple[HookAction, str] | None:
    """``(action, reason)`` of the first matching rule, or ``None``."""
    names: tuple[str, ...] = (call.name,)
    if call.name in RUNS_BASH_COMMAND:
        names = (call.name, "Bash")
    for rule in rules:
        if not any(fnmatchcase(n, rule.match_tool) for n in names):
            continue
        if rule.match_arg is not None and not _arg_matches(rule.match_arg, dict(call.input)):
            continue
        return rule.action, rule.reason or f"hook rule on {rule.match_tool!r}"
    return None
