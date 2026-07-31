"""Single-writer enforcement at the file boundary.

``task.context.plan_ref = ...`` may appear only in ``core/fold.py``. Even the
Engine's live path converges its state through fold's :func:`apply_event`, so
an assignment anywhere else (Composer, Policy, Tool, Engine) would mean two
writers for one field and a live state that a replay cannot reproduce. Type
checking cannot express that, so the source scan below is the barrier.
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
