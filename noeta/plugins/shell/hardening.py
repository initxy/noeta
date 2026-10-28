"""Which commands ``Bash`` runs hardened: a command matching one of the
rules (structurally, on its parsed argv) runs as that rule's rewritten argv
(the ``git`` flags that switch off a repository's own helper programs)."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from noeta.toolkit.shell import (
    SHELL_META_CHARS,
    harden_git,
    has_shell_meta,
    has_unquoted_expansion,
    parse_argv,
)

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


def hardened_argv(command: str, rules: Sequence[AllowRule]) -> list[str] | None:
    """``command``'s argv as the first matching rule rewrites it, or
    ``None`` when no rule with a rewrite matches (it runs as written)."""
    rule = matching_rule(command, rules)
    argv = parse_argv(command) if rule is not None and rule.harden is not None else None
    if rule is None or rule.harden is None or not argv:
        return None
    return rule.harden(argv)


# ---------------------------------------------------------------------------
# The curated default rules
# ---------------------------------------------------------------------------


def _trivial_validate(_: list[str]) -> bool:
    return True


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


DEFAULT_SHELL_RULES: tuple[AllowRule, ...] = (
    AllowRule("git", "status", _git_status_validate, "git_status", harden=harden_git),
    AllowRule("git", "diff", _git_diff_validate, "git_diff", harden=harden_git),
    AllowRule("git", "log", _git_log_validate, "git_log", harden=harden_git),
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
