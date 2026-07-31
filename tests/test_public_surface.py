"""Public-surface completeness contract for host builders.

A host binds only to ``noeta.sdk`` / ``noeta.presets``, so everything it
legitimately needs must be reachable there — a missing symbol is a hole that
pushes the host onto a runtime internal. Three things are pinned:

1. **Pinned surface** — the paths and symbols a host binds to are importable
   from ``noeta.sdk`` / ``noeta.sdk.storage`` (the data list below is the
   contract; extending it is how a host need gets blessed).
2. **Zero-logic re-export** — ``noeta.sdk.storage`` hands back (lazily,
   PEP 562) the very objects the ``storage`` built-in's backend packages
   (``noeta.builtins.storage.impl.{sqlite,postgres}``) define, so the blessed
   path cannot drift into a parallel implementation.
3. **Live proof** — every ``noeta.*`` import in the host-contract examples (the
   reference host and the first-party plugins, this repo's stand-in for a
   product) lands on the public surface. A gap shows up here as a failing test
   rather than as an internal import in someone's product.
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]

#: The host-contract examples: the reference host and the first-party plugins.
#: These are written against the public surface on purpose and stand in for a
#: product host. The rest of ``examples/`` are runtime-level demos that
#: deliberately show internals (composer, policies, in-memory stores) and are
#: out of scope here.
HOST_EXAMPLE_ROOTS = (
    REPO_ROOT / "examples" / "reference-host",
    REPO_ROOT / "examples" / "plugins",
)

#: Public roots. A module is public if it equals one of these or is a submodule
#: (so ``noeta.sdk.storage`` / ``noeta.sdk.testing`` / ``noeta.presets.*`` are
#: all covered).
PUBLIC_ROOTS = ("noeta.sdk", "noeta.presets")

#: The host contract: public path -> the symbols a host binds to there. A host
#: need that is missing here is closed by a **re-export**, never by blessing an
#: internal path.
HOST_CONTRACT: dict[str, tuple[str, ...]] = {
    # Durable storage: the single public doorway — the stack builders + path
    # predicates it defines itself, and the 12 lazy adapter re-exports
    # (6 sqlite + 6 postgres).
    "noeta.sdk.storage": (
        # The four functions the doorway defines itself.
        "build_storage_stack",
        "open_storage_stack",
        "is_memory_path",
        "is_postgres_url",
        # The 12 lazy class re-exports (PEP 562).
        "SqliteContentStore",
        "SqliteDispatcher",
        "SqliteEventLog",
        "SqliteReadOnlyError",
        "SqliteReadOnlyStore",
        "SqliteSchemaVersionError",
        "PostgresContentStore",
        "PostgresDispatcher",
        "PostgresEventLog",
        "PostgresReadOnlyError",
        "PostgresReadOnlyStore",
        "PostgresSchemaVersionError",
    ),
    "noeta.sdk": (
        # Assembly
        "Client",
        "HostConfig",
        "Options",
        "AgentDefinition",
        "compile_options",
        # Tool + MCP authoring
        "tool",
        "create_sdk_mcp_server",
        # Hooks — a Guard dispatches on the ProposedAction members, so the
        # union alone is not enough to write one.
        "Guard",
        "GuardContext",
        "ProposedAction",
        "ProposedToolCall",
        "ProposedSpawnSubtask",
        "ProposedFinish",
        "VerdictResult",
        "Observer",
        "ContentKindSpec",
        # Streaming + the wire projection a product serves to its clients
        "StreamDelta",
        "envelope_to_dict",
        # Execution-environment seam (the abstract types; the concrete AIO
        # adapters stay internal on purpose — execution-environment-seam ADR)
        "ExecEnv",
        "BrowserBackend",
        "SandboxExecEnvConfig",
        # Plugins (manifest mechanism: the loader / set a host binds to, the
        # builder an authored plugin uses, plus the trust-store + error surface)
        "PluginError",
        "load_plugins",
        "PluginSet",
        "PluginBuilder",
        "grant_trust",
        "is_trusted",
        # Workspace helpers a path guard needs
        "path_within",
    ),
    # Official factory content a host may start from.
    "noeta.presets": ("main_options",),
    # Test doubles a host's own suite runs on.
    "noeta.sdk.testing": ("FakeLLMProvider",),
}


# ---------------------------------------------------------------------------
# 1. The pinned surface
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", sorted(HOST_CONTRACT))
def test_contract_path_exports_its_symbols(path: str) -> None:
    """Every path in the contract imports and exports every symbol named."""
    module = importlib.import_module(path)
    missing = [s for s in HOST_CONTRACT[path] if not hasattr(module, s)]
    assert not missing, (
        f"{path} no longer exports {missing} — a host binds to these; "
        f"re-export them (never point a host at an internal path)"
    )


def test_sdk_exports_are_declared_in_all() -> None:
    """``noeta.sdk``'s contract symbols are declared, not accidental."""
    import noeta.sdk as sdk

    declared = set(sdk.__all__)
    undeclared = [s for s in HOST_CONTRACT["noeta.sdk"] if s not in declared]
    assert not undeclared, (
        f"{undeclared} are importable from noeta.sdk but missing from __all__ — "
        f"the public surface is what __all__ says it is"
    )


