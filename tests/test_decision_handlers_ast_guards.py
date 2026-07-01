"""AST regression tests pinning the decision-handler design contract.

The handler module deliberately cannot reach back to Engine, raw
EventLog, or HookManager. These checks are static — they look at
the parsed module text rather than trying to construct contrived
runtime calls — so a future refactor that re-introduces the coupling
fails CI immediately.

Contract:

* No ``import noeta.core.engine`` / ``from noeta.protocols.event_log
  import …`` / ``from noeta.runtime.tool import …`` in the handler
  module (the typed ``ToolInvoker`` Protocol is defined locally
  instead).
* No function parameter named ``event_log`` outside ``HandlerContext``
  field definitions.
* No ``engine.*`` / ``self.*`` access patterns (handler module is
  free functions; there is no ``self``).
* No ``*.system_emit(...)`` call (only the narrow
  ``ctx.create_child_task`` cross-stream surface is allowed).
"""

from __future__ import annotations

import ast
from pathlib import Path


import noeta.core._decision_handlers as _decision_handlers_mod

# Task #12: derive the source path from the imported module (now in noeta-runtime).
_HANDLER_MODULE = Path(_decision_handlers_mod.__file__)


def _tree() -> ast.AST:
    return ast.parse(_HANDLER_MODULE.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Forbidden imports
# ---------------------------------------------------------------------------


_FORBIDDEN_IMPORT_PREFIXES = (
    "noeta.core.engine",
    "noeta.protocols.event_log",
    "noeta.runtime.tool",
)


def test_handler_module_does_not_import_engine_eventlog_or_toolruntime() -> None:
    """C3 acceptance: handler module imports only L0 protocols + canonical
    types; specifically does NOT import Engine, EventLog protocols, or
    ToolRuntime."""
    bad: list[str] = []
    for node in ast.walk(_tree()):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if any(
                    alias.name == p or alias.name.startswith(p + ".")
                    for p in _FORBIDDEN_IMPORT_PREFIXES
                ):
                    bad.append(f"import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if any(
                mod == p or mod.startswith(p + ".")
                for p in _FORBIDDEN_IMPORT_PREFIXES
            ):
                bad.append(f"from {mod} import ...")
    assert not bad, (
        "handler module must not import Engine / EventLog Protocols / "
        f"ToolRuntime; found: {bad}"
    )


# ---------------------------------------------------------------------------
# Forbidden parameter name
# ---------------------------------------------------------------------------


def test_handler_module_has_no_event_log_param_outside_handler_context() -> None:
    """C3 acceptance: no function parameter named ``event_log`` — handlers
    reach the log via ``ctx.emit`` only. The ``HandlerContext`` field
    definitions are class-level (dataclass annotations), not function
    args, so they aren't caught here."""
    offenders: list[str] = []
    for node in ast.walk(_tree()):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for arg in (
                node.args.posonlyargs
                + node.args.args
                + node.args.kwonlyargs
            ):
                if arg.arg == "event_log":
                    offenders.append(f"{node.name}({arg.arg})")
    assert not offenders, (
        f"handler module must not take ``event_log`` as a parameter; "
        f"found: {offenders}"
    )


# ---------------------------------------------------------------------------
# Forbidden access patterns
# ---------------------------------------------------------------------------


def test_handler_module_has_no_engine_attribute_access() -> None:
    """C3 acceptance: handler module never receives an Engine
    instance; no ``engine.foo`` attribute access should appear."""
    bad: list[str] = []
    for node in ast.walk(_tree()):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "engine"
        ):
            bad.append(f"engine.{node.attr}")
    assert not bad, (
        f"handler module must not access ``engine.*``; found: {bad}"
    )


def test_handler_module_has_no_self_attribute_access() -> None:
    """C3 acceptance: handler module is free functions (no classes
    with methods); no ``self.foo`` access pattern should appear."""
    bad: list[str] = []
    for node in ast.walk(_tree()):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "self"
        ):
            bad.append(f"self.{node.attr}")
    assert not bad, (
        f"handler module must not access ``self.*``; found: {bad}"
    )


# ---------------------------------------------------------------------------
# Forbidden call patterns
# ---------------------------------------------------------------------------


def test_handler_module_has_no_system_emit_call() -> None:
    """C3 acceptance: only the narrow ``ctx.create_child_task`` is
    allowed for cross-stream writes; any ``*.system_emit(...)`` call
    means the handler is reaching back to a raw EventLog."""
    bad: list[str] = []
    for node in ast.walk(_tree()):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "system_emit"
        ):
            bad.append(ast.unparse(node.func))
    assert not bad, (
        "handler module must not call ``*.system_emit(...)``; the "
        "only allowed cross-stream surface is "
        f"``ctx.create_child_task(...)``; found: {bad}"
    )


# ---------------------------------------------------------------------------
# Sanity: dispatch_exit raises NotImplementedError with the byte-equal
# message ``"Unknown decision type: <name>"`` per the C3 design contract.
# ---------------------------------------------------------------------------


def test_unknown_decision_raises_byte_equal_message() -> None:
    """rev4 byte-equal pin: any unmapped Decision must raise
    ``NotImplementedError`` with the exact message string. This
    matches Engine's pre-refactor ``engine.py:405-406`` behaviour."""
    from dataclasses import dataclass

    import pytest

    from noeta.core._decision_handlers import (
        HandlerContext,
        dispatch_exit,
    )
    from noeta.protocols.task import Task, TaskState

    @dataclass(frozen=True, slots=True)
    class _NotADecisionAnyoneHandles:
        pass

    # Minimal HandlerContext stub — exit dispatch raises before
    # touching any of the callable fields, so we only need an
    # object that satisfies the dataclass shape. type:ignore is
    # avoided by passing the required fields' simplest valid values.
    def _emit(**_: object) -> object:
        raise AssertionError("emit must not be called for unmapped decision")

    def _noop(*_: object, **__: object) -> object:
        raise AssertionError("ctx callable must not be called for unmapped decision")

    ctx = HandlerContext(
        emit=_emit,  # type: ignore[arg-type]
        create_child_task=_noop,  # type: ignore[arg-type]
        apply_event=_noop,  # type: ignore[arg-type]
        guard=_noop,  # type: ignore[arg-type]
        write_snapshot=_noop,  # type: ignore[arg-type]
        resolve_tool=_noop,  # type: ignore[arg-type]
        tool_invoker=None,
        content_store=None,  # type: ignore[arg-type]
        id_factory=lambda: "x",
        clock=lambda: 0.0,
        actor="engine",
    )
    task = Task(task_id="t", state=TaskState())

    with pytest.raises(NotImplementedError, match=r"^Unknown decision type: ") as exc:
        dispatch_exit(
            ctx,
            task,
            _NotADecisionAnyoneHandles(),  # type: ignore[arg-type]
            lease_id="lease",
            trace_id="trace",
        )
    # Pin the exact message byte-equal (engine.py:405-406 pre-refactor)
    assert str(exc.value) == (
        "Unknown decision type: _NotADecisionAnyoneHandles"
    )
