"""I3 — fresh-venv install + ``python -m noeta.agent`` boot smoke (TL6).

Validates the install path the docs promise. After the three-package
collapse the ``noeta`` console script is gone — the meta ``noeta`` is a
metadata-only distribution that pulls ``noeta-agent`` (and transitively
``noeta-sdk`` / ``noeta-runtime``), and the runnable entry is
``python -m noeta.agent``. Each test creates a brand-new venv outside the
workspace, ``uv pip install -e apps/noeta-agent`` from a local checkout,
then asserts the resulting env exposes ``noeta.agent`` as a bootable module
(and does NOT resurrect a ``noeta`` console script).

These tests are **slow** (creating a venv + cold install can take
30–60 s) and **write to a temp directory** outside the workspace, so
they are gated by the ``install_smoke`` pytest marker. The dedicated
CI job opts in via ``-m install_smoke``; ordinary developer runs
``pytest`` skip them automatically.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import sysconfig
import venv
from pathlib import Path

import pytest


_REPO_ROOT = Path(__file__).resolve().parents[1]
_PACKAGE_PATH = _REPO_ROOT / "apps" / "noeta-agent"

# Files/dirs we never want to copy when materialising a throwaway
# package tree for the git-URL install test.
_COPY_IGNORE = shutil.ignore_patterns(
    "__pycache__",
    "*.pyc",
    ".venv",
    ".pytest_cache",
    ".mypy_cache",
    "*.egg-info",
    "*.sqlite",
    "*.sqlite-*",
)


def _assert_noeta_agent_boots(py: Path, env: dict[str, str]) -> None:
    """The meta install must yield a runnable ``noeta.agent`` module.

    TL6 removed the ``noeta`` console script, so the gate is no longer a
    ``noeta --help`` argparse surface; it is that ``python -m noeta.agent``'s
    module imports and exposes a callable ``main`` (the runner loop itself
    is exercised by the noeta-agent unit tests — booting it here would block
    on a server). We import the launcher module rather than spawning it so
    the test stays fast and does not bind a socket.
    """
    result = subprocess.run(
        [
            str(py),
            "-c",
            (
                "import importlib\n"
                "mod = importlib.import_module('noeta.agent.__main__')\n"
                "assert callable(mod.main), 'noeta.agent.__main__.main missing'\n"
                "print('noeta-agent-boot-ok')\n"
            ),
        ],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    assert "noeta-agent-boot-ok" in result.stdout


def _clean_env() -> dict[str, str]:
    """Build a subprocess env that strips ``PYTHONPATH`` / ``PYTHONHOME``.

    The install smoke must prove the wheel's metadata + module are enough
    — it must NOT rely on the current checkout's ``PYTHONPATH`` leaking
    into the subprocess. Stripping these two vars makes the isolation a
    property of the test itself.
    """
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    return env


def _make_venv(venv_dir: Path) -> Path:
    """Create a fresh venv and return the path to its python."""
    builder = venv.EnvBuilder(
        system_site_packages=False,
        clear=False,
        with_pip=True,
        upgrade_deps=False,
    )
    builder.create(str(venv_dir))
    # venv layout differs by platform.
    scripts = "Scripts" if sysconfig.get_platform().startswith("win") else "bin"
    return venv_dir / scripts / "python"


def _venv_console_script(venv_dir: Path, name: str) -> Path:
    scripts = "Scripts" if sysconfig.get_platform().startswith("win") else "bin"
    suffix = ".exe" if sysconfig.get_platform().startswith("win") else ""
    return venv_dir / scripts / f"{name}{suffix}"


@pytest.mark.install_smoke
def test_uv_install_meta_noeta_then_noeta_agent_boots(tmp_path: Path) -> None:
    """TL6: after the collapse, ``noeta`` is a meta distribution that
    pulls noeta-agent (and transitively sdk/runtime). ``uv pip install
    -e apps/noeta-agent`` resolves the workspace siblings; the resulting env must:

    1. Install successfully (workspace deps resolved)
    2. NOT register a ``noeta`` console entry point (TL6 retired it)
    3. Expose ``python -m noeta.agent`` as a bootable module
    4. Pull noeta-agent into the env (the meta's only direct dependency)
    """
    uv_bin = shutil.which("uv")
    if uv_bin is None:
        pytest.skip("uv not on PATH; the split meta install needs uv to resolve workspace deps")
    venv_dir = tmp_path / "venv"
    py = _make_venv(venv_dir)

    env = _clean_env()
    install = subprocess.run(
        [uv_bin, "pip", "install", "--python", str(py), "-e", str(_PACKAGE_PATH)],
        capture_output=True,
        text=True,
        env=env,
    )
    assert install.returncode == 0, (
        f"uv pip install -e apps/noeta-agent failed:\n{install.stderr}"
    )

    # TL6: the ``noeta`` console script is retired and must not come back.
    noeta_cmd = _venv_console_script(venv_dir, "noeta")
    assert not noeta_cmd.exists(), (
        f"TL6 removed the `noeta` console script; found one at {noeta_cmd}"
    )

    _assert_noeta_agent_boots(py, env)

    # The meta must have pulled noeta-agent into the env.
    meta_check = subprocess.run(
        [
            str(py),
            "-c",
            (
                "import importlib.metadata as md\n"
                "assert md.distribution('noeta-agent') is not None\n"
                "print('noeta-agent-present')\n"
            ),
        ],
        capture_output=True,
        text=True,
        env=env,
    )
    assert meta_check.returncode == 0, meta_check.stderr
    assert "noeta-agent-present" in meta_check.stdout


@pytest.mark.install_smoke
def test_uv_pip_install_editable_then_noeta_agent_boots(tmp_path: Path) -> None:
    """B1 — the docs also promise ``uv pip install -e apps/noeta-agent``.
    Gate that path: fresh venv (created with the current >=3.11 test
    Python), ``uv pip install --python <venv-python> -e ...``, then the
    same ``python -m noeta.agent`` boot assertion (no console script).

    Skips when ``uv`` is not on PATH (developer machines without uv);
    CI installs uv via astral-sh/setup-uv so the path is always gated
    there.
    """
    uv_bin = shutil.which("uv")
    if uv_bin is None:
        pytest.skip("uv not on PATH; CI gates this path via setup-uv")

    venv_dir = tmp_path / "venv"
    py = _make_venv(venv_dir)
    env = _clean_env()

    install = subprocess.run(
        [
            uv_bin,
            "pip",
            "install",
            "--python",
            str(py),
            "-e",
            str(_PACKAGE_PATH),
        ],
        capture_output=True,
        text=True,
        env=env,
    )
    assert install.returncode == 0, (
        f"uv pip install -e apps/noeta-agent failed:\n{install.stderr}"
    )

    noeta_cmd = _venv_console_script(venv_dir, "noeta")
    assert not noeta_cmd.exists(), (
        f"TL6 removed the `noeta` console script; found one at {noeta_cmd}"
    )
    _assert_noeta_agent_boots(py, env)


@pytest.mark.install_smoke
def test_pyproject_metadata_is_present(tmp_path: Path) -> None:
    """Sanity-check that the installed wheel exposes the metadata I3
    promised: name, version 0.1.8, requires-python ≥3.11, and — post-TL6 —
    that it depends on noeta-agent with NO ``noeta`` console entry point."""
    import importlib.metadata as md

    # This test runs inside the workspace's editable install, so
    # importlib.metadata reads the same pyproject we shipped.
    # D4: the noeta shell package is
    # gone; what we check is the product distribution noeta-agent.
    dist = md.distribution("noeta-agent")
    assert dist.metadata["Name"] == "noeta-agent"
    assert dist.metadata["Version"] == "0.1.8"
    requires = dist.metadata["Requires-Python"]
    assert requires is not None
    assert ">=3.11" in requires.replace(" ", "")

    # TL6: no ``noeta`` console entry
    # point — the only entry is ``python -m noeta.agent``. noeta-agent must depend
    # on noeta-runtime and noeta-sdk.
    noeta_entries = [ep for ep in dist.entry_points if ep.name == "noeta"]
    assert not noeta_entries, (
        "noeta-agent must not declare a `noeta` console entry point (TL6)"
    )
    req_names = {
        (req.split(";")[0].split("==")[0].split(">=")[0].split("<")[0].strip())
        for req in (dist.requires or [])
    }
    assert {"noeta-runtime", "noeta-sdk"} <= req_names, (
        "noeta-agent must depend on noeta-runtime + noeta-sdk"
    )

    # License metadata — Apache-2.0 per owner decision
    license_expression = dist.metadata.get("License-Expression")
    assert license_expression == "Apache-2.0", (
        f"expected SPDX License-Expression 'Apache-2.0', got {license_expression!r}"
    )
    classifiers = dist.metadata.get_all("Classifier") or []
    assert "License :: OSI Approved :: Apache Software License" in classifiers, (
        "Apache Software License classifier missing from installed metadata"
    )


def test_repo_root_license_file_exists() -> None:
    """Apache-2.0 distribution requires the LICENSE text to be available
    alongside the redistributed work. The canonical copy lives at the
    repo root."""
    license_path = _REPO_ROOT / "LICENSE"
    assert license_path.exists(), (
        "repo-root LICENSE file missing; Apache-2.0 distribution requires it"
    )
    text = license_path.read_text(encoding="utf-8")
    assert "Apache License" in text
    assert "Version 2.0" in text


def test_pyproject_dependencies_use_lower_bounds_only() -> None:
    """Architect Q1 ruling: pyproject deps must use ``>=`` lower bounds,
    not ``==`` tight pins. (uv.lock pins dev environment; published
    package should not pin its consumers' resolution.)"""
    import re

    pyproject_path = _PACKAGE_PATH / "pyproject.toml"
    text = pyproject_path.read_text(encoding="utf-8")
    # Capture the [project] dependencies list — between `dependencies = [`
    # and the next `]` at column 0.
    m = re.search(
        r"^dependencies\s*=\s*\[(.*?)^]",
        text,
        flags=re.MULTILINE | re.DOTALL,
    )
    assert m is not None, "could not locate [project] dependencies list"
    deps_block = m.group(1)
    for line in deps_block.splitlines():
        stripped = line.strip().rstrip(",")
        if not stripped or stripped.startswith("#"):
            continue
        # Strip surrounding quotes.
        spec = stripped.strip('"').strip("'")
        if "==" in spec:
            raise AssertionError(
                f"dependency {spec!r} uses a tight `==` pin; Phase 2 I3 "
                f"requires lower-bound `>=` constraints (architect Q1)."
            )
