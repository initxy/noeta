"""**determinism** guard for orchestration scripts (controlled namespace + AST static check).

The goal is "determinism", not "security" (red line): the model writes
the script and already holds shell/file tools, so ``exec``-ing the model's Python **adds no new attack surface** — we
don't need a security sandbox, we need "same input resumes the same": D3's "rerun from scratch"
requires the script to have no non-deterministic source
(``time`` / ``random`` / ``datetime`` / external IO).

Two gates:

* :func:`check_workflow_script` — **startup-time (translation-time) AST static check**. Runs *before*
  ``run_workflow`` translates into ``SpawnSubtaskDecision``: rejects syntax errors, any ``import``,
  non-deterministic builtins / external IO, and reflection-escape attributes, pointing the error at the
  offending line. A violation yields a recoverable receipt (the model can fix and retry) and **produces no
  half-run subtask** (red line).
* :data:`SAFE_BUILTINS` — controlled builtin allowlist for the exec namespace. Injects only the orchestration API
  (``agent`` / ``log`` / ``args``) plus a safe builtin set with no ``import`` / ``open`` / ``eval`` / ``exec`` /
  ``__import__``. Even if the AST check misses something, runtime can't reach ``time`` / ``random`` / ``os``.

This module depends only on the stdlib ``ast`` / ``builtins``, so both ``_control_translate`` (translation time) and
``orchestration`` (exec time) can import it without a cycle.
"""

from __future__ import annotations

import ast
import builtins as _builtins
from typing import Optional


__all__ = ["check_workflow_script", "SAFE_BUILTINS"]


#: Builtins banned outright (non-deterministic / dynamic execution / external IO / sandbox-escape entry points).
#: The dynamic import form ``__import__`` is absent here only because it starts with ``_`` and is already blocked by
#: :data:`SAFE_BUILTINS`'s prefix filter; this list holds the visible names that show up in an AST ``Name``.
_FORBIDDEN_BUILTINS = frozenset(
    {
        "open",
        "eval",
        "exec",
        "compile",
        "input",
        "breakpoint",
        "globals",
        "locals",
        "vars",
        "getattr",
        "setattr",
        "delattr",
        "print",  # progress goes through log(), no direct IO
    }
)

#: Module names that are non-deterministic / IO sources. Scripts shouldn't import them at all (all imports are
#: banned); this also blocks "bare references" (e.g. a script writes ``time.time()`` without importing — that would
#: be a NameError, but we give a clear error pointing at the offending node at AST time rather than at runtime). A
#: name counts as a violation only when it is **not assigned in the script** (so a script using ``os`` as a plain
#: variable name isn't wrongly rejected).
_NONDETERMINISTIC_MODULES = frozenset(
    {
        "time",
        "random",
        "datetime",
        "os",
        "sys",
        "subprocess",
        "socket",
        "secrets",
        "uuid",
        "asyncio",
        "threading",
        "multiprocessing",
        "urllib",
        "requests",
        "http",
        "pathlib",
        "io",
    }
)

#: Attributes commonly used for reflection / sandbox escape; static attribute access is banned (e.g. ``obj.__globals__``).
_FORBIDDEN_ATTRS = frozenset(
    {
        "__globals__",
        "__builtins__",
        "__import__",
        "__class__",
        "__bases__",
        "__subclasses__",
        "__mro__",
        "__code__",
        "__closure__",
        "__dict__",
        "__getattribute__",
        "__loader__",
        "__spec__",
        "__module__",
    }
)


def check_workflow_script(script: str) -> Optional[str]:
    """Run a determinism AST static check on an orchestration script.

    Returns ``None`` if compliant; otherwise a human-readable message **pointing at the offending line** (for the
    model to fix and retry). Called at ``run_workflow`` translation time: a violation is rejected and no subtask is
    spawned.
    """
    try:
        tree = ast.parse(script, filename="<workflow>", mode="exec")
    except SyntaxError as exc:
        loc = f"line {exc.lineno}" if exc.lineno else "unknown line"
        return f"workflow script syntax error at {loc}: {exc.msg}"

    # First collect names assigned (Store) in the script, to let through the "module name used as a plain variable" case.
    assigned: set[str] = {
        node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)
    }
    # Function/lambda parameters are ast.arg, not ast.Name(Store), so collect them separately; otherwise a legitimate
    # script like ``def f(io): ...`` that uses a module name as a parameter would be wrongly rejected.
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            a = node.args
            for arg in (*a.posonlyargs, *a.args, *a.kwonlyargs):
                assigned.add(arg.arg)
            if a.vararg is not None:
                assigned.add(a.vararg.arg)
            if a.kwarg is not None:
                assigned.add(a.kwarg.arg)

    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            return (
                f"workflow script forbids 'import' (line {node.lineno}): "
                "scripts must be deterministic — no imports, and no "
                "time/random/datetime/os/IO. Use the injected agent()/log()/args."
            )
        if isinstance(node, ast.Attribute) and node.attr in _FORBIDDEN_ATTRS:
            return (
                f"workflow script forbids attribute {node.attr!r} "
                f"(line {node.lineno}): reflection / sandbox-escape not allowed."
            )
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            if node.id.startswith("__") and node.id.endswith("__"):
                # dunder names (``__import__`` / ``__builtins__`` / …) are
                # reflection / import escape vectors — never legitimate in a
                # deterministic orchestration script.
                return (
                    f"workflow script forbids {node.id!r} (line {node.lineno}): "
                    "dunder access (reflection / dynamic import) not allowed."
                )
            if node.id in _FORBIDDEN_BUILTINS:
                return (
                    f"workflow script forbids {node.id!r} (line {node.lineno}): "
                    "non-deterministic / IO builtin is unavailable."
                )
            if node.id in _NONDETERMINISTIC_MODULES and node.id not in assigned:
                return (
                    f"workflow script forbids {node.id!r} (line {node.lineno}): "
                    "non-deterministic / IO source. Workflow scripts must be "
                    "reproducible (no time/random/datetime/os/network)."
                )
    return None


def _build_safe_builtins() -> dict[str, object]:
    """Controlled builtin allowlist for the orchestration script's exec namespace.

    Takes every name in ``builtins`` that doesn't start with ``_`` (auto-blocks ``__import__``) and isn't in the
    :data:`_FORBIDDEN_BUILTINS` blacklist, plus ``__build_class__`` needed for ``class`` definitions. The result holds
    only deterministic, IO-free builtins (``len`` / ``range`` / ``sorted`` / ``dict`` / ``str`` / what f-strings need,
    etc.) — it keeps normal scripts working while denying ``import`` / ``open`` / ``eval``.
    """
    safe: dict[str, object] = {
        name: getattr(_builtins, name)
        for name in dir(_builtins)
        if not name.startswith("_") and name not in _FORBIDDEN_BUILTINS
    }
    # Needed by class definitions; it is deterministic itself (only assembles the class object).
    safe["__build_class__"] = _builtins.__build_class__
    return safe


#: The ``__builtins__`` injected into the orchestration script's exec namespace (see ``orchestration._run_script``).
SAFE_BUILTINS: dict[str, object] = _build_safe_builtins()
