"""The fs built-in's curated shell allowlist — the product's default rule table.

``noeta.runtime.shell_policy`` owns the *mechanism* (rule matching, spec
parsing, ``build_allowlist``); which commands are safe and what flag shapes
they may take is product policy, so the table ships beside the ``shell_run``
tool that enforces it. Callers hand :data:`DEFAULT_SHELL_RULES` to
``build_allowlist(..., base_rules=…)``.
"""

from __future__ import annotations

from noeta.runtime.shell_policy import SHELL_META_CHARS, AllowRule


__all__ = ["DEFAULT_SHELL_RULES"]


def _is_safe_path_arg(arg: str) -> bool:
    """A path-shaped arg has no shell metas and does not start with `-`.

    Second line of defence behind the top-level metacharacter scan.
    """
    if not arg:
        return False
    if arg.startswith("-"):
        return False
    return not any(c in SHELL_META_CHARS for c in arg)


def _git_status_validate(tail: list[str]) -> bool:
    return tail in ([], ["--short"], ["-s"], ["--porcelain"])


def _git_diff_validate(tail: list[str]) -> bool:
    allowed_flags = {"--stat", "--name-only", "--"}
    for arg in tail:
        if arg in allowed_flags:
            continue
        if arg.startswith("-"):
            return False
        if not _is_safe_path_arg(arg):
            return False
    return True


def _git_log_validate(tail: list[str]) -> bool:
    # ``git log`` is history inspection and writes no file, but with ``-p`` it
    # still honours a repo-configured ``textconv`` driver (a program named in
    # ``.git/config``); :func:`_harden_git` switches that off when the rule
    # runs. Bound the shape the way ``git diff`` is bounded: a curated flag set
    # plus path-shaped args (which also covers ``-n``'s count and ``-L``'s
    # range), rejecting every other ``-``-prefixed token, so ``--ext-diff``
    # (and any future flag) has to be added here deliberately rather than
    # arriving for free.
    allowed_flags = {
        "--oneline",
        "--stat",
        "--name-only",
        "--name-status",
        "--graph",
        "--decorate",
        "--no-merges",
        "--patch",
        "-p",
        "-n",
        "--",
    }
    for arg in tail:
        if arg in allowed_flags:
            continue
        # ``-L <start>,<end>:<file>`` (line-range trace), ``-5`` / ``-n 5``
        # (commit count), ``--max-count=5``.
        if arg.startswith("-L") or arg.startswith("--max-count="):
            continue
        if arg.startswith("-") and arg[1:].isdigit():
            continue
        if arg.startswith("-"):
            return False
        if not _is_safe_path_arg(arg):
            return False
    return True


def _pytest_validate(tail: list[str]) -> bool:
    # The shell-meta scan already blocked the dangerous tokens; what remains to
    # reject is the interactive prompt, which would hang the call.
    forbidden = {"--pdb", "--pdb-trace"}
    return all(a not in forbidden for a in tail)


def _uv_run_pytest_validate(tail: list[str]) -> bool:
    # ``tail`` starts AFTER ``["uv", "run"]``.
    if not tail or tail[0] != "pytest":
        return False
    return _pytest_validate(tail[1:])


def _trivial_validate(_: list[str]) -> bool:
    return True


def _grep_validate(_: list[str]) -> bool:
    # grep can neither execute a command nor write a file, and the top-level
    # metachar scan already blocks injection — every flag / pattern / path
    # shape is safe to search with.
    return True


def _rg_validate(tail: list[str]) -> bool:
    # ripgrep is read-only EXCEPT for the flags that shell out to an external
    # program per file; reject those so `rg` stays a pure search.
    for arg in tail:
        if arg == "--hostname-bin":
            return False
        if arg == "--pre" or arg.startswith("--pre="):
            return False
        if arg == "--pre-glob" or arg.startswith("--pre-glob="):
            return False
    return True


def _find_validate(tail: list[str]) -> bool:
    # find can run commands, delete files, or write files; reject those
    # predicates so it stays pure traversal / matching.
    forbidden = {
        "-exec",
        "-execdir",
        "-ok",
        "-okdir",
        "-delete",
        "-fprint",
        "-fprintf",
        "-fprint0",
        "-fls",
    }
    return all(a not in forbidden for a in tail)


#: Per-subcommand flags that switch off the helper programs a repository's
#: own ``.git/config`` can name: the ``core.fsmonitor`` hook (index refresh in
#: ``status`` / ``diff``), an external diff driver and ``textconv`` filters.
#: Global ``-c`` flags go before the subcommand, the rest right after it (ahead
#: of any ``--``). Residual: a ``filter.<driver>.clean`` program has no global
#: off switch, so a ``.git/config`` that names one still runs it on ``status``
#: / ``diff`` of a modified file.
_GIT_HARDENING: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "status": (("-c", "core.fsmonitor="), ()),
    "diff": (("-c", "core.fsmonitor="), ("--no-ext-diff", "--no-textconv")),
    "log": ((), ("--no-ext-diff", "--no-textconv")),
}


def _harden_git(argv: list[str]) -> list[str]:
    """``git <sub> …`` → the same command with :data:`_GIT_HARDENING` applied."""
    before, after = _GIT_HARDENING[argv[1]]
    return [argv[0], *before, argv[1], *after, *argv[2:]]


DEFAULT_SHELL_RULES: tuple[AllowRule, ...] = (
    AllowRule(
        "git", "status", _git_status_validate, "git_status", harden=_harden_git
    ),
    AllowRule("git", "diff", _git_diff_validate, "git_diff", harden=_harden_git),
    AllowRule("git", "log", _git_log_validate, "git_log", harden=_harden_git),
    # Test runners execute repository code by design (``conftest.py``,
    # ``package.json`` scripts), so the host honours them only in a trusted
    # workspace — the same trust that gates ``.noeta/shell-allowlist.json``.
    AllowRule(
        "pytest", None, _pytest_validate, "pytest", runs_workspace_code=True
    ),
    AllowRule(
        "uv", "run", _uv_run_pytest_validate, "uv_run_pytest",
        runs_workspace_code=True,
    ),
    AllowRule(
        "npm", "test", _trivial_validate, "npm_test", runs_workspace_code=True
    ),
    AllowRule(
        "pnpm", "test", _trivial_validate, "pnpm_test", runs_workspace_code=True
    ),
    # Read-only search / listing, so an ALLOWLIST-mode agent with no grep/glob
    # tool of its own can still search the workspace through the shell.
    AllowRule("grep", None, _grep_validate, "grep"),
    AllowRule("rg", None, _rg_validate, "rg"),
    AllowRule("find", None, _find_validate, "find"),
    AllowRule("ls", None, _trivial_validate, "ls"),
)
