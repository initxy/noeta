"""Behavioural tests for ``scripts/lint-naming.py``.

The lint script walks a root directory (typically the repo root), reads
project source/doc files and reports any file that uses a banned name
(``class Run`` / ``class Workflow`` / ``class Session`` / ``class Mutator`` /
``class Pattern`` / ``WorkflowRunner`` / ``WorkflowPolicy`` / ``WorkflowSpec`` /
``SessionStore`` / ``ConversationManager``). The script exits non-zero when
violations exist and zero when they do not.

These tests build small temporary roots that mimic the real project layout
and shell out to the script to exercise the same code path CI runs.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "lint-naming.py"


def _run(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(root)],
        capture_output=True,
        text=True,
        check=False,
    )


def test_clean_tree_exits_zero(tmp_path: Path) -> None:
    pkg = tmp_path / "packages" / "noeta" / "noeta"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("# clean\n")
    (pkg / "mod.py").write_text("class Engine:\n    pass\n")
    result = _run(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr


def test_banned_class_run_in_source_fails(tmp_path: Path) -> None:
    pkg = tmp_path / "packages" / "noeta" / "noeta"
    pkg.mkdir(parents=True)
    (pkg / "bad.py").write_text("class Run:\n    pass\n")
    result = _run(tmp_path)
    assert result.returncode != 0
    assert "class Run" in result.stdout
    assert "bad.py" in result.stdout


@pytest.mark.parametrize(
    "needle",
    [
        "class Workflow",
        "class Session",
        "class Mutator",
        "class Pattern",
        "WorkflowRunner",
        "WorkflowPolicy",
        "WorkflowSpec",
        "SessionStore",
        "ConversationManager",
    ],
)
def test_every_banned_string_is_detected(tmp_path: Path, needle: str) -> None:
    pkg = tmp_path / "packages" / "noeta" / "noeta"
    pkg.mkdir(parents=True)
    (pkg / "bad.py").write_text(f"# violates: {needle}\n")
    result = _run(tmp_path)
    assert result.returncode != 0
    assert needle in result.stdout


def test_examples_dir_is_scanned(tmp_path: Path) -> None:
    examples = tmp_path / "examples"
    examples.mkdir()
    (examples / "demo.py").write_text("class Workflow:\n    pass\n")
    result = _run(tmp_path)
    assert result.returncode != 0
    assert "demo.py" in result.stdout


def test_scripts_dir_is_scanned(tmp_path: Path) -> None:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "bad.py").write_text("WorkflowRunner = 1\n")
    result = _run(tmp_path)
    assert result.returncode != 0


def test_root_readme_is_scanned(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("This project bans WorkflowSpec.\n")
    result = _run(tmp_path)
    assert result.returncode != 0
    assert "README.md" in result.stdout


def test_scratch_dir_is_exempted(tmp_path: Path) -> None:
    scratch = tmp_path / ".scratch" / "phase-0-kernel"
    scratch.mkdir(parents=True)
    (scratch / "issue.md").write_text("- ban: WorkflowRunner / SessionStore\n")
    pkg = tmp_path / "packages" / "noeta" / "noeta"
    pkg.mkdir(parents=True)
    (pkg / "ok.py").write_text("# clean\n")
    result = _run(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr


def test_docs_adr_dir_is_exempted(tmp_path: Path) -> None:
    adr = tmp_path / "docs" / "adr"
    adr.mkdir(parents=True)
    (adr / "0001.md").write_text(
        "We forbid `WorkflowSpec` and `SessionStore`.\n"
    )
    pkg = tmp_path / "packages" / "noeta" / "noeta"
    pkg.mkdir(parents=True)
    (pkg / "ok.py").write_text("# clean\n")
    result = _run(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr


def test_context_md_is_exempted(tmp_path: Path) -> None:
    (tmp_path / "CONTEXT.md").write_text(
        "Flagged ambiguities: WorkflowSpec / SessionStore.\n"
    )
    pkg = tmp_path / "packages" / "noeta" / "noeta"
    pkg.mkdir(parents=True)
    (pkg / "ok.py").write_text("# clean\n")
    result = _run(tmp_path)
    assert result.returncode == 0


def test_venv_and_caches_are_excluded(tmp_path: Path) -> None:
    venv = tmp_path / ".venv" / "lib"
    venv.mkdir(parents=True)
    (venv / "vendored.py").write_text("class Run:\n    pass\n")
    pycache = tmp_path / "packages" / "noeta" / "noeta" / "__pycache__"
    pycache.mkdir(parents=True)
    (pycache / "x.py").write_text("class Workflow:\n    pass\n")
    pkg = tmp_path / "packages" / "noeta" / "noeta"
    (pkg / "ok.py").write_text("# clean\n")
    result = _run(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr


def test_real_repo_is_clean() -> None:
    """Run the script against the actual repo: it must pass."""
    result = _run(REPO_ROOT)
    assert result.returncode == 0, result.stdout + result.stderr


# ---------------------------------------------------------------------------
# Session-as-identity ban (CONTEXT.md `Flagged ambiguities` -> "Session").
# Identity is banned, construction scope is not; see the CONTEXT.md entry.


@pytest.mark.parametrize(
    "name",
    [
        "session_id",
        "session_root_id",
        "list_session_summaries",
        "max_background_jobs_per_session",
        "kill_session",
        "SessionCatalog",
    ],
)
def test_session_identity_names_are_rejected(tmp_path: Path, name: str) -> None:
    pkg = tmp_path / "packages" / "noeta-runtime" / "noeta"
    pkg.mkdir(parents=True)
    (pkg / "bad.py").write_text(f"{name} = 1\n")
    result = _run(tmp_path)
    assert result.returncode != 0, result.stdout
    assert name in result.stdout


@pytest.mark.parametrize(
    "name",
    [
        # Construction-scope vocabulary — core terms, not banned identities.
        "session_pack",
        "default_session_packs",
        "build_fs_session_pack",
        "_SESSION_PACK_CACHE",
        "SessionBuildContext",
        "SessionPackEntry",
        "SessionRecorder",
        "SessionInputs",
        "build_session_inputs",
        # subprocess.Popen's own keyword.
        "start_new_session",
    ],
)
def test_session_scope_vocabulary_is_allowed(tmp_path: Path, name: str) -> None:
    pkg = tmp_path / "packages" / "noeta-runtime" / "noeta"
    pkg.mkdir(parents=True)
    (pkg / "ok.py").write_text(f"{name} = 1\n")
    result = _run(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr


def test_bare_session_word_in_prose_is_allowed(tmp_path: Path) -> None:
    """"session" as a scope adjective is legal — only compounds are identities."""
    pkg = tmp_path / "packages" / "noeta-runtime" / "noeta"
    pkg.mkdir(parents=True)
    (pkg / "ok.py").write_text("# one container per session, torn down at the end\n")
    result = _run(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr


def test_session_rule_does_not_apply_to_tests_dir(tmp_path: Path) -> None:
    """A test harness stands in for a host, and a host may own the concept."""
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "helper.py").write_text("def _sdk_session():\n    return 1\n")
    result = _run(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr


def test_session_rule_applies_to_examples(tmp_path: Path) -> None:
    examples = tmp_path / "examples" / "reference-host"
    examples.mkdir(parents=True)
    (examples / "app.py").write_text("session_id = 'x'\n")
    result = _run(tmp_path)
    assert result.returncode != 0
    assert "session_id" in result.stdout


def test_changelog_is_exempt_as_released_history(tmp_path: Path) -> None:
    (tmp_path / "CHANGELOG.md").write_text(
        "## [0.3.2]\n- `HostConfig.sandbox_session_policy` added.\n"
    )
    result = _run(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
