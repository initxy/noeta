"""Docs stay executable, and stay honest about what the code ships.

Snippets rot silently unless something re-runs them, so every fenced block
preceded by ``<!-- runnable: smoke -->`` is extracted and executed — a snippet
that stopped working fails here instead of in a reader's terminal. The marker
is opt-in on purpose: an untagged block (illustrative shell, configuration,
sample output) must never run, or the suite would happily execute a
``pip install`` line out of a README.

The remaining gates hold user-facing pages to claims the code has to back: a
single canonical README, install paths that exist, and the durable
exactly-once wake.
"""

from __future__ import annotations

import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest


_REPO_ROOT = Path(__file__).resolve().parents[1]
_FENCE_RE = re.compile(
    r"<!--\s*runnable:\s*(?P<tag>\w+)\s*-->\s*\n"
    r"```(?P<lang>\w+)\s*\n"
    r"(?P<body>.*?)\n"
    r"```",
    flags=re.DOTALL,
)


def _extract_runnable_blocks(md_path: Path) -> list[tuple[str, str, str]]:
    """Return a list of ``(tag, lang, body)`` for every runnable block
    in ``md_path``."""
    text = md_path.read_text(encoding="utf-8")
    return [
        (m.group("tag"), m.group("lang"), m.group("body"))
        for m in _FENCE_RE.finditer(text)
    ]


_RUNNABLE_MD_FILES = (
    _REPO_ROOT / "README.md",
    _REPO_ROOT / "docs" / "tutorials" / "quickstart.md",
)

# Subtrees the user-doc gates skip: an ADR argues its rejected alternatives on
# purpose, and the rest are internal working notes — none of them are pages a
# user reads.
_NON_USER_DOC_SUBTREES = ("adr", "implementation-specs", "_research")


def _user_facing_doc_pages() -> list[Path]:
    """All user-facing ``docs/**/*.md`` pages, recursive, minus the
    excluded subtrees above."""
    docs_root = _REPO_ROOT / "docs"
    return [
        p
        for p in sorted(docs_root.glob("**/*.md"))
        if p.relative_to(docs_root).parts[0] not in _NON_USER_DOC_SUBTREES
    ]


def _collect_runnables() -> list[tuple[Path, str, str, str]]:
    items: list[tuple[Path, str, str, str]] = []
    for md_path in _RUNNABLE_MD_FILES:
        if not md_path.exists():
            continue
        for tag, lang, body in _extract_runnable_blocks(md_path):
            items.append((md_path, tag, lang, body))
    return items


def test_at_least_one_smoke_block_is_discoverable() -> None:
    """At least one runnable smoke block must exist — an empty extraction
    would let every other assertion here pass over nothing."""
    runnables = _collect_runnables()
    smoke_blocks = [r for r in runnables if r[1] == "smoke"]
    assert smoke_blocks, (
        "no `<!-- runnable: smoke -->` blocks found in README/quickstart"
    )


@pytest.mark.parametrize(
    "md_path,tag,lang,body",
    _collect_runnables(),
    ids=lambda x: (
        str(x.relative_to(_REPO_ROOT)) if isinstance(x, Path) else str(x)
    ),
)
def test_runnable_codeblocks_execute_successfully(
    md_path: Path, tag: str, lang: str, body: str
) -> None:
    """Execute every ``<!-- runnable: ... -->``-tagged code block.

    Each block runs in a fresh subprocess (the same interpreter/venv), so a
    snippet's top-level ``assert`` is the success signal and no snippet side
    effect (imports, monkey-patches, global state) can leak into the test
    process — which is also how a reader actually runs it."""
    assert tag == "smoke", (
        f"unknown runnable tag {tag!r} in {md_path}; only 'smoke' is supported"
    )
    assert lang == "python", (
        f"smoke blocks must be Python (got lang={lang!r}) in {md_path}"
    )
    dedented = textwrap.dedent(body)
    result = subprocess.run(
        [sys.executable, "-c", dedented],
        capture_output=True,
        text=True,
        cwd=_REPO_ROOT,
        timeout=120,
    )
    assert result.returncode == 0, (
        f"smoke block in {md_path} exited {result.returncode}:\n"
        f"{result.stdout}\n{result.stderr}"
    )


