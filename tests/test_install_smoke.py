"""Fresh-venv wheel install smoke for the two library distributions.

Two closures, per the microkernel distribution boundary (acceptance 4/5 of
``docs/implementation-specs/archive/2026-07-29-microkernel-capability-migration.md``):

* **runtime-alone** — the noeta-runtime wheel installed by itself is a pure
  kernel: kernel imports work, no capability impl (``noeta.builtins``) or SDK
  band or ``httpx`` is present, and a hand-injected agent (protocol objects
  only) runs a turn to its terminal event.
* **sdk closure** — both wheels together carry the public surface AND the
  capability impls: ``noeta.builtins`` ships in the sdk wheel and the parts
  doorway resolves the default 11-tool roster from it.

The wheels — not an editable install — are the artifact under
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
import textwrap
import venv
import zipfile
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


def _require_uv() -> str:
    uv_bin = shutil.which("uv")
    if uv_bin is None:
        pytest.skip("uv not on PATH; CI gates this path via setup-uv")
    return uv_bin


def _build_wheel(
    uv_bin: str, package_dir: Path, dist_dir: Path, env: dict[str, str]
) -> Path:
    """Build one package's wheel into ``dist_dir`` and return its path."""
    before = set(dist_dir.glob("*.whl")) if dist_dir.exists() else set()
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
    new = set(dist_dir.glob("*.whl")) - before
    assert len(new) == 1, f"expected 1 new wheel, got {[w.name for w in new]}"
    return new.pop()


