#!/usr/bin/env python3
"""Grep lint: forbidden Noeta naming.

CONTEXT.md `Flagged ambiguities` locks the project's core
vocabulary on **Task** and ban a small set of synonyms that the pre-Phase-0
design draft introduced (``Run`` / ``Workflow`` / ``Session`` /
``Mutator`` / ``Pattern``) plus several compound names (``WorkflowRunner`` /
``WorkflowPolicy`` / ``WorkflowSpec`` / ``SessionStore`` /
``ConversationManager``).

This script walks a root directory and reports every project source/doc
file that mentions any banned name. Spec files that explicitly list the
bans as bans themselves (``CONTEXT.md`` / ``docs/adr/`` / ``docs/design/`` /
``.scratch/``) are exempted to keep the negative examples alive.

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

# Each ban is a ``(display, regex)`` pair. ``display`` is the human-
# readable forbidden string used in error output. ``regex`` matches the
# violation but with word boundaries so legitimate identifiers that
# merely share a prefix (``class RuntimeState`` / ``class PatternMatcher``)
# do not trip the lint.
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

# Files / directories that are *allowed* to mention the banned names
# because they catalogue the bans themselves (CONTEXT.md, ADRs, the
# Phase 0 PRD/issues, the SDD).
EXEMPT_FILE_NAMES: frozenset[str] = frozenset(
    {
        "CONTEXT.md",
        # The lint script itself catalogues the bans, as does its test.
        "lint-naming.py",
        "test_lint_naming.py",
    }
)
EXEMPT_DIR_PARTS: frozenset[str] = frozenset(
    {
        ".scratch",
        # Tooling state, not source: agent worktrees / snapshots of other
        # branches live here and may legitimately quote banned names in
        # their own ADRs/docs. Never part of the checked-out source tree.
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
# Spec / design docs under docs/ that legitimately reference the bans.
# ``docs/adr/`` holds the topic decisions and catalogues the bans
# (e.g. task-as-only-primitive names the rejected ``WorkflowRunner`` to keep it
# rejected), so it gets an exemption.
EXEMPT_REL_DIRS: tuple[tuple[str, ...], ...] = (
    ("docs", "adr"),
    ("docs", "design"),
    # The server-platform application deliberately models an app-layer
    # "session" entity (the unit of conversation the UI lists and resumes,
    # grouping one or more engine tasks). The Task-vocabulary ban protects
    # the engine libraries, where Task stays the only primitive; it does not
    # extend to the product app's own domain model.
    ("apps", "noeta-agent"),
)

# Suffixes worth scanning. We deliberately stay narrow so binary
# artefacts and lock files never trip the grep.
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
        for lineno, line in enumerate(text.splitlines(), start=1):
            for display, pattern in BANNED:
                if pattern.search(line):
                    violations.append((path, lineno, display, line.rstrip()))
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
