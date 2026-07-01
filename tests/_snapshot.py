"""Tiny hand-written golden-file (snapshot) helper.

Captures the *model-visible bytes* of the official preset agents and the
built-in tool schemas, so a refactor that silently changes them fails a test
with a **human-readable text diff** instead of slipping through. This is the
lightweight replacement for the deleted verify/replay byte-equality moat: its
one valuable property — "did a refactor change what the model sees?" — kept
without the heavy machinery.

No third-party snapshot library (syrupy / pytest-snapshot) is used — the
comparison is a plain UTF-8 file read. Goldens live under ``tests/snapshots/``.

Re-pin (regenerate goldens) with one command::

    UPDATE_SNAPSHOTS=1 uv run pytest \\
        tests/test_prompt_snapshot.py tests/test_tool_schema_snapshot.py \\
        -q -p no:cacheprovider

When ``UPDATE_SNAPSHOTS`` is set, :func:`assert_snapshot` writes the actual
text to the golden and passes; otherwise it reads the golden and asserts
byte-equality, printing a unified diff on mismatch.
"""

from __future__ import annotations

import difflib
import json
import os
from pathlib import Path
from typing import Any

import pytest


SNAPSHOT_DIR = Path(__file__).parent / "snapshots"


def _update_requested() -> bool:
    """True when the run should (re)write goldens rather than compare."""
    return os.environ.get("UPDATE_SNAPSHOTS", "") not in ("", "0", "false", "False")


def stable_json(obj: Any) -> str:
    """Serialize ``obj`` to deterministic, human-diffable JSON text.

    ``sort_keys=True`` so dict ordering can never introduce noise, two-space
    indent so a diff reads line-by-line, ``ensure_ascii=False`` so non-ASCII
    prompt text stays legible. A trailing newline keeps the golden file
    POSIX-clean. Contains no object ids / addresses / timestamps by
    construction — callers pass only plain JSON-able data.
    """
    return json.dumps(obj, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def assert_snapshot(name: str, actual: str) -> None:
    """Compare ``actual`` against the golden ``tests/snapshots/<name>``.

    ``name`` is a bare filename (e.g. ``"preset_main.txt"``). On
    ``UPDATE_SNAPSHOTS`` the golden is written and the call passes. Otherwise
    the golden is read and compared; a mismatch fails with a unified text diff,
    and a missing golden fails with the re-pin instruction.
    """
    golden = SNAPSHOT_DIR / name

    if _update_requested():
        SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        golden.write_text(actual, encoding="utf-8")
        return

    if not golden.exists():
        pytest.fail(
            f"Missing snapshot golden {golden}.\n"
            f"Generate it with:\n"
            f"  UPDATE_SNAPSHOTS=1 uv run pytest <this test> "
            f"-q -p no:cacheprovider"
        )

    expected = golden.read_text(encoding="utf-8")
    if actual == expected:
        return

    diff = "".join(
        difflib.unified_diff(
            expected.splitlines(keepends=True),
            actual.splitlines(keepends=True),
            fromfile=f"{name} (golden)",
            tofile=f"{name} (actual)",
        )
    )
    pytest.fail(
        f"Snapshot drift for {name} — the model-visible bytes changed.\n"
        f"If this change is intentional, re-pin with:\n"
        f"  UPDATE_SNAPSHOTS=1 uv run pytest <this test> "
        f"-q -p no:cacheprovider\n\n"
        f"{diff}"
    )
