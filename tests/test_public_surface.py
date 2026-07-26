"""Public-surface completeness contract for the ``apps/noeta-agent`` product.

The repo-split spec (``docs/implementation-specs/2026-07-26-sdk-only-repo-split.md``,
decision D4) makes this repo SDK-only and moves the agent product to its own
repository, where import-linter will forbid it from reaching runtime internals
directly. This test is the *here* half of that gate: it re-scans every
``noeta.*`` import in ``apps/noeta-agent`` and asserts each one is either

* the app's own package (``noeta.agent`` — moves with the app),
* already on the public surface (``noeta.sdk`` / ``noeta.presets`` and their
  submodules),
* an internal runtime module with a **documented public equivalent** (the
  ``INTERNAL_TO_PUBLIC`` mapping below — the contract Phase 2's import rewrite
  consumes), or
* an ADR-documented deliberate exemption (the two sandbox-adapter modules that
  extend the runtime-internal concrete AIO adapters, kept OFF the public
  surface on purpose).

The test suite of the app (``apps/noeta-agent/tests``) is deliberately OUT of
the import-linter source scope (``.importlinter`` lists only the app package —
``host`` / ``api`` / ``main`` / ``store`` / ``services`` / ...), so its
white-box / test-double reach into runtime internals is recorded in
``TEST_ONLY_INTERNAL`` rather than blessed onto the public surface.

The mapping is data, not prose, so Phase 2 can consume it mechanically.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

# --------------------------------------------------------------------------- #
# Locations
# --------------------------------------------------------------------------- #

REPO_ROOT = Path(__file__).resolve().parents[1]
APP_ROOT = REPO_ROOT / "apps" / "noeta-agent"
#: The importable product package (what the import-linter contracts govern and
#: what Phase 2 rewrites); everything else under ``APP_ROOT`` is the test suite.
APP_PACKAGE_ROOT = APP_ROOT / "noeta"

# --------------------------------------------------------------------------- #
# The contract data
# --------------------------------------------------------------------------- #

#: The app's own package. Moves to the new repo with the app; never part of the
#: SDK contract.
SELF_ROOT = "noeta.agent"

#: Public roots. A module is public if it equals one of these or is a submodule
#: (so ``noeta.sdk.testing`` / ``noeta.sdk.providers`` / ``noeta.sdk.storage`` /
#: ``noeta.sdk.authoring`` and ``noeta.presets.*`` are all covered).
PUBLIC_ROOTS = (
    "noeta.sdk",
    "noeta.presets",
)

#: internal runtime module -> the public path that re-exports what the app uses.
#: This is the load-bearing half of the contract: each app-source internal
#: import is rewritten to its public equivalent here in Phase 2. Keep it exact —
#: bless only what the app source actually imports.
INTERNAL_TO_PUBLIC = {
    # The sqlite storage triple the host builds for durable storage
    # (SqliteContentStore / SqliteDispatcher / SqliteEventLog). Blessed in this
    # milestone as ``noeta.sdk.storage``.
    "noeta.storage.sqlite": "noeta.sdk.storage",
}

#: Deliberate, ADR-documented exemptions (importer module -> internal modules it
#: may reach directly). The two official-SDK sandbox adapter modules extend the
#: runtime-internal concrete AIO adapters, which are kept OFF the ``noeta.sdk``
#: public surface on purpose — publishing retirement-slated implementation would
#: freeze it into the user-facing API. See the execution-environment-seam ADR
#: ("SDK-adapter export surface") and ``.importlinter`` (contract
#: ``app-uses-only-sdk`` ``ignore_imports``). Scoped to exactly these pairs so
#: the exemption keeps its meaning.
DELIBERATE_EXEMPTIONS = {
    "noeta.agent.host.sdk_sandbox_exec_env": {"noeta.tools.fs.exec_env"},
    "noeta.agent.host.sdk_browser_backend": {"noeta.tools.browser"},
}

#: Test-suite-only internal imports. The app test suite is out of the
#: import-linter source scope, so tests keep white-box / test-double access to
#: runtime internals. These are NOT on the public surface and are NOT blessed —
#: they are recorded so the scan stays exhaustive and any *new* internal reach
#: from a test shows up here for review. A note records where a public
#: equivalent already exists (a test may adopt it, but is not required to).
TEST_ONLY_INTERNAL = {
    # EventEnvelope built white-box for a fake stream; the type is not on the
    # public surface (only ``envelope_to_dict`` is, on ``noeta.sdk``).
    "noeta.protocols.events": None,
    # SubtaskResult, constructed to drive a subtask-suspend path.
    "noeta.protocols.wake": None,
    # LLM message value types; the symbols the tests use (LLMResponse /
    # TextBlock / Usage / StreamDelta / ToolUseBlock) are all re-exported on
    # ``noeta.sdk``.
    "noeta.protocols.messages": "noeta.sdk",
    # FakeLLMProvider is public via ``noeta.sdk.testing``;
    # FakeStreamingLLMProvider is NOT currently re-exported (test-only).
    "noeta.testing.fake_llm": "noeta.sdk.testing",
    # derive_compaction_config — a compaction-config helper used to assert
    # routing behaviour; runtime-internal.
    "noeta.execution.builder": None,
    # CATALOG — public via ``noeta.sdk.providers``.
    "noeta.providers.catalog": "noeta.sdk.providers",
    # White-box access to the concrete AIO browser backend for the adapter
    # tests (the adapter itself is a deliberate source-side exemption above).
    "noeta.tools.browser._backend": None,
    # White-box access to the concrete AIO exec-env for the adapter tests.
    "noeta.tools.fs.exec_env": None,
}


# --------------------------------------------------------------------------- #
# Scanner
# --------------------------------------------------------------------------- #


def _iter_py_files(root: Path):
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        yield path


def _module_of_file(path: Path) -> str | None:
    """Dotted module name of a file inside the app package, else ``None``.

    Files under ``apps/noeta-agent/noeta/`` are the importable product package;
    everything else (the test suite) has no ``noeta.*`` module identity.
    """
    try:
        rel = path.relative_to(APP_PACKAGE_ROOT)
    except ValueError:
        return None
    parts = list(rel.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(["noeta", *parts])


def _imported_noeta_modules(path: Path) -> set[str]:
    """Every ``noeta.*`` module a file imports (module targets, not symbols)."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "noeta" or alias.name.startswith("noeta."):
                    out.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            # Relative imports (level > 0) are within-package (self); skip.
            if node.level == 0 and node.module and (
                node.module == "noeta" or node.module.startswith("noeta.")
            ):
                out.add(node.module)
    return out


