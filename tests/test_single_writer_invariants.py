"""Single-writer enforcement at the file boundary.

``task.context.plan_ref = ...`` must only appear in ``core/fold.py``.
Anywhere else (Composer, Policy, Tool, Engine) is a violation: even
Engine's live path converges its state through fold's
:func:`apply_event`, so the assignment line stays in fold.py — the lint
check below is the regression barrier.

Future issues extend this file with the same shape of check for
``runtime.messages`` / ``state.*`` / ``governance.*`` writers.
"""

from __future__ import annotations

import re
from pathlib import Path


def test_context_state_plan_ref_single_writer() -> None:
    pkg = Path(__file__).resolve().parents[1] / "noeta"
    allowed = {"core/fold.py"}
    offenders: list[str] = []
    pattern = re.compile(r"task\.context\.plan_ref\s*=")
    for path in pkg.rglob("*.py"):
        rel = path.relative_to(pkg).as_posix()
        if rel in allowed:
            continue
        if pattern.search(path.read_text(encoding="utf-8")):
            offenders.append(rel)
    assert offenders == [], (
        f"task.context.plan_ref written outside fold.py: {offenders}"
    )
