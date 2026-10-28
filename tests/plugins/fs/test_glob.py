"""``Glob``: rg's walk (gitignore-aware, hidden skipped, symlinks not
followed) plus pathlib glob matching with ``{a,b}``, newest first, capped
(ported from 0.6)."""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from noeta.backends.execenvs.local import LocalExecEnv
from noeta.core.execenv import RunResult
from noeta.plugins.fs._rg import compile_glob, expand_braces, glob_matches
from noeta.plugins.fs.glob import GlobTool
from tests.plugins.conftest import Harness, text_of


async def test_glob_matches_relative_pattern(h: Harness, ws: Path) -> None:
    (ws / "a.py").write_text("")
    (ws / "sub").mkdir()
    (ws / "sub" / "b.py").write_text("")
    (ws / "c.txt").write_text("")
    out = await h.call(GlobTool(), pattern="**/*.py")
    assert not out.is_error
    assert set(text_of(out).splitlines()) == {"a.py", "sub/b.py"}


async def test_glob_star_stays_top_level(h: Harness, ws: Path) -> None:
    (ws / "a.py").write_text("")
    (ws / "sub").mkdir()
    (ws / "sub" / "b.py").write_text("")
    assert text_of(await h.call(GlobTool(), pattern="*.py")).splitlines() == ["a.py"]


async def test_glob_sorts_by_mtime_newest_first(h: Harness, ws: Path) -> None:
    old, new = ws / "old.py", ws / "new.py"
    old.write_text("")
    new.write_text("")
    os.utime(old, (1_000_000, 1_000_000))
    os.utime(new, (2_000_000, 2_000_000))
    out = await h.call(GlobTool(), pattern="*.py")
    assert text_of(out).splitlines() == ["new.py", "old.py"]


async def test_glob_mtime_ties_break_alphabetically(h: Harness, ws: Path) -> None:
    for name in ("b.py", "a.py", "c.py"):
        (ws / name).write_text("")
        os.utime(ws / name, (1_000_000, 1_000_000))
    out = await h.call(GlobTool(), pattern="*.py")
    assert text_of(out).splitlines() == ["a.py", "b.py", "c.py"]


async def test_glob_skips_hidden_and_gitignored_dirs(h: Harness, ws: Path) -> None:
    (ws / "keep.py").write_text("")
    (ws / ".git").mkdir()  # rg only honours .gitignore in a repo
    (ws / ".gitignore").write_text("node_modules/\n")
    (ws / "node_modules").mkdir()
    (ws / "node_modules" / "dep.py").write_text("")
    (ws / ".hidden").mkdir()
    (ws / ".hidden" / "h.py").write_text("")
    out = await h.call(GlobTool(), pattern="**/*.py")
    assert text_of(out).splitlines() == ["keep.py"]


async def test_glob_hidden_root_is_searched_when_targeted(h: Harness, ws: Path) -> None:
    (ws / ".hidden").mkdir()
    (ws / ".hidden" / "h.py").write_text("")
    out = await h.call(GlobTool(), pattern="*.py", path=".hidden")
    assert text_of(out).splitlines() == [".hidden/h.py"]


async def test_glob_no_matches(h: Harness) -> None:
    out = await h.call(GlobTool(), pattern="*.zig")
    assert not out.is_error
    assert text_of(out) == "No files found (hidden and gitignored files are not listed)"


async def test_glob_pattern_required(h: Harness) -> None:
    out = await h.call(GlobTool())
    assert out.is_error
    assert text_of(out) == "Glob: requires non-empty 'pattern'"


async def test_glob_absolute_pattern_rejected(h: Harness) -> None:
    out = await h.call(GlobTool(), pattern="/etc/*")
    assert out.is_error
    assert text_of(out) == (
        "Glob: pattern must be relative to the searched directory (no leading "
        "'/' / '..') — use 'path' to search another tree"
    )


async def test_glob_dotdot_pattern_rejected(h: Harness) -> None:
    assert (await h.call(GlobTool(), pattern="../*")).is_error


async def test_glob_searches_an_absolute_path_outside_the_workspace(
    h: Harness, ws: Path
) -> None:
    outside = ws.parent / "elsewhere"
    outside.mkdir()
    (outside / "x.md").write_text("")
    out = await h.call(GlobTool(), pattern="*.md", path=str(outside))
    assert not out.is_error
    assert text_of(out).splitlines() == [str(outside / "x.md")]


