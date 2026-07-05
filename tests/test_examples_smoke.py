"""Smoke tests for the SDK-facing examples in ``examples/`` (issue 09).

Each example under ``examples/`` (the task-oriented SDK examples — NOT the
contributor-facing kernel demos under ``examples/_internal/``, which have
their own coverage in ``tests/test_examples_demo.py`` and the CI phase0
run) gets a smoke test here: import the module and drive its minimal path
with the offline :class:`FakeLLMProvider` the example ships with. The
point is rot-detection — if the SDK public surface drifts and an example
stops importing or stops reaching a terminal answer, CI goes red before a
user hits it.

The examples are parametrised by module name so adding a new
``examples/<name>.py`` with a top-level ``run(...)`` automatically widens
coverage once it is listed in ``_SMOKE_EXAMPLES``.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


_EXAMPLES_DIR = Path(__file__).resolve().parents[1] / "examples"

#: SDK examples that expose a ``run(*, workspace_dir=..., ...)`` entrypoint
#: and ship an offline default provider. ``_internal/`` demos are excluded
#: by design (they are real-provider gates / kernel walkthroughs).
_SMOKE_EXAMPLES = (
    "minimal_agent",
    "custom_tool",
    "swap_provider",
    "spawn_subtask",
    "sdk_minimal",
    "mcp_server",
    "permission_gate",
)


def _load_example(name: str):
    path = _EXAMPLES_DIR / f"{name}.py"
    assert path.exists(), f"missing example {path}"
    spec = importlib.util.spec_from_file_location(f"_example_{name}", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("name", _SMOKE_EXAMPLES)
def test_example_imports_and_documents_capability(name: str) -> None:
    """Every SDK example imports cleanly and its docstring states which
    SDK capability it demonstrates (acceptance criterion: top docstring
    names the demoed capability)."""
    mod = _load_example(name)
    doc = mod.__doc__ or ""
    assert "Demonstrated SDK capability" in doc, (
        f"{name}.py docstring must declare the demonstrated SDK capability"
    )
    assert hasattr(mod, "run"), f"{name}.py must expose a run() entrypoint"


def test_minimal_agent_returns_answer(tmp_path: Path) -> None:
    mod = _load_example("minimal_agent")
    answer = mod.run(workspace_dir=tmp_path)
    assert isinstance(answer, str) and answer.strip(), answer


def test_custom_tool_actually_runs_the_closure(tmp_path: Path) -> None:
    mod = _load_example("custom_tool")
    called = mod.run(workspace_dir=tmp_path)
    assert "word_count" in called, called


def test_swap_provider_keeps_recipe_identity_stable(tmp_path: Path) -> None:
    mod = _load_example("swap_provider")
    answer_a, answer_b, identity_equal = mod.run(workspace_dir=tmp_path)
    assert answer_a and answer_b
    assert answer_a != answer_b, "the two providers should answer differently"
    assert identity_equal, (
        "provider neutrality: the compiled agent identity must not depend on "
        "which provider is wired in"
    )


def test_spawn_subtask_spawns_a_distinct_child(tmp_path: Path) -> None:
    mod = _load_example("spawn_subtask")
    parent_id, child_id = mod.run(workspace_dir=tmp_path)
    assert parent_id and child_id, (parent_id, child_id)
    assert parent_id != child_id, "child Task must be a distinct stream"


def test_mcp_server_bundled_tool_actually_runs(tmp_path: Path) -> None:
    mod = _load_example("mcp_server")
    called = mod.run(workspace_dir=tmp_path)
    assert "echo" in called, called


def test_permission_gate_denies_the_write(tmp_path: Path) -> None:
    mod = _load_example("permission_gate")
    approved, resolver, wrote_file = mod.run(workspace_dir=tmp_path)
    assert approved is False, approved
    assert resolver == "can_use_tool", resolver
    assert wrote_file is False, "denied write must never run"


def test_main_entrypoints_exit_zero() -> None:
    """Each example's ``main()`` runs end-to-end and returns 0 — this is
    the path a user gets from ``python examples/<name>.py``."""
    for name in _SMOKE_EXAMPLES:
        mod = _load_example(name)
        assert mod.main() == 0, f"{name}.main() did not return 0"


def test_crash_resume_survives_sigkill(tmp_path: Path) -> None:
    """The crash-resume demo is orchestration (two processes + SIGKILL),
    so it runs as a subprocess rather than through ``_SMOKE_EXAMPLES``.
    Slowest smoke here (~8s: a real wait_timer must come due)."""
    import subprocess
    import sys

    proc = subprocess.run(
        [
            sys.executable,
            str(_EXAMPLES_DIR / "crash_resume.py"),
            "--db",
            str(tmp_path / "demo.sqlite"),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "kill -9" in proc.stdout
    assert "task completed: 'Weekly report ready.'" in proc.stdout
