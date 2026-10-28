"""A skill's argument placeholders, filled as Claude Code fills them."""

from __future__ import annotations

import pytest

from noeta.plugins.skills.registry import substitute_arguments


@pytest.mark.parametrize(("body", "args", "out"), [
    ("Fix $ARGUMENTS now.", "issue 12", "Fix issue 12 now."),
    ("From $0 to $1.", "a 'b c'", "From a to b c."),
    ("First $ARGUMENTS[0], third $ARGUMENTS[2].", "x y", "First x, third $ARGUMENTS[2]."),
    ("Keep $5 and $1x.", "a b", "Keep $5 and $1x.\nARGUMENTS: a b"),
    (r"Literal \$ARGUMENTS and $ARGUMENTS.", "v", "Literal $ARGUMENTS and v."),
    ("No placeholder.", "v1 v2", "No placeholder.\nARGUMENTS: v1 v2"),
    ("No placeholder.", "", "No placeholder."),
    ("Empty: [$ARGUMENTS]", "", "Empty: []"),
    ("Untouched $ARGUMENTS $0", None, "Untouched $ARGUMENTS $0"),
    # an argument's own $ is never filled again
    ("Run $ARGUMENTS and $1", "$1 two", "Run $1 two and two"),
    ("Unbalanced $1", "a 'b", "Unbalanced 'b"),
])
def test_substitute_arguments(body: str, args: str | None, out: str) -> None:
    assert substitute_arguments(body, args) == out