async def test_glob_path_scopes_to_a_subdirectory(h: Harness, ws: Path) -> None:
    (ws / "a.py").write_text("")
    (ws / "sub").mkdir()
    (ws / "sub" / "b.py").write_text("")
    out = await h.call(GlobTool(), pattern="*.py", path="sub")
    assert text_of(out).splitlines() == ["sub/b.py"]


async def test_glob_path_must_be_a_string(h: Harness) -> None:
    out = await h.call(GlobTool(), pattern="*.py", path=7)
    assert out.is_error
    assert text_of(out) == "Glob: 'path' must be a string"


async def test_glob_missing_path_yields_no_matches(h: Harness) -> None:
    out = await h.call(GlobTool(), pattern="*.py", path="nowhere")
    assert not out.is_error
    assert text_of(out) == "No files found"


async def test_glob_file_path_yields_no_matches(h: Harness, ws: Path) -> None:
    (ws / "a.py").write_text("")
    out = await h.call(GlobTool(), pattern="*.py", path="a.py")
    assert text_of(out) == "No files found"


async def test_glob_drops_symlink_to_outside(h: Harness, ws: Path) -> None:
    outside = ws.parent / "secret.py"
    outside.write_text("")
    (ws / "leak.py").symlink_to(outside)
    out = await h.call(GlobTool(), pattern="*.py")
    assert text_of(out).startswith("No files found")


async def test_glob_drops_a_symlink_escaping_the_searched_tree(
    h: Harness, ws: Path
) -> None:
    (ws / "inner").mkdir()
    real = ws / "real.py"
    real.write_text("")
    (ws / "inner" / "leak.py").symlink_to(real)
    out = await h.call(GlobTool(), pattern="*.py", path="inner")
    assert text_of(out).startswith("No files found")


async def test_glob_bounded_under_many_matches(h: Harness, ws: Path) -> None:
    for i in range(230):
        (ws / f"f{i:03}.py").write_text("")
    out = await h.call(GlobTool(), pattern="*.py")
    lines = text_of(out).splitlines()
    shown = [line for line in lines if not line.startswith("(Results truncated")]
    assert len(shown) == 200
    assert lines[-1] == (
        "(Results truncated: showing 200 of 230 matches, newest first. "
        "Narrow the pattern.)"
    )
    assert out.meta == {"matches": 200, "total": 230}


async def test_glob_supports_brace_alternatives(h: Harness, ws: Path) -> None:
    (ws / "src").mkdir()
    for name in ("a.ts", "b.tsx", "c.js", "d.md"):
        (ws / "src" / name).write_text("x\n")
    out = await h.call(GlobTool(), pattern="**/*.{ts,tsx}")
    assert sorted(text_of(out).splitlines()) == ["src/a.ts", "src/b.tsx"]
    out = await h.call(GlobTool(), pattern="src/*.{j{s,x},md}")
    assert sorted(text_of(out).splitlines()) == ["src/c.js", "src/d.md"]


async def test_glob_brace_explosion_is_refused(h: Harness) -> None:
    out = await h.call(GlobTool(), pattern="{a,b}{c,d}{e,f}{g,h}{i,j}{k,l}{m,n}{o,p}{q,r}")
    assert out.is_error
    assert "expands to more than 256 alternatives" in text_of(out)


async def test_glob_notes_a_capped_rg_stream(ws: Path) -> None:
    class _Capped(LocalExecEnv):
        async def run(
            self,
            argv: Sequence[str],
            *,
            cwd: str | None = None,
            env: Mapping[str, str] | None = None,
            stdin: bytes | None = None,
            timeout: float | None = None,
            output_cap: int | None = None,
        ) -> RunResult:
            res = await super().run(argv, cwd=cwd, timeout=timeout, output_cap=output_cap)
            return RunResult(exit_code=res.exit_code, stdout=res.stdout, stdout_truncated=True)

    h = Harness(env=_Capped(str(ws)))
    (ws / "a.py").write_text("")
    text = text_of(await h.call(GlobTool(), pattern="*.py"))
    assert text.startswith("a.py\n")
    assert "the listing is partial" in text


async def test_glob_reports_a_missing_rg_binary(ws: Path) -> None:
    class _NoRg(LocalExecEnv):
        async def run(self, argv: Sequence[str], **kw: object) -> RunResult:
            raise FileNotFoundError("rg")

    h = Harness(env=_NoRg(str(ws)))
    out = await h.call(GlobTool(), pattern="*.py")
    assert out.is_error
    assert "ripgrep (rg) is required" in text_of(out)


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