@pytest.mark.install_smoke
def test_wheel_install_imports_public_surface(tmp_path: Path) -> None:
    """The **sdk closure**: both wheels into a fresh venv; the public surface
    imports fully offline and the capability impls are present.

    Microkernel acceptance 4, sdk half: ``noeta.builtins`` (manifests + impls)
    ships in the noeta-sdk wheel, and the parts doorway resolves the default
    11-tool roster from it. Also gates the TL6 invariant: the wheels must not
    resurrect a ``noeta`` console script — the libraries ship no entry point
    at all.
    """
    uv_bin = _require_uv()
    env = _clean_env()

    # 1. Build the two library wheels out of the workspace checkout.
    dist_dir = tmp_path / "dist"
    wheels = [
        _build_wheel(uv_bin, package_dir, dist_dir, env)
        for package_dir in _PACKAGE_DIRS
    ]

    # The distribution boundary, asserted on the artifacts themselves: the
    # capability impls (noeta/builtins/…) ship in the sdk wheel ONLY.
    runtime_wheel = next(w for w in wheels if w.name.startswith("noeta_runtime"))
    sdk_wheel = next(w for w in wheels if w.name.startswith("noeta_sdk"))
    assert _wheel_module_paths(runtime_wheel, "noeta/builtins/") == []
    assert _wheel_module_paths(sdk_wheel, "noeta/builtins/") != []

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

    # 3. Import the public surface in the fresh venv, fully offline — and
    # prove the impls arrived: the loader-resolved default roster is the
    # 11 fs/web tools (microkernel D2), reachable only if the sdk wheel
    # carries ``noeta.builtins`` and its impl modules.
    smoke = subprocess.run(
        [
            str(py),
            "-c",
            (
                "import noeta.sdk, noeta.sdk.storage, noeta.presets; "
                "from noeta.sdk import Client, Options, PluginSet, "
                "load_plugin_set, PluginBuilder; "
                "from noeta.client.parts import builtin_tool_classes; "
                "n = len(builtin_tool_classes()); "
                "assert n == 11, f'expected the 11 default tools, got {n}'; "
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


#: The runtime-alone smoke body (microkernel acceptance 4, kernel half). Runs
#: inside the fresh venv where ONLY the noeta-runtime wheel is installed:
#: kernel imports work, no capability impl / SDK band / httpx is importable,
#: and a hand-injected agent (protocol objects only — a hand-written Policy +
#: FakeTool over in-memory storage; since microkernel phase 2b even ReActPolicy
#: lives in the react built-in, so the kernel-alone story is a host-authored
#: Policy against the protocol) runs a turn to its terminal event.
_RUNTIME_ALONE_SCRIPT = """
    import importlib.util

    # 1. Kernel imports work (one module per kernel band). noeta.testing.profile
    # must IMPORT runtime-alone; calling its build_runtime / build_policy_factory
    # needs the sdk (they resolve guard/policy classes from noeta.builtins at
    # call time). noeta.policies is the control band (schemas + translation).
    import noeta.core.engine
    import noeta.runtime.llm
    import noeta.runtime.governance
    import noeta.storage.memory
    import noeta.context.composer
    import noeta.policies.control_tools
    import noeta.read_models.sessions
    import noeta.observers.audit
    import noeta.agent.spec
    import noeta.testing.profile

    # 2. No capability impls, no SDK bands, no httpx in the closure.
    for absent in (
        "noeta.builtins",
        "noeta.client",
        "noeta.sdk",
        "noeta.presets",
        "httpx",
    ):
        assert importlib.util.find_spec(absent) is None, (
            f"{absent} must not be importable when noeta-runtime is "
            f"installed alone"
        )

    # 3. A hand-injected agent runs: the kernel hosts execution; every part —
    # the Policy included — arrives as a protocol object.
    from noeta.core.engine import Engine
    from noeta.protocols.decisions import (
        FinishDecision,
        ToolCall,
        ToolCallsDecision,
    )
    from noeta.storage.memory import (
        InMemoryContentStore,
        InMemoryDispatcher,
        InMemoryEventLog,
    )
    from noeta.testing.composer import trivial_three_segment
    from noeta.tools.fake import FakeTool

    class EchoOncePolicy:
        def __init__(self):
            self._called = False

        def decide(self, ctx, view):
            if not self._called:
                self._called = True
                return ToolCallsDecision(
                    calls=[
                        ToolCall(
                            tool_name="echo",
                            arguments={"text": "hi"},
                            call_id="c-1",
                        )
                    ]
                )
            return FinishDecision(answer="done")

    cs = InMemoryContentStore()
    disp = InMemoryDispatcher()
    log = InMemoryEventLog(lease_validator=disp)
    tool = FakeTool(name="echo", script={("hi",): "echo:hi"})
    engine = Engine(
        event_log=log,
        content_store=cs,
        composer=trivial_three_segment(cs),
        policy=EchoOncePolicy(),
        tools={"echo": tool},
    )
    task = engine.create_task(goal="say hi", policy_name="react")
    disp.enqueue(task.task_id)
    lease = disp.lease(worker_id="w-smoke")
    assert lease is not None
    engine.run_one_step(task, lease_id=lease.lease_id)
    types = [e.type for e in log.read(task.task_id)]
    assert types[-1] == "TaskCompleted", types
    print("runtime-alone ok")
"""


@pytest.mark.install_smoke
def test_runtime_wheel_alone_is_a_pure_kernel(tmp_path: Path) -> None:
    """The **runtime-alone closure**: the kernel wheel installed by itself.

    Microkernel acceptance 4 + 5, kernel half: kernel imports work, no
    capability impl (``noeta.builtins``) / SDK band / ``httpx`` is present,
    and a hand-injected agent (protocols only) runs a scripted turn to
    ``TaskCompleted``. The wheel content is inspected too: no
    ``noeta/builtins/`` portion may ship in the kernel wheel.
    """
    uv_bin = _require_uv()
    env = _clean_env()

    # 1. Build ONLY the runtime wheel and pin its contents impl-free.
    dist_dir = tmp_path / "dist"
    runtime_wheel = _build_wheel(
        uv_bin, _REPO_ROOT / "packages" / "noeta-runtime", dist_dir, env
    )
    assert _wheel_module_paths(runtime_wheel, "noeta/builtins/") == [], (
        "the kernel wheel must not carry capability impls"
    )

    # 2. Fresh venv with the runtime wheel alone (psycopg resolves from the
    # index / uv cache; httpx must NOT arrive — it is an sdk dependency now).
    venv_dir = tmp_path / "venv"
    py = _make_venv(venv_dir)
    install = subprocess.run(
        [uv_bin, "pip", "install", "--python", str(py), str(runtime_wheel)],
        capture_output=True,
        text=True,
        env=env,
    )
    assert install.returncode == 0, (
        f"uv pip install of the runtime wheel failed:\n{install.stderr}"
    )

    # 3. Kernel smoke + hand-injected agent, fully offline.
    script = tmp_path / "runtime_alone_smoke.py"
    script.write_text(
        textwrap.dedent(_RUNTIME_ALONE_SCRIPT).lstrip(), encoding="utf-8"
    )
    smoke = subprocess.run(
        [str(py), str(script)],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(tmp_path),
    )
    assert smoke.returncode == 0, (
        f"runtime-alone smoke failed in the fresh venv:\n{smoke.stderr}"
    )
    assert smoke.stdout.strip() == "runtime-alone ok"


def _wheel_module_paths(wheel: Path, prefix: str) -> list[str]:
    """Every member of ``wheel`` under ``prefix`` (POSIX-style wheel names)."""
    with zipfile.ZipFile(wheel) as zf:
        return [n for n in zf.namelist() if n.startswith(prefix)]


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


def test_httpx_is_an_sdk_dependency_not_a_runtime_one() -> None:
    """Microkernel acceptance 5, statically (no wheel build, every dev run).

    The kernel is transport-free: every HTTP-speaking impl (providers, web,
    mcp, sandbox backends, otlp export) ships in the sdk wheel, so ``httpx``
    must be declared by noeta-sdk and must NOT be declared by noeta-runtime.
    The fresh-venv closure test proves the same on the installed artifacts;
    this pins the declaration itself so a stray re-add fails fast.
    """
    runtime_text = (
        _REPO_ROOT / "packages" / "noeta-runtime" / "pyproject.toml"
    ).read_text(encoding="utf-8")
    sdk_text = (
        _REPO_ROOT / "packages" / "noeta-sdk" / "pyproject.toml"
    ).read_text(encoding="utf-8")
    assert '"httpx' not in runtime_text, (
        "noeta-runtime must not depend on httpx (microkernel M4: the kernel "
        "is transport-free)"
    )
    assert '"httpx' in sdk_text, (
        "noeta-sdk must declare the httpx dependency its capability impls use"
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
