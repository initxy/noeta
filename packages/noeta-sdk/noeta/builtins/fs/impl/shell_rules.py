"""The fs built-in's curated shell allowlist — the product's default rule table.

Phase 2c: the kernel's ``noeta.runtime.shell_policy`` keeps the *mechanism*
(:class:`~noeta.runtime.shell_policy.AllowRule` matching, spec parsing,
``build_allowlist``); the *curated* table of safe commands and their
flag-shape validators is product policy, so it ships here — beside the
``shell_run`` tool that enforces it. Callers hand
:data:`DEFAULT_SHELL_RULES` to ``build_allowlist(..., base_rules=…)`` (the
fs pack itself and the SDK host's approval predicate both do).
"""

from __future__ import annotations

from noeta.runtime.shell_policy import SHELL_META_CHARS, AllowRule


__all__ = ["DEFAULT_SHELL_RULES"]


def _is_safe_path_arg(arg: str) -> bool:
    """A path-shaped arg has no shell metas and does not start with `-`.

    (Top-level metachar scan already caught most cases; this is a second
    line of defense for paths that might contain spaces or quotes.)
    """
    if not arg:
        return False
    if arg.startswith("-"):
        return False
    return not any(c in SHELL_META_CHARS for c in arg)


def _git_status_validate(tail: list[str]) -> bool:
    return tail in ([], ["--short"], ["-s"], ["--porcelain"])


def _git_diff_validate(tail: list[str]) -> bool:
    # Allowed shapes: `git diff`, `git diff <path>`, `git diff -- <path>`,
    # `git diff --stat`, `git diff <path1> <path2>` (still all paths /
    # the path separator).
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
    # pytest takes arbitrary args; the shell-meta scan already
    # disallowed the dangerous tokens. Reject only obvious red flags
    # (``--pdb`` lands you in an interactive prompt, which would hang).
    forbidden = {"--pdb", "--pdb-trace"}
    return all(a not in forbidden for a in tail)


def _uv_run_pytest_validate(tail: list[str]) -> bool:
    # tail starts AFTER ["uv", "run"]. First element must be `pytest`,
    # rest is pytest-tail-shaped.
    if not tail or tail[0] != "pytest":
        return False
    return _pytest_validate(tail[1:])


def _trivial_validate(_: list[str]) -> bool:
    return True


def _grep_validate(_: list[str]) -> bool:
    # grep cannot execute a command or write a file; the top-level
    # metachar scan already blocks `; & | < > $` injection. Any flag /
    # pattern / path shape is safe to search with.
    return True


def _rg_validate(tail: list[str]) -> bool:
    # ripgrep is read-only EXCEPT a few flags that shell out to an
    # external program per file. Reject those so `rg` stays a pure search.
    for arg in tail:
        if arg == "--hostname-bin":
            return False
        if arg == "--pre" or arg.startswith("--pre="):
            return False
        if arg == "--pre-glob" or arg.startswith("--pre-glob="):
            return False
    return True


def _find_validate(tail: list[str]) -> bool:
    # find can run commands (-exec/-execdir/-ok/-okdir), delete files
    # (-delete), or write files (-fprint*/-fls). Reject all of those so
    # find stays pure traversal/matching.
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
    # read-only search / listing so an ALLOWLIST-mode agent —
    # notably general-purpose, which has no grep/glob tool of its own —
    # can still search the workspace via shell. All four are read-only;
    # the validators reject the handful of flags that shell out to an
    # external program or mutate the filesystem.
    AllowRule("grep", None, _grep_validate, "grep"),
    AllowRule("rg", None, _rg_validate, "rg"),
    AllowRule("find", None, _find_validate, "find"),
    AllowRule("ls", None, _trivial_validate, "ls"),
)
