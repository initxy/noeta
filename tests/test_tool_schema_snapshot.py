"""Golden snapshot of every built-in tool's model-visible schema.

For each tool in ``BUILTIN_TOOL_CLASSES`` (read / glob / grep / edit / write /
apply_patch / shell_run / shell_poll / shell_kill / webfetch) this pins the
exact metadata the LLM adapter advertises to the model:

* ``name``
* ``description`` — the hand-written, LLM-facing semantics (the canonical tool
  description: the single source of truth for a tool's model-visible meaning)
* ``input_schema`` — the JSON-Schema-shaped argument contract
* ``risk_level`` — the approval-gating tier

A refactor that changes a tool description or its schema (the kind of drift the
old fingerprint / tool_versions manifest guarded) fails the single all-tools
golden with a human-readable text diff.

Metadata is read off each tool class' **static dataclass-field defaults** — the
same values ``builtin_tool_ref`` reads — so no live tool needs to be wired
(``read`` wants a ``WorkspaceRoot``, ``shell_run`` a runner, etc.). These
defaults are exactly what the model ultimately sees.

Re-pin (regenerate the golden) with one command::

    UPDATE_SNAPSHOTS=1 uv run pytest \\
        tests/test_prompt_snapshot.py tests/test_tool_schema_snapshot.py \\
        -q -p no:cacheprovider

Determinism: only the four plain JSON-able fields are captured (no object ids /
addresses / timestamps), tools iterate in sorted order, and ``stable_json``
sorts dict keys.
"""

from __future__ import annotations

import dataclasses
from typing import Any

from noeta.client.parts import BUILTIN_TOOL_CLASSES

from tests._snapshot import assert_snapshot, stable_json


def _static_default(cls: type, field_name: str) -> Any:
    """Return ``cls.field_name``'s static dataclass-field default.

    Resolves ``field(default=...)`` and ``field(default_factory=...)`` without
    constructing the tool (so wiring args like ``WorkspaceRoot`` / runner are
    not needed). Raises if a field has no static default — that would mean the
    metadata cannot be read without instantiation, which the snapshot must not
    paper over.
    """
    for f in dataclasses.fields(cls):
        if f.name == field_name:
            if f.default is not dataclasses.MISSING:
                return f.default
            if f.default_factory is not dataclasses.MISSING:  # type: ignore[misc]
                return f.default_factory()  # fresh instance each call
            raise TypeError(
                f"{cls.__name__}.{field_name} has no static default; "
                f"cannot read tool schema without instantiation"
            )
    raise AttributeError(f"{cls.__name__} has no field {field_name!r}")


def _tool_schema_view() -> list[dict[str, object]]:
    """Build the stable snapshot payload for every built-in tool, name-sorted."""
    out: list[dict[str, object]] = []
    for name, cls in sorted(BUILTIN_TOOL_CLASSES.items()):
        out.append(
            {
                "name": str(_static_default(cls, "name")),
                "description": str(_static_default(cls, "description")),
                "input_schema": _static_default(cls, "input_schema"),
                "risk_level": str(_static_default(cls, "risk_level")),
            }
        )
    return out


def test_builtin_tool_schemas_snapshot() -> None:
    """Every built-in tool's name + description + input_schema + risk_level
    matches the single all-tools golden."""
    payload = stable_json(_tool_schema_view())
    assert_snapshot("builtin_tool_schemas.txt", payload)


def test_snapshot_covers_all_builtin_tools() -> None:
    """Guard: the golden carries exactly the current built-in tool set.

    Catches a tool added to (or removed from) ``BUILTIN_TOOL_CLASSES`` that the
    snapshot author forgot to re-pin — the count/name set is asserted directly
    so a drift in coverage is loud, not silent.
    """
    captured = {entry["name"] for entry in _tool_schema_view()}
    assert captured == set(BUILTIN_TOOL_CLASSES)
    assert len(captured) == len(BUILTIN_TOOL_CLASSES)
