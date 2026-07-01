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


def test_docs_design_dir_is_exempted(tmp_path: Path) -> None:
    design = tmp_path / "docs" / "design"
    design.mkdir(parents=True)
    (design / "sdd.md").write_text("Avoid: WorkflowRunner.\n")
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