def _is_public(module: str) -> bool:
    return module == "noeta" or any(
        module == root or module.startswith(root + ".") for root in PUBLIC_ROOTS
    )


def _is_self(module: str) -> bool:
    return module == SELF_ROOT or module.startswith(SELF_ROOT + ".")


def _scan(root: Path):
    """Yield ``(file_path, importer_module_or_None, imported_module)`` tuples."""
    for path in _iter_py_files(root):
        importer = _module_of_file(path)
        for module in sorted(_imported_noeta_modules(path)):
            yield path, importer, module


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #


def test_app_root_exists():
    """Guard: the audit target must exist (the scan asserting nothing is a bug)."""
    assert APP_PACKAGE_ROOT.is_dir(), APP_PACKAGE_ROOT


def test_app_source_imports_only_public_or_mapped_or_exempt():
    """Every ``noeta.*`` import in the app *source package* is on the contract.

    This is the load-bearing assertion: it mirrors the import-linter source
    scope, so a green result here means Phase 2 can rewrite every source-side
    internal import to its public equivalent (the ``INTERNAL_TO_PUBLIC`` map)
    with the only remaining direct-reach being the two ADR-documented
    sandbox-adapter exemptions.
    """
    violations: list[str] = []
    for path, importer, module in _scan(APP_PACKAGE_ROOT):
        if _is_self(module) or _is_public(module):
            continue
        if module in INTERNAL_TO_PUBLIC:
            continue
        if importer in DELIBERATE_EXEMPTIONS and module in DELIBERATE_EXEMPTIONS[importer]:
            continue
        violations.append(f"{path.relative_to(REPO_ROOT)}: {importer} -> {module}")

    assert not violations, (
        "app source reaches runtime internals with no public equivalent — bless "
        "a re-export through noeta.sdk (never the internal path) and add it to "
        "INTERNAL_TO_PUBLIC:\n  " + "\n  ".join(violations)
    )


