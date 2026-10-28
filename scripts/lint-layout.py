#!/usr/bin/env python3
"""Layout lint: code sits in the package that matches what it is.

``lint-imports`` checks which way imports go; this checks where code lands
(CONTEXT.md: Where code goes):

* ``noeta/`` holds only the known top-level packages;
* every module of ``noeta/core`` is used by the kernel itself (imported by
  another ``noeta/core`` module) or is kernel surface listed below with its
  reason. Code only plugins share belongs in ``noeta/toolkit``.

Exit code:
    0 — clean
    1 — at least one violation
    2 — invocation error

Usage::

    python scripts/lint-layout.py [ROOT]
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

TOP_LEVEL = {
    "__init__.py", "py.typed",
    "core", "toolkit", "plugins", "backends", "host", "agents", "cli", "testing",
}

#: Kernel modules no other kernel module imports, and why they belong to it.
KERNEL_SURFACE = {
    "noeta.core.commands": "front ends list and expand the slash commands plugins offer",
    "noeta.core.questions": "the question form plugins ask with and front ends answer with",
}


def module_name(root: Path, path: Path) -> str:
    parts = list(path.relative_to(root).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def imported(root: Path, path: Path, modules: set[str]) -> set[str]:
    """The modules of ``modules`` that ``path`` imports."""
    here = module_name(root, path)
    package = here if path.name == "__init__.py" else here.rpartition(".")[0]
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text("utf-8"), str(path))):
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                up = package.split(".")[: len(package.split(".")) - node.level + 1]
                base = ".".join([*up, base] if base else up)
            names = [base, *(f"{base}.{a.name}" for a in node.names)]
        else:
            continue
        for name in names:
            while name:
                if name in modules:
                    found.add(name)
                name = name.rpartition(".")[0]
    return found


def check(root: Path) -> list[str]:
    problems = []
    pkg = root / "noeta"
    for entry in sorted(pkg.iterdir()):
        if entry.name == "__pycache__" or entry.name in TOP_LEVEL:
            continue
        problems.append(f"noeta/{entry.name}: not a known top-level package "
                        f"({', '.join(sorted(TOP_LEVEL - {'__init__.py', 'py.typed'}))}); "
                        "see CONTEXT.md: Where code goes")
    core_files = [p for p in sorted((pkg / "core").rglob("*.py")) if "__pycache__" not in p.parts]
    modules = {module_name(root, p): p for p in core_files if p.name != "__init__.py"}
    used: set[str] = set()
    for path in core_files:
        used |= imported(root, path, set(modules)) - {module_name(root, path)}
    for name, path in modules.items():
        if name not in used and name not in KERNEL_SURFACE:
            problems.append(f"{path.relative_to(root)}: the kernel does not use it; move it to "
                            "noeta/toolkit (shared by plugins) or a plugin, or list it in "
                            "scripts/lint-layout.py KERNEL_SURFACE with the reason")
    return problems


def main(argv: list[str]) -> int:
    if len(argv) > 2:
        print(__doc__, file=sys.stderr)
        return 2
    root = Path(argv[1] if len(argv) == 2 else Path(__file__).resolve().parents[1])
    if not (root / "noeta" / "core").is_dir():
        print(f"lint-layout: {root} has no noeta/core", file=sys.stderr)
        return 2
    problems = check(root)
    for line in problems:
        print(f"lint-layout: {line}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
