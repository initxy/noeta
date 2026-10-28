"""The glob dialect of Write's allowed_path_globs."""

from __future__ import annotations

import pytest

from noeta.plugins.fs._globs import compile_glob, expand_braces, glob_matches


@pytest.mark.parametrize(
    "pattern,path,expected",
    [
        ("*.py", "a.py", True),
        ("*.py", "sub/a.py", False),
        ("**/*.py", "a.py", True),
        ("**/*.py", "x/y/a.py", True),
        ("src/**", "src/a/b", True),
        ("*.{md,txt}", "notes.txt", True),
        ("[ab].py", "c.py", False),
        ("./*.py", "a.py", True),
    ],
)
def test_glob_dialect(pattern: str, path: str, expected: bool) -> None:
    assert glob_matches(tuple(path.split("/")), compile_glob(pattern)) is expected


def test_brace_edge_cases_stay_literal() -> None:
    assert expand_braces("{x}.py") == ["{x}.py"]
    assert expand_braces("{a,b") == ["{a,b"]
    assert expand_braces("a{b,{c,d}}") == ["ab", "ac", "ad"]
