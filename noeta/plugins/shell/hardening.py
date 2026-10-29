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
class HardeningRule:
    """One rule of which commands run hardened. ``program`` matches ``argv[0]``
    exactly, ``subcommand`` (if set) ``argv[1]``; ``validate`` then inspects
    the remaining args. Matching is on the parsed argv only, never the raw
    string. A matched argv runs as ``harden`` rewrites it."""

    program: str
    subcommand: str | None
    validate: _ArgValidator
    harden: Callable[[list[str]], list[str]]

    def matches(self, argv: list[str]) -> bool:
        if not argv or argv[0] != self.program:
            return False
        tail = argv[1:]
        if self.subcommand is not None:
            if not tail or tail[0] != self.subcommand:
                return False
            tail = tail[1:]
        return self.validate(tail)


def matching_rule(command: str, rules: Sequence[HardeningRule]) -> HardeningRule | None:
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


def hardened_argv(command: str, rules: Sequence[HardeningRule]) -> list[str] | None:
    """``command``'s argv as the first matching rule rewrites it, or
    ``None`` when no rule matches (it runs as written)."""
    rule = matching_rule(command, rules)
    argv = parse_argv(command) if rule is not None else None
    if rule is None or not argv:
        return None
    return rule.harden(argv)


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


DEFAULT_SHELL_RULES: tuple[HardeningRule, ...] = (
    HardeningRule("git", "status", _git_status_validate, harden_git),
    HardeningRule("git", "diff", _git_diff_validate, harden_git),
    HardeningRule("git", "log", _git_log_validate, harden_git),
)
