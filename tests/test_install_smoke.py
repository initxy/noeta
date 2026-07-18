"""Fresh-venv wheel install + ``python -m noeta.agent`` boot smoke.

Validates the install path the server platform promises: build the three
wheels (noeta-runtime, noeta-sdk, noeta-agent), install them into a
brand-new venv outside the workspace, boot the server with the offline mock
provider (sandbox off, temp data dir), hit the health endpoint, and shut
down cleanly. The wheels — not an editable install — are the artifact under
test: the packaged metadata + module tree must be enough on their own
(``PYTHONPATH`` is stripped from the subprocess env).

These tests are **slow** (venv + wheel build + cold install can take
30-60 s) and **write to a temp directory** outside the workspace, so they
are gated by the ``install_smoke`` pytest marker (root pyproject addopts
deselect it). The dedicated CI job opts in via ``-m install_smoke``;
ordinary developer runs ``pytest`` skip them automatically.
"""

from __future__ import annotations

import os
import shutil
import signal
import socket
import subprocess
import sysconfig
import time
import urllib.error
import urllib.request
import venv
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
# The three shipped distributions: the two libraries + the product.
_PACKAGE_DIRS = (
    _REPO_ROOT / "packages" / "noeta-runtime",
    _REPO_ROOT / "packages" / "noeta-sdk",
    _REPO_ROOT / "apps" / "noeta-agent",
)


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


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.mark.install_smoke
def test_wheel_install_boots_server_and_serves_health(tmp_path: Path) -> None:
    """Build the three wheels, install them into a fresh venv, boot
    ``python -m noeta.agent`` fully offline (mock provider, sandbox off,
    temp data dir), assert ``GET /api/v1/health`` answers, and shut down.

    Also gates the TL6 invariant: the wheels must not resurrect a ``noeta``
    console script — ``python -m noeta.agent`` is the only entry.
    """
    uv_bin = shutil.which("uv")
    if uv_bin is None:
        pytest.skip("uv not on PATH; CI gates this path via setup-uv")
    env = _clean_env()

    # 1. Build the three wheels out of the workspace checkout.
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
    assert len(wheels) == 3, f"expected 3 wheels, got {[w.name for w in wheels]}"

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

    # TL6: no console script; the module entry is the only entry.
    noeta_cmd = _venv_console_script(venv_dir, "noeta")
    assert not noeta_cmd.exists(), (
        f"the wheels must not install a `noeta` console script; found {noeta_cmd}"
    )

    # 3. Boot the server offline: mock provider, sandbox off, temp data dir.
    # The installed config.py resolves relative paths against site-packages,
    # so every path setting must be absolute here; missing models.json
    # degrades to the built-in single-model fallback by design.
    port = _free_port()
    server_env = dict(
        env,
        HOST="127.0.0.1",
        PORT=str(port),
        DATA_DIR=str(tmp_path / "data"),
        SHARED_DATA_DIR=str(tmp_path / "data" / "shared"),
        LLM_PROVIDER="mock",
        SANDBOX_ENABLED="false",
        MEMORY_CONSOLIDATION="false",
        SESSION_SECRET="install-smoke-secret",
    )
    proc = subprocess.Popen(
        [str(py), "-m", "noeta.agent"],
        cwd=str(tmp_path),
        env=server_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        # 4. Health must answer with the mock provider identity.
        health_url = f"http://127.0.0.1:{port}/api/v1/health"
        deadline = time.time() + 60
        body: str | None = None
        while time.time() < deadline:
            if proc.poll() is not None:
                out = proc.stdout.read() if proc.stdout else ""
                raise AssertionError(
                    f"server exited early ({proc.returncode}):\n{out}"
                )
            try:
                with urllib.request.urlopen(health_url, timeout=2) as resp:
                    assert resp.status == 200
                    body = resp.read().decode()
                    break
            except (urllib.error.URLError, ConnectionError, TimeoutError):
                time.sleep(0.25)
        assert body is not None, "health endpoint never came up within 60 s"
        assert '"ok":true' in body.replace(" ", "")
        assert "mock" in body

        # 5. Graceful shutdown: SIGTERM → uvicorn drains the lifespan, then
        # re-raises the captured signal so the exit status reflects it
        # (modern uvicorn exits -SIGTERM, older versions 0 — both are the
        # graceful path). The drain evidence is the lifespan-shutdown log
        # line plus finishing within the bounded wait.
        proc.terminate()
        try:
            out, _ = proc.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            raise AssertionError("server did not shut down within 30 s")
        assert proc.returncode in (0, -signal.SIGTERM), (
            f"server did not shut down cleanly ({proc.returncode}):\n{out}"
        )
        assert "Application shutdown complete" in out, out
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)


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
