"""Fresh-venv wheel install smoke for the two library distributions.

Build the noeta-runtime and noeta-sdk wheels, install them into a
brand-new venv outside the workspace, and import the public surface fully
offline. The wheels — not an editable install — are the artifact under
test: the packaged metadata + module tree must be enough on their own
(``PYTHONPATH`` is stripped from the subprocess env).

These tests are **slow** (venv + wheel build + cold install can take
30-60 s) and **write to a temp directory** outside the workspace, so they
are gated by the ``install_smoke`` pytest marker (root pyproject addopts
deselect it). The dedicated CI job opts in via ``-m install_smoke``;
ordinary developer runs ``pytest`` skip them automatically.
"""

from __future__ import annotations

import ast
import os
import shutil
import subprocess
import sysconfig
import venv
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
# The two shipped library distributions.
_PACKAGE_DIRS = (
    _REPO_ROOT / "packages" / "noeta-runtime",
    _REPO_ROOT / "packages" / "noeta-sdk",
)

#: Distribution name → (source root, the noeta-* dists it may import from).
#: The closure is what the dist's ``dependencies`` actually declare:
#: noeta-runtime depends on no noeta package, noeta-sdk on runtime.
_DISTRIBUTIONS: dict[str, tuple[Path, frozenset[str]]] = {
    "noeta-runtime": (
        _REPO_ROOT / "packages" / "noeta-runtime" / "noeta",
        frozenset({"noeta-runtime"}),
    ),
    "noeta-sdk": (
        _REPO_ROOT / "packages" / "noeta-sdk" / "noeta",
        frozenset({"noeta-sdk", "noeta-runtime"}),
    ),
}


def _clean_env() -> dict[str, str]:
    """Subprocess env without ``PYTHONPATH`` / ``PYTHONHOME``.

    The install smoke must prove the wheels' metadata + modules are enough —
    it must NOT rely on the current checkout leaking into the subprocess.
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
    scripts = "Scripts" if sysconfig.get_platform().startswith("win") else "bin"
    return venv_dir / scripts / "python"


def _venv_console_script(venv_dir: Path, name: str) -> Path:
    scripts = "Scripts" if sysconfig.get_platform().startswith("win") else "bin"
    suffix = ".exe" if sysconfig.get_platform().startswith("win") else ""
    return venv_dir / scripts / f"{name}{suffix}"


@pytest.mark.install_smoke
def test_wheel_install_imports_public_surface(tmp_path: Path) -> None:
    """Build the two wheels, install them into a fresh venv, and import
    the public surface fully offline.

    Also gates the TL6 invariant: the wheels must not resurrect a ``noeta``
    console script — the libraries ship no entry point at all.
    """
    uv_bin = shutil.which("uv")
    if uv_bin is None:
        pytest.skip("uv not on PATH; CI gates this path via setup-uv")
    env = _clean_env()

    # 1. Build the two library wheels out of the workspace checkout.
    dist_dir = tmp_path / "dist"
    for package_dir in _PACKAGE_DIRS:
        build = subprocess.run(
            [uv_bin, "build", "--wheel", "--out-dir", str(dist_dir),
             str(package_dir)],
            capture_output=True,
            text=True,
            env=env,
        )
        assert build.returncode == 0, (
            f"uv build {package_dir.name} failed:\n{build.stderr}"
        )
    wheels = sorted(dist_dir.glob("*.whl"))
    assert len(wheels) == 2, f"expected 2 wheels, got {[w.name for w in wheels]}"

    # 2. Install the wheels into a brand-new venv (third-party deps resolve
    # from the index / uv cache; the noeta-* inter-deps from the local wheels).
    venv_dir = tmp_path / "venv"
    py = _make_venv(venv_dir)
    install = subprocess.run(
        [uv_bin, "pip", "install", "--python", str(py)]
        + [str(w) for w in wheels],
        capture_output=True,
        text=True,
        env=env,
    )
    assert install.returncode == 0, (
        f"uv pip install of the built wheels failed:\n{install.stderr}"
    )

    # TL6: no console script; the libraries ship no entry point at all.
    noeta_cmd = _venv_console_script(venv_dir, "noeta")
    assert not noeta_cmd.exists(), (
        f"the wheels must not install a `noeta` console script; found {noeta_cmd}"
    )

    # 3. Import the public surface in the fresh venv, fully offline.
    smoke = subprocess.run(
        [
            str(py),
            "-c",
            (
                "import noeta.sdk, noeta.sdk.storage, noeta.presets; "
                "from noeta.sdk import Client, Options, PluginAPI, "
                "load_plugins, merge_plugins; "
                "print('ok')"
            ),
        ],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(tmp_path),
    )
    assert smoke.returncode == 0, (
        f"public-surface import failed in the fresh venv:\n{smoke.stderr}"
    )
    assert smoke.stdout.strip() == "ok"


def _module_owners() -> dict[str, str]:
    """Map every shipped ``noeta.*`` module path to the dist that ships it.

    Built by walking the checkout rather than hard-coded, so it stays honest
    when a subpackage moves between distributions.
    """
    owners: dict[str, str] = {}
    for dist, (source_root, _) in _DISTRIBUTIONS.items():
        for path in source_root.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            rel = path.relative_to(source_root.parent).with_suffix("")
            parts = list(rel.parts)
            if parts[-1] == "__init__":
                parts.pop()
            owners[".".join(parts)] = dist
    return owners


def _imported_noeta_modules(path: Path) -> set[str]:
    """Every ``noeta.*`` module name imported by ``path`` (absolute imports).

    Relative imports stay inside the importing package, so they can never
    cross a distribution boundary and are skipped.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "noeta" or alias.name.startswith("noeta."):
                    found.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # relative — intra-package by construction
                continue
            module = node.module or ""
            if module == "noeta" or module.startswith("noeta."):
                found.add(module)
                # ``from noeta.x import y`` may be importing a submodule y.
                for alias in node.names:
                    found.add(f"{module}.{alias.name}")
    return found