def test_repo_root_readme_is_the_canonical_entry() -> None:
    """One canonical entry point: the repo-root README. A second package-level
    copy would drift out of sync with it."""
    repo_root_readme = _REPO_ROOT / "README.md"
    package_readme = _REPO_ROOT / "packages" / "noeta" / "README.md"

    assert repo_root_readme.exists(), "repo-root README.md must exist"
    assert not package_readme.exists(), (
        "do not create packages/noeta/README.md (architect Q1 — repo-root only)"
    )


def test_docs_dont_promise_pypi_install_paths() -> None:
    """No page may send a reader to a distribution named ``noeta``.

    The published distributions are ``noeta-sdk`` and ``noeta-runtime``; an
    install line naming plain ``noeta`` points a reader at something else
    entirely. An "Out of scope" section is stripped before the scan — that is
    the one place a page may name such a line in order to disclaim it.
    """
    forbidden_phrases = (
        "uv add noeta",
        "pip install noeta\n",  # trailing \n so `pip install noeta-sdk` passes
        "pypi.org/project/noeta",
    )

    md_paths = _user_facing_doc_pages()
    md_paths.append(_REPO_ROOT / "README.md")

    for md in md_paths:
        if not md.exists():
            continue
        text = md.read_text(encoding="utf-8")
        # Strip the "Out of scope" section: from a heading line that contains
        # "Out of scope" to the next heading or EOF.
        scrubbed = re.sub(
            r"(^|\n)#{1,6}[^\n]*Out of scope[^\n]*\n.*?(?=\n#{1,6}\s|\Z)",
            "\n",
            text,
            flags=re.DOTALL,
        )
        for phrase in forbidden_phrases:
            assert phrase not in scrubbed, (
                f"{md} names the bare ``noeta`` install path ({phrase!r}) "
                f"outside its 'Out of scope' section; that dist name belongs "
                f"to an unrelated package — install ``noeta-sdk`` instead."
            )


# Framings the durable wake contradicts. Plain substrings; "operator re-issue"
# is handled separately below, because the correct wording ("No operator
# re-issue is needed") legitimately contains it.
_BANNED_WAKE_PHRASES = (
    "at-most-once wake",
    "lost wake",
    "wake event is lost",
)


def test_no_pre_h2_wake_residue_in_user_docs() -> None:
    """The wake is single-worker, durable and exactly-once, so no user-facing
    page may describe it as lossy or tell a reader to re-issue one by hand —
    that would teach an operational habit the runtime does not need.

    ADR records argue rejected alternatives on purpose and are excluded (see
    ``_NON_USER_DOC_SUBTREES``). "operator re-issue" is counted rather than
    matched: only the affirmative recipe is wrong, and the correct wording
    ("No operator re-issue is needed") contains the same substring."""
    scanned = _user_facing_doc_pages()
    scanned.append(_REPO_ROOT / "README.md")
    offenders: list[str] = []
    for path in scanned:
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        low = text.lower()
        for phrase in _BANNED_WAKE_PHRASES:
            if phrase in low:
                offenders.append(f"{path.relative_to(_REPO_ROOT)}: {phrase!r}")
        # "operator re-issue" is only stale in the AFFIRMATIVE old recipe; the
        # correct H2 wording "No operator re-issue is needed" is allowed. Flag
        # only when occurrences exceed the negated ones.
        total_reissue = len(re.findall(r"operator re-issue", text, re.IGNORECASE))
        negated_reissue = len(
            re.findall(r"no\s+operator re-issue", text, re.IGNORECASE)
        )
        if total_reissue > negated_reissue:
            offenders.append(
                f"{path.relative_to(_REPO_ROOT)}: affirmative 'operator re-issue'"
            )
    assert not offenders, (
        "pre-H2 wake framing resurfaced — sweep to 'single-worker durable "
        "exactly-once wake' (H2). ADR/.scratch are excluded:\n"
        + "\n".join(offenders)
    )
