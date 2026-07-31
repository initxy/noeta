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


DEFAULT_SHELL_RULES: tuple[AllowRule, ...] = (
    AllowRule("git", "status", _git_status_validate, "git_status"),
    AllowRule("git", "diff", _git_diff_validate, "git_diff"),
    AllowRule("pytest", None, _pytest_validate, "pytest"),
    AllowRule("uv", "run", _uv_run_pytest_validate, "uv_run_pytest"),
    AllowRule("npm", "test", _trivial_validate, "npm_test"),
    AllowRule("pnpm", "test", _trivial_validate, "pnpm_test"),
    # Read-only search / listing, so an ALLOWLIST-mode agent with no grep/glob
    # tool of its own can still search the workspace through the shell.
    AllowRule("grep", None, _grep_validate, "grep"),
    AllowRule("rg", None, _rg_validate, "rg"),
    AllowRule("find", None, _find_validate, "find"),
    AllowRule("ls", None, _trivial_validate, "ls"),
)