def test_no_distribution_imports_outside_its_dependency_closure() -> None:
    """A wheel may only import ``noeta.*`` modules its own dependencies ship.

    The PEP 420 namespace makes every distribution's modules look importable
    from inside the workspace checkout, so a misplaced subpackage passes every
    editable-install test and only breaks for the user who installs that one
    wheel. ``noeta.presets`` shipped in the noeta-runtime wheel while importing
    ``noeta.client.*`` (noeta-sdk) — ``pip install noeta-runtime`` then made
    ``import noeta.presets`` raise ``ModuleNotFoundError``. This pins the
    boundary statically, with no wheel build.
    """
    owners = _module_owners()
    violations: list[str] = []
    for dist, (source_root, allowed) in _DISTRIBUTIONS.items():
        for path in sorted(source_root.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            for module in sorted(_imported_noeta_modules(path)):
                # Longest-prefix match: ``noeta.agent`` is split across the
                # runtime (spec/registry) and the product (everything else).
                owner = None
                probe = module
                while probe and owner is None:
                    owner = owners.get(probe)
                    probe = probe.rpartition(".")[0]
                if owner is None or owner in allowed:
                    continue
                violations.append(
                    f"{path.relative_to(_REPO_ROOT)} ({dist}) imports "
                    f"{module!r}, shipped by {owner}"
                )
    assert not violations, (
        "distribution boundary violated — these modules would raise "
        "ModuleNotFoundError when their own wheel is installed alone:\n  "
        + "\n  ".join(violations)
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
    """Published packages must use ``>=`` lower bounds, not ``==`` tight
    pins (uv.lock pins the dev environment; a published package should not
    pin its consumers' resolution). Deliberate ``<`` upper bounds on
    pre-1.0 deps are allowed."""
    import re

    for package_dir in _PACKAGE_DIRS:
        pyproject_path = package_dir / "pyproject.toml"
        text = pyproject_path.read_text(encoding="utf-8")
        m = re.search(
            r"^dependencies\s*=\s*\[(.*?)^]",
            text,
            flags=re.MULTILINE | re.DOTALL,
        )
        if m is None:
            continue  # no [project] dependencies list
        for line in m.group(1).splitlines():
            stripped = line.strip().rstrip(",")
            if not stripped or stripped.startswith("#"):
                continue
            spec = stripped.strip('"').strip("'")
            assert "==" not in spec, (
                f"{package_dir.name}: dependency {spec!r} uses a tight `==` "
                f"pin; published packages require `>=` lower bounds"
            )
