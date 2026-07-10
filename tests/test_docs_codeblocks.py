"""Phase 2 I2 — runnable docs codeblock extraction + execution.

Docs and READMEs accumulate stale snippets fast unless something
re-runs them. This test scans the repo-root ``README.md`` and the
``docs/*.md`` tree, extracts any fenced code block immediately
preceded by an HTML comment ``<!-- runnable: <tag> -->``, and runs
it. Untagged code blocks (illustrative bash, configuration, output
samples) are skipped.

The opt-in convention is intentional — the test must not auto-run
``pip install`` shell snippets or other side-effectful examples. Only
blocks the author marked runnable participate.

Supported runnable tags this issue ships:

* ``smoke`` — a quick in-process Python smoke that exits 0 fast.

The extractor is a small regex (per architect: extract codeblocks with
a small regex, no new dependency); no external library is introduced.
"""

from __future__ import annotations

import re
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

# Subtrees under docs/ that the user-doc scan gates skip: ADR decision
# records describe before-states by design, and implementation-specs /
# _research are internal working artifacts, not user-facing docs.
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
    """The README + quickstart must contain at least one runnable
    smoke block — otherwise the docs are silently un-tested."""
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

    Each block runs in an isolated namespace (``exec(body, ns)``) so
    its top-level ``assert`` becomes the success signal."""
    assert tag == "smoke", (
        f"unknown runnable tag {tag!r} in {md_path}; only 'smoke' is supported"
    )
    assert lang == "python", (
        f"smoke blocks must be Python (got lang={lang!r}) in {md_path}"
    )
    namespace: dict[str, object] = {"__name__": "__codeblock__"}
    dedented = textwrap.dedent(body)
    try:
        exec(compile(dedented, str(md_path), "exec"), namespace)
    except SystemExit as exc:
        if exc.code not in (None, 0):
            raise AssertionError(
                f"smoke block in {md_path} exited non-zero: {exc.code}"
            ) from exc


def test_repo_root_readme_is_the_canonical_entry() -> None:
    """Architect Q1 ruling: repo-root README is the canonical entry.
    Do NOT create a second ``packages/noeta/README.md``."""
    repo_root_readme = _REPO_ROOT / "README.md"
    package_readme = _REPO_ROOT / "packages" / "noeta" / "README.md"

    assert repo_root_readme.exists(), "repo-root README.md must exist"
    assert not package_readme.exists(), (
        "do not create packages/noeta/README.md (architect Q1 — repo-root only)"
    )


def test_docs_dont_promise_pypi_install_paths() -> None:
    """Architect PRD §2 Non-goal: docs MUST NOT promise PyPI / `uv add noeta`.
    The install paths Phase 2 actually ships are local checkout and
    git+url with a subdirectory.

    Scans the repo-root README in addition to the recursive user-facing
    ``docs/**/*.md`` pages (per architect NB2 in I2 rev1 review; the
    ADR / internal subtrees are excluded — see
    ``_NON_USER_DOC_SUBTREES``). The README's "Out of scope" section is
    the one allowed mention — it explicitly lists PyPI / ``uv add
    noeta`` as NOT shipping in Phase 2 — so we only flag the forbidden
    phrases when they appear OUTSIDE that section.
    """
    forbidden_phrases = (
        "uv add noeta",
        "pip install noeta\n",  # bare PyPI install line (allow `pip install -e packages/noeta`)
        "pypi.org/project/noeta",
    )

    md_paths = _user_facing_doc_pages()
    md_paths.append(_REPO_ROOT / "README.md")

    for md in md_paths:
        if not md.exists():
            continue
        text = md.read_text(encoding="utf-8")
        # Strip the "Out of scope" section: from a heading line that
        # contains "Out of scope" to the next heading or EOF. The
        # allowed PyPI mentions live only in that section.
        scrubbed = re.sub(
            r"(^|\n)#{1,6}[^\n]*Out of scope[^\n]*\n.*?(?=\n#{1,6}\s|\Z)",
            "\n",
            text,
            flags=re.DOTALL,
        )
        for phrase in forbidden_phrases:
            assert phrase not in scrubbed, (
                f"{md} promises a PyPI install path ({phrase!r}) outside "
                f"its 'Out of scope' section; Phase 2 does not publish "
                f"to PyPI — use local checkout or git+url instead."
            )


# Pre-H2 wake framing banned from user-facing docs + help (CW4). Plain
# substrings; "operator re-issue" is handled separately because the CORRECT
# H2 wording "No operator re-issue is needed" legitimately contains it.
_BANNED_WAKE_PHRASES = (
    "at-most-once wake",
    "lost wake",
    "wake event is lost",
)


def test_no_pre_h2_wake_residue_in_user_docs() -> None:
    """CW4 residue gate — H2 shipped **single-worker durable
    exactly-once wake**, so user-facing docs must not carry the pre-H2
    'at-most-once / lost wake / operator re-issue' framing. ADR decision
    records (a separate subtree — excluded via ``_NON_USER_DOC_SUBTREES``)
    and ``.scratch`` design history describe the before-state by design
    and are intentionally NOT scanned. (The ``noeta serve --help`` source the
    gate used to also scan went away with the operator CLI in TL6.)
    Lightweight (regex over a handful of files; no new dependency)."""
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