# ---------------------------------------------------------------------------
# 2. The storage bless is a live, zero-logic re-export
# ---------------------------------------------------------------------------


def test_storage_bless_is_a_live_zero_logic_reexport() -> None:
    """``noeta.sdk.storage`` re-exports the backend objects themselves.

    Identity, not equality: the blessed path must be the same class the
    ``storage`` built-in's backend packages define, so the contract cannot
    pass on a stale copy. The stack builders / path predicates are the
    doorway's own functions, not re-exports, so only the adapter classes are
    checked here.
    """
    import noeta.sdk.storage as public

    internal_by_prefix = {
        "Sqlite": importlib.import_module("noeta.builtins.storage.impl.sqlite"),
        "Postgres": importlib.import_module("noeta.builtins.storage.impl.postgres"),
    }
    for name in HOST_CONTRACT["noeta.sdk.storage"]:
        prefix = next((p for p in internal_by_prefix if name.startswith(p)), None)
        if prefix is None:
            continue  # a doorway-defined function, not a re-export
        internal = internal_by_prefix[prefix]
        assert getattr(public, name) is getattr(internal, name), (
            f"noeta.sdk.storage.{name} is not the same object as "
            f"{internal.__name__}.{name} — the re-export must be zero-logic"
        )


def test_storage_doorway_all_matches_the_contract() -> None:
    """``noeta.sdk.storage.__all__`` is exactly the pinned surface — the
    4 stack functions + the 12 lazy adapter re-exports, nothing else."""
    import noeta.sdk.storage as public

    assert set(public.__all__) == set(HOST_CONTRACT["noeta.sdk.storage"])


# ---------------------------------------------------------------------------
# 3. The examples prove a host can live on the public surface
# ---------------------------------------------------------------------------


def _iter_example_files():
    for root in HOST_EXAMPLE_ROOTS:
        for path in sorted(root.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            yield path


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
            if node.level == 0 and node.module and (
                node.module == "noeta" or node.module.startswith("noeta.")
            ):
                out.add(node.module)
    return out


def _is_public(module: str) -> bool:
    return module == "noeta" or any(
        module == root or module.startswith(root + ".") for root in PUBLIC_ROOTS
    )


def test_examples_exist() -> None:
    """Guard: the scan must have something to scan."""
    assert list(_iter_example_files()), HOST_EXAMPLE_ROOTS


def test_examples_import_only_the_public_surface() -> None:
    """The reference host and every first-party plugin stay on ``noeta.sdk``.

    They are this repo's stand-in for a product host, so a runtime-internal
    import here is the contract gap a real host would hit — close it with a
    re-export and add the symbol to ``HOST_CONTRACT``.
    """
    violations = [
        f"{path.relative_to(REPO_ROOT)}: {module}"
        for path in _iter_example_files()
        for module in sorted(_imported_noeta_modules(path))
        if not _is_public(module)
    ]
    assert not violations, (
        "an example reaches a runtime internal — bless a re-export through "
        "noeta.sdk (never the internal path):\n  " + "\n  ".join(violations)
    )