def test_app_test_suite_internal_imports_are_documented():
    """Every internal ``noeta.*`` import in the app *test suite* is accounted for.

    The test suite is out of the import-linter source scope, so it may keep
    internal reach — but each such module must be a known public path or a
    documented ``TEST_ONLY_INTERNAL`` entry, so a *new* internal reach surfaces
    for review rather than sliding in silently.
    """
    undocumented: list[str] = []
    tests_root = APP_ROOT / "tests"
    for path, _importer, module in _scan(tests_root):
        if _is_self(module) or _is_public(module):
            continue
        if module in INTERNAL_TO_PUBLIC or module in TEST_ONLY_INTERNAL:
            continue
        undocumented.append(f"{path.relative_to(REPO_ROOT)}: {module}")

    assert not undocumented, (
        "app test suite reaches a runtime internal not recorded in "
        "TEST_ONLY_INTERNAL (nor public/mapped); document it (or bless a public "
        "equivalent if the app source needs it too):\n  " + "\n  ".join(undocumented)
    )


def test_storage_bless_is_a_live_zero_logic_reexport():
    """``noeta.sdk.storage`` re-exports the exact objects the app imports.

    Pins the mapping as real (importable) and zero-logic (identity with the
    internal source), so the contract cannot pass on a stale name.
    """
    import noeta.sdk.storage as public
    import noeta.storage.sqlite as internal

    for name in ("SqliteContentStore", "SqliteDispatcher", "SqliteEventLog"):
        assert hasattr(public, name), f"noeta.sdk.storage missing {name}"
        assert getattr(public, name) is getattr(internal, name), (
            f"noeta.sdk.storage.{name} is not the same object as "
            f"noeta.storage.sqlite.{name} — the re-export must be zero-logic"
        )


@pytest.mark.parametrize("public_path", sorted(set(INTERNAL_TO_PUBLIC.values())))
def test_mapped_public_paths_import(public_path):
    """Every public path the mapping names is importable."""
    __import__(public_path)


def test_deliberate_exemptions_match_importlinter():
    """The exemption set here matches the ``.importlinter`` ``ignore_imports``.

    Keeps the two records from drifting: adding/removing an exemption must touch
    both this contract and the ratchet.
    """
    text = (REPO_ROOT / ".importlinter").read_text(encoding="utf-8")
    expected = {
        f"{importer} -> {module}"
        for importer, modules in DELIBERATE_EXEMPTIONS.items()
        for module in modules
    }
    for line in expected:
        assert line in text, (
            f"exemption {line!r} is declared here but not in .importlinter "
            f"(contract app-uses-only-sdk ignore_imports)"
        )


def test_app_source_has_exactly_one_mapped_and_two_exempt():
    """The audit's shape is pinned: one blessed mapping + two exemptions.

    A regression here (a new internal reach in the source) is exactly what the
    split must not ship silently; this makes the expected surface explicit.
    """
    mapped: set[str] = set()
    exempt: set[tuple[str, str]] = set()
    for _path, importer, module in _scan(APP_PACKAGE_ROOT):
        if module in INTERNAL_TO_PUBLIC:
            mapped.add(module)
        elif importer in DELIBERATE_EXEMPTIONS and module in DELIBERATE_EXEMPTIONS[importer]:
            exempt.add((importer, module))

    assert mapped == {"noeta.storage.sqlite"}, mapped
    assert exempt == {
        ("noeta.agent.host.sdk_sandbox_exec_env", "noeta.tools.fs.exec_env"),
        ("noeta.agent.host.sdk_browser_backend", "noeta.tools.browser"),
    }, exempt
