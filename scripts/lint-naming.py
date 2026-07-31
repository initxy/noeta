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

# ---------------------------------------------------------------------------
# Session-as-identity ban (CONTEXT.md `Flagged ambiguities` -> "Session").
#
# The bare word "session" is fine in prose: CONTEXT.md itself says "per-session
# workspace" when it means "for the lifetime of one root-task tree". What is
# banned is naming an *identity* after it -- a `session_id`, a sessions list, a
# session-keyed cap -- because the engine knows only Tasks and the concept
# already has a name (`task_id` / `root_task_id`).
#
# So this rule fires only on COMPOUND tokens (an identifier joined by `_` or a
# camelCase hump), never on a standalone `session` / `sessions` word. The
# allow-list below is the construction-scope vocabulary CONTEXT.md promoted to
# real terms in microkernel phase 3 -- a "session pack" builds one task's tool
# set, which is a scope, not an identity.
_SESSION_ALLOWED_EXACT: frozenset[str] = frozenset(
    {
        "SessionBuildContext",
        "SessionPackEntry",
        "SessionPackFactory",
        "SessionRecorder",
        "SessionInputs",
        "build_session_inputs",
        # subprocess.Popen's own keyword -- stdlib, not ours to rename.
        "start_new_session",
    }
)
#: Any token containing this substring is construction-scope vocabulary
#: (`build_fs_session_pack`, `default_session_packs`, `session_pack_map`, ...).
_SESSION_ALLOWED_SUBSTRING = "session_pack"

_SESSION_TOKEN = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*\b")

#: Where the session-as-identity ban applies. CONTEXT.md scopes it to
#: "engine/SDK code ... below-app identifiers" — i.e. the shipped wheels and the
#: reference host. ``tests/`` is deliberately OUT: a test harness stands in for
#: a host (``tests/_sdk_session`` builds a client and drives turns), and a host
#: is exactly the layer CONTEXT.md says may own the concept.
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

# Files / directories that are *allowed* to mention the banned names
# because they catalogue the bans themselves (CONTEXT.md, ADRs, the
# Phase 0 PRD/issues, the SDD).
EXEMPT_FILE_NAMES: frozenset[str] = frozenset(
    {
        "CONTEXT.md",
        # The lint script itself catalogues the bans, as does its test.
        "lint-naming.py",
        "test_lint_naming.py",
        # Released history: an entry records the name that actually shipped at
        # that version, so renaming it would falsify the record. The rename is
        # recorded as a new breaking-change entry instead.
        "CHANGELOG.md",
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
    # Archived specs are a historical record of what was built at the time —
    # rewriting their vocabulary would falsify them (same reason as CHANGELOG).
    ("docs", "implementation-specs", "archive"),
    # An APP-LAYER fixture: a vendored mirror of the agent product's read-model
    # layer (a separate repo). CONTEXT.md puts "session" squarely inside a
    # host's own vocabulary — "a host that groups turns into a user-visible
    # session owns that concept itself" — so these names are correct where they
    # live, and renaming them would only make the mirror diverge from the code
    # it mirrors. The ban is on ENGINE/SDK identifiers, which this is not.
    ("tests", "_read_models"),
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
