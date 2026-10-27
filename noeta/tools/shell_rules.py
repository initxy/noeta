"""Pure shell-command classification: the structural allowlist engine, the
curated default rules, and Claude Code's long-leading-``sleep`` check.

Nothing here runs anything or reads a file. ``Bash`` uses the default rules'
hardening rewrites (``git`` flags that switch off repo-configured helper
programs) and :func:`long_leading_sleep`; deciding which commands may run
without approval is the permissions extension's job, built on
:func:`matching_rule` / :func:`command_in_allowlist`.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

__all__ = [
    "DEFAULT_SHELL_RULES",
    "LONG_SLEEP_S",
    "SHELL_EXPANSION_CHARS",
    "SHELL_META_CHARS",
    "AllowRule",
    "ShellMode",
    "build_allowlist",
    "command_in_allowlist",
    "has_shell_meta",
    "has_unquoted_expansion",
    "long_leading_sleep",
    "matching_rule",
    "parse_argv",
    "rule_from_spec",
    "rule_spec_from_command",
    "subcommands",
]


SHELL_META_CHARS = frozenset(";&|<>`$()\n\r")

#: Characters bash expands in an UNQUOTED word — brace expansion, globbing,
#: tilde expansion — that ``shlex`` leaves literal. A command carrying one
#: unquoted would be judged on an argv bash never runs
#: (``find . {-exec,touch} x {} +`` hides ``-exec`` from the validator).
SHELL_EXPANSION_CHARS = frozenset("{*?[~")


class ShellMode(StrEnum):
    """How much of the shell a host allows.

    * ``OFF`` — no ``Bash`` at all.
    * ``ALLOWLIST`` — only metachar-free commands matching the allowlist.
    * ``ARBITRARY`` — anything ``bash -c`` runs (pipes, redirects, ...).
    """

    OFF = "off"
    ALLOWLIST = "allowlist"
    ARBITRARY = "arbitrary"


_ArgValidator = Callable[[list[str]], bool]


@dataclass(frozen=True)
class AllowRule:
    """One structural allowlist entry. ``program`` matches ``argv[0]``
    exactly, ``subcommand`` (if set) ``argv[1]``; ``validate`` then inspects
    the remaining args. Matching is on the parsed argv only, never the raw
    string."""

    program: str
    subcommand: str | None
    validate: _ArgValidator
    label: str
    #: The command runs code or config the WORKSPACE controls (a test runner
    #: loading ``conftest.py``), so it should be honoured only in a trusted
    #: workspace.
    runs_workspace_code: bool = False
    #: Rewrites a matched argv before it runs; ``None`` = run as parsed.
    harden: Callable[[list[str]], list[str]] | None = None

    def matches(self, argv: list[str]) -> bool:
        if not argv or argv[0] != self.program:
            return False
        tail = argv[1:]
        if self.subcommand is not None:
            if not tail or tail[0] != self.subcommand:
                return False
            tail = tail[1:]
        return self.validate(tail)


# ---------------------------------------------------------------------------
# Parsing and classification
# ---------------------------------------------------------------------------


def parse_argv(command: str) -> list[str] | None:
    """POSIX ``shlex`` split, or ``None`` for unbalanced quotes."""
    try:
        return shlex.split(command, posix=True)
    except ValueError:
        return None


def has_shell_meta(command: str) -> bool:
    return any(c in SHELL_META_CHARS for c in command)


def has_unquoted_expansion(command: str) -> bool:
    """True iff a :data:`SHELL_EXPANSION_CHARS` character sits outside quotes.

    Mirrors bash's quoting: a backslash escapes the next character, nothing
    inside single quotes is special, and inside double quotes brace, glob and
    tilde expansion do not happen. ``~`` expands only as a tilde prefix — at
    the start of a word or after ``=`` / ``:`` — so ``HEAD~1`` stays allowed.
    Unbalanced quotes are left to ``shlex``, which rejects them.
    """
    quote: str | None = None
    escaped = False
    prev = " "
    for ch in command:
        if escaped:
            escaped = False
        elif quote == "'":
            if ch == "'":
                quote = None
        elif ch == "\\":
            escaped = True
        elif quote == '"':
            if ch == '"':
                quote = None
        elif ch in ("'", '"'):
            quote = ch
        elif ch == "~":
            if prev.isspace() or prev in "=:":
                return True
        elif ch in SHELL_EXPANSION_CHARS:
            return True
        prev = ch
    return False


def matching_rule(command: str, rules: Sequence[AllowRule]) -> AllowRule | None:
    """The first rule ``command`` matches, or ``None``. A command with shell
    metacharacters, an unquoted expansion character or unbalanced quotes never
    matches: past those checks the ``shlex`` argv is exactly what ``bash -c``
    would run."""
    if has_shell_meta(command) or has_unquoted_expansion(command):
        return None
    argv = parse_argv(command)
    if not argv:
        return None
    for rule in rules:
        if rule.matches(argv):
            return rule
    return None


def command_in_allowlist(command: str, rules: Sequence[AllowRule]) -> bool:
    return matching_rule(command, rules) is not None


def _trivial_validate(_: list[str]) -> bool:
    return True


def rule_from_spec(spec: Mapping[str, Any]) -> AllowRule:
    """An operator rule (``{"program": "npm", "subcommand": "start"}``,
    subcommand optional) accepting any tail args — looser than the curated
    validators on purpose: it means "this program may run"."""
    program = spec.get("program")
    if not isinstance(program, str) or not program:
        raise ValueError(f"shell allowlist rule needs a non-empty string 'program': {spec!r}")
    subcommand = spec.get("subcommand")
    if subcommand is not None and not isinstance(subcommand, str):
        raise ValueError(
            f"shell allowlist rule 'subcommand' must be a string or absent: {spec!r}"
        )
    label = spec.get("label")
    if not isinstance(label, str) or not label:
        label = program if subcommand is None else f"{program}_{subcommand}"
    return AllowRule(program, subcommand, _trivial_validate, label)


def build_allowlist(
    extra_specs: Sequence[Mapping[str, Any]] = (),
    *,
    base_rules: Sequence[AllowRule] = (),
) -> tuple[AllowRule, ...]:
    """``base_rules`` kept verbatim, plus operator ``extra_specs``."""
    return tuple(base_rules) + tuple(rule_from_spec(s) for s in extra_specs)


def rule_spec_from_command(command: str) -> dict[str, str] | None:
    """A rememberable spec for an approved command: program + first arg
    (``npm start`` -> ``{"program": "npm", "subcommand": "start"}``), or
    ``None`` for a command with metacharacters or unbalanced quotes."""
    if has_shell_meta(command):
        return None
    argv = parse_argv(command)
    if not argv:
        return None
    spec: dict[str, str] = {"program": argv[0]}
    if len(argv) >= 2:
        spec["subcommand"] = argv[1]
    return spec


# ---------------------------------------------------------------------------
# Long leading sleep (Claude Code 2.1.300)
# ---------------------------------------------------------------------------

#: Claude Code's threshold for a leading ``sleep`` it refuses, in seconds.
LONG_SLEEP_S = 25.0

#: The shell's control operators — where a command splits into subcommands.
_CONTROL_OPERATORS = ("&&", "||", ";", "|", "&", "\n")


def subcommands(command: str) -> list[str]:
    """``command`` split at its unquoted control operators (empty parts
    dropped). Quoting is honoured just far enough that an operator inside
    quotes does not split."""
    parts: list[str] = []
    current: list[str] = []
    quote = ""
    i = 0
    while i < len(command):
        ch = command[i]
        if quote:
            current.append(ch)
            if ch == "\\" and quote == '"' and i + 1 < len(command):
                current.append(command[i + 1])
                i += 1
            elif ch == quote:
                quote = ""
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            current.append(ch)
            i += 1
            continue
        operator = next((op for op in _CONTROL_OPERATORS if command.startswith(op, i)), None)
        if operator is not None:
            parts.append("".join(current))
            current = []
            i += len(operator)
            continue
        current.append(ch)
        i += 1
    parts.append("".join(current))
    return [part.strip() for part in parts if part.strip()]


def _js_number(value: float) -> str:
    """How JavaScript prints a parsed float: ``110``, never ``110.0``."""
    return str(int(value)) if value.is_integer() else repr(value)


def long_leading_sleep(command: str) -> str | None:
    """Claude Code's description of a command whose first subcommand is
    ``sleep N`` with ``N >= LONG_SLEEP_S`` — ``standalone sleep N`` or
    ``sleep N followed by: <the rest>`` — else ``None``."""
    parts = subcommands(command)
    if not parts:
        return None
    match = re.match(r"^sleep\s+(\d+(?:\.\d*)?)\s*$", parts[0])
    if match is None:
        return None
    seconds = float(match.group(1))
    if seconds < LONG_SLEEP_S:
        return None
    rest = " ".join(parts[1:]).strip()
    shown = _js_number(seconds)
    return f"sleep {shown} followed by: {rest}" if rest else f"standalone sleep {shown}"


# ---------------------------------------------------------------------------
# The curated default rules
# ---------------------------------------------------------------------------


def _is_safe_path_arg(arg: str) -> bool:
    """A path-shaped arg: no shell metas, does not start with ``-``."""
    if not arg or arg.startswith("-"):
        return False
    return not any(c in SHELL_META_CHARS for c in arg)


def _git_status_validate(tail: list[str]) -> bool:
    return tail in ([], ["--short"], ["-s"], ["--porcelain"])


def _git_diff_validate(tail: list[str]) -> bool:
    allowed_flags = {"--stat", "--name-only", "--"}
    for arg in tail:
        if arg in allowed_flags:
            continue
        if arg.startswith("-") or not _is_safe_path_arg(arg):
            return False
    return True


def _git_log_validate(tail: list[str]) -> bool:
    # A curated flag set plus path-shaped args (which also covers ``-n``'s
    # count and ``-L``'s range); every other ``-`` token is rejected, so
    # ``--ext-diff`` (and any future flag) must be added deliberately.
    allowed_flags = {
        "--oneline", "--stat", "--name-only", "--name-status", "--graph",
        "--decorate", "--no-merges", "--patch", "-p", "-n", "--",
    }
    for arg in tail:
        if arg in allowed_flags:
            continue
        if arg.startswith(("-L", "--max-count=")):
            continue
        if arg.startswith("-") and arg[1:].isdigit():
            continue
        if arg.startswith("-") or not _is_safe_path_arg(arg):
            return False
    return True


def _pytest_validate(tail: list[str]) -> bool:
    # The interactive debugger prompt would hang the call.
    return all(a not in {"--pdb", "--pdb-trace"} for a in tail)


def _uv_run_pytest_validate(tail: list[str]) -> bool:
    # ``tail`` starts AFTER ``["uv", "run"]``.
    if not tail or tail[0] != "pytest":
        return False
    return _pytest_validate(tail[1:])


def _rg_validate(tail: list[str]) -> bool:
    # ripgrep is read-only except for the flags that run a program per file.
    for arg in tail:
        if arg == "--hostname-bin":
            return False
        if arg == "--pre" or arg.startswith("--pre="):
            return False
        if arg == "--pre-glob" or arg.startswith("--pre-glob="):
            return False
    return True


def _find_validate(tail: list[str]) -> bool:
    # find can run commands, delete or write files; keep it pure traversal.
    forbidden = {
        "-exec", "-execdir", "-ok", "-okdir", "-delete",
        "-fprint", "-fprintf", "-fprint0", "-fls",
    }
    return all(a not in forbidden for a in tail)


#: Per-subcommand flags that switch off the helper programs a repository's
#: own ``.git/config`` can name: the ``core.fsmonitor`` hook, an external diff
#: driver and ``textconv`` filters. Global ``-c`` flags go before the
#: subcommand, the rest right after it. Residual: a ``filter.<driver>.clean``
#: program has no global off switch.
_GIT_HARDENING: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "status": (("-c", "core.fsmonitor="), ()),
    "diff": (("-c", "core.fsmonitor="), ("--no-ext-diff", "--no-textconv")),
    "log": ((), ("--no-ext-diff", "--no-textconv")),
}


def _harden_git(argv: list[str]) -> list[str]:
    """``git <sub> ...`` with :data:`_GIT_HARDENING` applied."""
    before, after = _GIT_HARDENING[argv[1]]
    return [argv[0], *before, argv[1], *after, *argv[2:]]


DEFAULT_SHELL_RULES: tuple[AllowRule, ...] = (
    AllowRule("git", "status", _git_status_validate, "git_status", harden=_harden_git),
    AllowRule("git", "diff", _git_diff_validate, "git_diff", harden=_harden_git),
    AllowRule("git", "log", _git_log_validate, "git_log", harden=_harden_git),
    # Test runners execute repository code by design.
    AllowRule("pytest", None, _pytest_validate, "pytest", runs_workspace_code=True),
    AllowRule(
        "uv", "run", _uv_run_pytest_validate, "uv_run_pytest", runs_workspace_code=True
    ),
    AllowRule("npm", "test", _trivial_validate, "npm_test", runs_workspace_code=True),
    AllowRule("pnpm", "test", _trivial_validate, "pnpm_test", runs_workspace_code=True),
    # Read-only search / listing.
    AllowRule("grep", None, _trivial_validate, "grep"),
    AllowRule("rg", None, _rg_validate, "rg"),
    AllowRule("find", None, _find_validate, "find"),
    AllowRule("ls", None, _trivial_validate, "ls"),
)
