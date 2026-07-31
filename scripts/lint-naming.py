#!/usr/bin/env python3
"""Grep lint: names that collide with the project's core vocabulary.

Task is the only entity type the engine has, so ``Run`` / ``Workflow`` /
``Session`` / ``Mutator`` / ``Pattern`` and compounds like ``WorkflowRunner``
or ``SessionStore`` each duplicate something the domain already names, and
admitting one forks the vocabulary between the code and the prose about it.
CONTEXT.md (`Flagged ambiguities`) pins that vocabulary; this script enforces
it, down to the narrower session-as-identity rule below. Files that catalogue
the bans as bans are exempt, so the negative examples survive their own rule.

Exit code:
    0 — clean
    1 — at least one violation
    2 — invocation error

Usage::

    python scripts/lint-naming.py [ROOT]
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Iterable

# Word boundaries keep identifiers that merely share a prefix with a banned
# name — ``class RuntimeState``, ``class PatternMatcher`` — out of the results.
_BANNED_CLASSES = (
    "Run",
    "Workflow",
    "Session",
    "Mutator",
    "Pattern",
)
_BANNED_IDENTIFIERS = (
    "WorkflowRunner",
    "WorkflowPolicy",
    "WorkflowSpec",
    "SessionStore",
    "ConversationManager",
)

BANNED: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (f"class {name}", re.compile(rf"\bclass\s+{re.escape(name)}\b"))
    for name in _BANNED_CLASSES
) + tuple(
    (name, re.compile(rf"\b{re.escape(name)}\b")) for name in _BANNED_IDENTIFIERS
)

# ---------------------------------------------------------------------------
# Session as identity (CONTEXT.md `Flagged ambiguities` -> "Session").
#
# The bare word is fine in prose: "per-session workspace" reads well and means
# "for the lifetime of one root-task tree". Naming an *identity* after it is
# not -- a `session_id`, a sessions list, a session-keyed cap -- because the
# engine knows only Tasks and the thing being named already has a name
# (`task_id` / `root_task_id`).
#
# Hence the rule fires only on COMPOUND tokens (an identifier joined by `_` or
# a camelCase hump), never on a standalone `session` / `sessions` word, and the
# allow-list below carves out construction-scope vocabulary: a session pack
# builds one task's tool set, which is a scope, not an identity.
_SESSION_ALLOWED_EXACT: frozenset[str] = frozenset(
    {
        "SessionBuildContext",
        "SessionPackEntry",
        "SessionPackFactory",
        "SessionRecorder",
        "SessionInputs",
        "build_session_inputs",
        # subprocess.Popen's own keyword -- stdlib spelling, not ours to pick.
        "start_new_session",
    }
)
#: Any token containing this substring is construction-scope vocabulary
#: (`build_fs_session_pack`, `default_session_packs`, `session_pack_map`, ...).
_SESSION_ALLOWED_SUBSTRING = "session_pack"

_SESSION_TOKEN = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*\b")

#: Where the ban applies: the shipped wheels and the reference host. ``tests/``
#: is deliberately OUT — a test harness stands in for a host (``tests/_sdk_session``
#: builds a client and drives turns), and a host is exactly the layer CONTEXT.md
#: lets own the concept.
SESSION_RULE_REL_DIRS: tuple[tuple[str, ...], ...] = (
    ("packages",),
    ("examples",),
)


def _is_compound(token: str) -> bool:
    """A token that names something, as opposed to the prose word "session"."""
    if "_" in token:
        return True
    # camelCase / PascalCase hump around the word, e.g. ``SessionStore``.
    return token not in {"session", "Session", "sessions", "Sessions"}


def session_identity_violations(line: str) -> list[str]:
    """Return every session-as-identity token on ``line``."""
    found: list[str] = []
    for token in _SESSION_TOKEN.findall(line):
        if "session" not in token.lower():
            continue
        if not _is_compound(token):
            continue
        if token in _SESSION_ALLOWED_EXACT:
            continue
        if _SESSION_ALLOWED_SUBSTRING in token.lower():
            continue
        found.append(token)
    return found

# Files allowed to mention the banned names because stating a ban requires
# spelling it out.
EXEMPT_FILE_NAMES: frozenset[str] = frozenset(
    {
        "CONTEXT.md",
        # The working agreement states the ban and the glossary mirrors
        # CONTEXT.md's flagged-ambiguities list; both must spell the names out.
        "AGENTS.md",
        "glossary.md",
        "lint-naming.py",
        "test_lint_naming.py",
        # A changelog entry says what a given version shipped under; editing it
        # to satisfy the current vocabulary would falsify that record. A name
        # change is a new breaking-change entry instead.
        "CHANGELOG.md",
    }
)
EXEMPT_DIR_PARTS: frozenset[str] = frozenset(
    {
        ".scratch",
        # Tooling state rather than source: agent worktrees and branch
        # snapshots land here and may quote banned names in their own docs.
        ".claude",
        ".git",
        ".venv",
        "venv",
        ".mypy_cache",
        ".pytest_cache",
        "__pycache__",
        ".coverage",
        "node_modules",
        "dist",
        "build",
        ".ruff_cache",
    }
)
EXEMPT_REL_DIRS: tuple[tuple[str, ...], ...] = (
    # An ADR records what was rejected as well as what was chosen, and
    # task-as-only-primitive keeps ``WorkflowRunner`` rejected by naming it.
    ("docs", "adr"),
    # An app-layer fixture: a mirror of a host product's read-model layer, which
    # lives in its own repo. A host may own the session concept, so these names
    # are correct where they live and editing them would only make the mirror
    # diverge from what it mirrors.
    ("tests", "_read_models"),
)

# Deliberately narrow, so binary artefacts and lock files never trip the grep.
SCAN_SUFFIXES: frozenset[str] = frozenset(
    {".py", ".md", ".toml", ".yaml", ".yml", ".cfg", ".ini"}
)


def _is_under(rel_parts: tuple[str, ...], prefix: tuple[str, ...]) -> bool:
    return len(rel_parts) >= len(prefix) and rel_parts[: len(prefix)] == prefix


def _exempt(path: Path, root: Path) -> bool:
    rel = path.relative_to(root)
    parts = rel.parts
    if path.name in EXEMPT_FILE_NAMES:
        return True
    if any(part in EXEMPT_DIR_PARTS for part in parts):
        return True
    return any(_is_under(parts, prefix) for prefix in EXEMPT_REL_DIRS)


def _candidate_files(root: Path) -> Iterable[Path]:
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix not in SCAN_SUFFIXES:
            continue
        if _exempt(path, root):
            continue
        yield path


def scan(root: Path) -> list[tuple[Path, int, str, str]]:
    """Return ``(path, lineno, banned_name, line)`` for every violation."""
    violations: list[tuple[Path, int, str, str]] = []
    for path in _candidate_files(root):
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        rel_parts = path.relative_to(root).parts
        session_rule_applies = any(
            _is_under(rel_parts, prefix) for prefix in SESSION_RULE_REL_DIRS
        )
        for lineno, line in enumerate(text.splitlines(), start=1):
            for display, pattern in BANNED:
                if pattern.search(line):
                    violations.append((path, lineno, display, line.rstrip()))
            if session_rule_applies:
                for token in session_identity_violations(line):
                    violations.append((path, lineno, token, line.rstrip()))
    return violations


def main(argv: list[str]) -> int:
    if len(argv) > 2:
        print("usage: lint-naming.py [ROOT]", file=sys.stderr)
        return 2
    root = Path(argv[1]).resolve() if len(argv) == 2 else Path.cwd().resolve()
    if not root.is_dir():
        print(f"not a directory: {root}", file=sys.stderr)
        return 2
    violations = scan(root)
    if not violations:
        return 0
    for path, lineno, needle, line in violations:
        rel = path.relative_to(root)
        print(f"{rel}:{lineno}: forbidden name '{needle}' — {line}")
    print(f"\n{len(violations)} violation(s) of forbidden names.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
