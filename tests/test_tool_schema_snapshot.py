"""Golden snapshot of every built-in tool's model-visible schema.

For each tool in ``builtin_tool_classes()`` this pins the exact metadata the LLM
adapter advertises: ``name``, ``description`` (the hand-written, LLM-facing
semantics — the single source of truth for what a tool means to the model),
``input_schema``, and ``risk_level`` (the approval-gating tier). Any edit to a
description or a schema changes what the model sees and invalidates the cached
stable prefix, so it must surface as a human-readable diff against one
all-tools golden rather than as a behaviour change nobody reviewed.

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

from noeta.client.parts import builtin_tool_classes

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
    for name, cls in sorted(builtin_tool_classes().items()):
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
    """The golden carries exactly the built-in tool set, no more and no less.

    Asserting the name set directly is what makes a coverage gap loud: a tool
    the snapshot never captured could change its schema freely without failing
    the golden above.
    """
    captured = {entry["name"] for entry in _tool_schema_view()}
    assert captured == set(builtin_tool_classes())
    assert len(captured) == len(builtin_tool_classes())
