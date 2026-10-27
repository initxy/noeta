"""``Grep``: ripgrep with the reference surface — output modes, ``-i`` /
``-n`` / ``-o`` / context, ``head_limit`` / ``offset``, ``-u``, the rg regex
dialect and the walk's defaults (ported from 0.6)."""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
from noeta.execenv.base import RunResult
from noeta.execenv.local import LocalExecEnv
from noeta.tools.grep import GrepTool

from .conftest import Harness, text_of


async def grep(h: Harness, **input: Any) -> tuple[str, bool]:
    out = await h.call(GrepTool(), **input)
    return text_of(out), out.is_error


@pytest.fixture
def fx(h: Harness, ws: Path) -> Harness:
    (ws / "a.py").write_text("alpha\nneedle here\nomega\n")
    (ws / "b.py").write_text("nothing\n")
    (ws / "c.txt").write_text("needle again\nand needle twice\n")
    return h


async def test_default_mode_lists_files(fx: Harness) -> None:
    text, err = await grep(fx, pattern="needle")
    assert not err
    assert set(text.splitlines()) == {"a.py", "c.txt"}


async def test_files_mode_is_path_sorted(fx: Harness) -> None:
    text, _ = await grep(fx, pattern="needle")
    assert text.splitlines() == ["a.py", "c.txt"]


async def test_no_matches_names_the_escape_hatch(fx: Harness) -> None:
    text, err = await grep(fx, pattern="unicorn")
    assert not err
    assert text == (
        "No matches found\n(hidden and gitignored files are not searched; "
        "pass -u: true to include them)"
    )


async def test_no_matches_is_plain_when_unrestricted(fx: Harness) -> None:
    text, _ = await grep(fx, pattern="unicorn", **{"-u": True})
    assert text == "No matches found"


async def test_no_matches_is_plain_for_a_file_target(fx: Harness) -> None:
    text, _ = await grep(fx, pattern="unicorn", path="a.py")
    assert text == "No matches found"


async def test_content_mode_with_line_numbers(fx: Harness) -> None:
    text, _ = await grep(fx, pattern="needle", output_mode="content", **{"-n": True})
    assert text.splitlines() == [
        "a.py:2:needle here",
        "c.txt:1:needle again",
        "c.txt:2:and needle twice",
    ]


async def test_content_mode_line_numbers_default_on(fx: Harness) -> None:
    text, _ = await grep(fx, pattern="needle here", output_mode="content")
    assert text.splitlines() == ["a.py:2:needle here"]


async def test_content_mode_without_line_numbers(fx: Harness) -> None:
    text, _ = await grep(fx, pattern="needle here", output_mode="content", **{"-n": False})
    assert text.splitlines() == ["a.py:needle here"]


async def test_context_param_with_dash_c_alias(h: Harness, ws: Path) -> None:
    (ws / "f.txt").write_text("one\nhit\nthree\n")
    via_context, _ = await grep(h, pattern="hit", output_mode="content", context=1)
    via_alias, _ = await grep(h, pattern="hit", output_mode="content", **{"-C": 1})
    assert via_context == via_alias
    assert via_context.splitlines() == ["f.txt-1-one", "f.txt:2:hit", "f.txt-3-three"]


async def test_after_and_before_context(h: Harness, ws: Path) -> None:
    (ws / "f.txt").write_text("one\ntwo\nhit\nfour\nfive\n")
    text, _ = await grep(h, pattern="hit", output_mode="content", **{"-A": 1})
    assert text.splitlines() == ["f.txt:3:hit", "f.txt-4-four"]
    text, _ = await grep(h, pattern="hit", output_mode="content", **{"-B": 2})
    assert text.splitlines() == ["f.txt-1-one", "f.txt-2-two", "f.txt:3:hit"]


async def test_only_matching_prints_matched_parts(h: Harness, ws: Path) -> None:
    (ws / "f.txt").write_text("say needle_a and needle_b\n")
    text, _ = await grep(h, pattern=r"needle_\w", output_mode="content", **{"-o": True})
    assert text.splitlines() == ["f.txt:1:needle_a", "f.txt:1:needle_b"]


async def test_offset_skips_leading_results(fx: Harness) -> None:
    text, _ = await grep(fx, pattern="needle", offset=1)
    assert text.splitlines()[0] == "c.txt"


async def test_head_limit_zero_is_unlimited(h: Harness, ws: Path) -> None:
    (ws / "many.txt").write_text("needle\n" * 300)
    text, err = await grep(h, pattern="needle", output_mode="content", head_limit=0)
    assert not err
    assert "(Showing" not in text
    assert len(text.splitlines()) == 300


async def test_case_insensitive(h: Harness, ws: Path) -> None:
    (ws / "x.py").write_text("NEEDLE\n")
    text, _ = await grep(h, pattern="needle", **{"-i": True})
    assert text.splitlines() == ["x.py"]


async def test_context_lines(h: Harness, ws: Path) -> None:
    (ws / "ctx.txt").write_text("one\ntwo\nhit\nfour\nfive\nsix\nhit\neight\n")
    text, _ = await grep(h, pattern="hit", output_mode="content", **{"-n": True, "-C": 1})
    assert text.splitlines() == [
        "ctx.txt-2-two",
        "ctx.txt:3:hit",
        "ctx.txt-4-four",
        "--",
        "ctx.txt-6-six",
        "ctx.txt:7:hit",
        "ctx.txt-8-eight",
    ]


async def test_context_separates_files(h: Harness, ws: Path) -> None:
    (ws / "a.txt").write_text("hit\n")
    (ws / "b.txt").write_text("hit\n")
    text, _ = await grep(h, pattern="hit", output_mode="content", **{"-C": 1})
    assert text.splitlines() == ["a.txt:1:hit", "--", "b.txt:1:hit"]


async def test_count_mode(fx: Harness) -> None:
    text, _ = await grep(fx, pattern="needle", output_mode="count")
    assert text.splitlines() == ["a.py:1", "c.txt:2"]


async def test_count_mode_truncation(fx: Harness) -> None:
    text, _ = await grep(fx, pattern="needle", output_mode="count", head_limit=1)
    assert text.splitlines() == ["a.py:1", "(Results truncated: 1 of 2 files shown.)"]


async def test_files_mode_truncation(fx: Harness) -> None:
    text, _ = await grep(fx, pattern="needle", head_limit=1)
    assert text.splitlines() == [
        "a.py",
        "(Results truncated: 1 of 2 files shown. Narrow the query or raise head_limit.)",
    ]


async def test_head_limit(fx: Harness) -> None:
    text, _ = await grep(fx, pattern="needle", output_mode="content", head_limit=1)
    lines = text.splitlines()
    assert lines == [
        "a.py:2:needle here",
        "(Showing 1 of 3 matches. Narrow the query or use head_limit.)",
    ]


async def test_content_byte_fence_trim_is_counted_in_the_footer(
    h: Harness, ws: Path
) -> None:
    wide = "needle " + ("w" * 1900)
    (ws / "wide.txt").write_text("\n".join([wide] * 40) + "\n")
    out = await h.call(GrepTool(), pattern="needle", output_mode="content")
    text = text_of(out)
    body = [line for line in text.splitlines() if not line.startswith("(Showing")]
    assert len(body) < 40, "the byte fence should have dropped lines"
    assert f"(Showing {len(body)} of 40 matches." in text
    assert out.meta["matches"] == len(body)
    assert len(text.encode("utf-8")) <= 32 * 1024 + 200


async def test_multiline_spans_lines(h: Harness, ws: Path) -> None:
    (ws / "m.py").write_text("def foo(\n    x\n):\n    pass\n")
    text, _ = await grep(
        h, pattern=r"foo\(.*?\):", multiline=True, output_mode="content", **{"-n": True}
    )
    assert text.splitlines() == ["m.py:1:def foo(", "m.py:2:    x", "m.py:3:):"]


@pytest.fixture
def hidden(h: Harness, ws: Path) -> Harness:
    (ws / "keep.py").write_text("needle\n")
    (ws / ".git").mkdir()
    (ws / ".gitignore").write_text("node_modules/\n")
    (ws / "node_modules").mkdir()
    (ws / "node_modules" / "dep.py").write_text("needle\n")
    (ws / ".github").mkdir()
    (ws / ".github" / "ci.yml").write_text("needle\n")
    return h


async def test_skips_hidden_and_gitignored_dirs(hidden: Harness) -> None:
    text, _ = await grep(hidden, pattern="needle")
    assert text.splitlines() == ["keep.py"]


async def test_unrestricted_searches_hidden_and_gitignored(hidden: Harness) -> None:
    text, _ = await grep(hidden, pattern="needle", **{"-u": True})
    assert set(text.splitlines()) == {".github/ci.yml", "keep.py", "node_modules/dep.py"}


async def test_hidden_root_is_searched_when_targeted(hidden: Harness) -> None:
    text, _ = await grep(hidden, pattern="needle", path=".github")
    assert text.splitlines() == [".github/ci.yml"]


async def test_type_filter(fx: Harness) -> None:
    text, _ = await grep(fx, pattern="needle", type="py")
    assert text.splitlines() == ["a.py"]


async def test_unknown_type_rejected_by_rg(h: Harness) -> None:
    text, err = await grep(h, pattern="x", type="no-such-type")
    assert err
    assert text.startswith("Grep: rg: ")
    assert "no-such-type" in text


async def test_explicit_file_target_bypasses_the_type_filter(fx: Harness) -> None:
    text, _ = await grep(fx, pattern="needle", path="c.txt", type="py")
    assert text.splitlines() == ["c.txt"]


async def test_absolute_glob_filter_rejected(h: Harness) -> None:
    text, err = await grep(h, pattern="x", glob="/etc/**")
    assert err
    assert text == (
        "Grep: 'glob' must be relative to the searched directory (no leading "
        "'/' / '..') — use 'path' to search another tree"
    )


async def test_scoped_to_path(fx: Harness, ws: Path) -> None:
    (ws / "sub").mkdir()
    (ws / "sub" / "d.py").write_text("needle deep\n")
    text, _ = await grep(fx, pattern="needle", path="sub")
    assert text.splitlines() == ["sub/d.py"]


async def test_glob_filter(fx: Harness) -> None:
    text, _ = await grep(fx, pattern="needle", glob="**/*.py")
    assert text.splitlines() == ["a.py"]


async def test_on_single_file(fx: Harness) -> None:
    text, _ = await grep(fx, pattern="needle", path="c.txt", output_mode="content")
    assert text.splitlines() == ["c.txt:1:needle again", "c.txt:2:and needle twice"]


async def test_reaches_outside_the_workspace(h: Harness, ws: Path) -> None:
    outside = ws.parent / "elsewhere"
    outside.mkdir()
    (outside / "far.txt").write_text("needle far\n")
    text, err = await grep(h, pattern="needle", path=str(outside))
    assert not err
    assert text.splitlines() == [str(outside / "far.txt")]


async def test_missing_path_yields_no_matches(h: Harness) -> None:
    text, err = await grep(h, pattern="x", path="nowhere")
    assert not err
    assert text.startswith("No matches found")


async def test_invalid_regex_rejected(h: Harness) -> None:
    text, err = await grep(h, pattern="([unclosed")
    assert err
    assert "regex parse error" in text


async def test_bad_output_mode_rejected(h: Harness) -> None:
    text, err = await grep(h, pattern="x", output_mode="lines")
    assert err
    assert text == "Grep: 'output_mode' must be one of: files_with_matches, content, count"


async def test_lookaround_rejected_by_the_rg_dialect(h: Harness) -> None:
    text, err = await grep(h, pattern=r"foo(?=bar)")
    assert err
    assert "look-around" in text


async def test_nested_quantifiers_are_fine(h: Harness, ws: Path) -> None:
    (ws / "n.py").write_text("aa.bb. needle\n")
    text, err = await grep(h, pattern=r"(\w+\.)+ needle")
    assert not err
    assert text.splitlines() == ["n.py"]


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read anything")
async def test_survives_an_unreadable_file(h: Harness, ws: Path) -> None:
    (ws / "ok.txt").write_text("needle\n")
    locked = ws / "locked.txt"
    locked.write_text("needle\n")
    locked.chmod(0)
    try:
        text, err = await grep(h, pattern="needle")
    finally:
        locked.chmod(0o644)
    assert not err
    assert text.splitlines() == ["ok.txt"]


async def test_content_head_limit_is_an_output_line_cap(h: Harness, ws: Path) -> None:
    (ws / "ctx.txt").write_text("hit\nx\n" * 10)
    text, _ = await grep(h, pattern="hit", output_mode="content", head_limit=4, **{"-C": 1})
    body = [line for line in text.splitlines() if not line.startswith("(Showing")]
    assert len(body) == 4


class _CappedStream(LocalExecEnv):
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
        return RunResult(
            exit_code=res.exit_code, stdout=res.stdout, stderr=res.stderr, stdout_truncated=True
        )


async def test_notes_a_capped_rg_stream(ws: Path) -> None:
    h = Harness(env=_CappedStream(str(ws)))
    (ws / "a.txt").write_text("needle\n")
    text, err = await grep(h, pattern="needle")
    assert not err
    assert text.splitlines()[0] == "a.txt"
    assert text.endswith(
        "\n(rg output exceeded the capture cap — results are partial; narrow the query.)"
    )


async def test_reports_a_missing_rg_binary(ws: Path) -> None:
    class _NoRg(LocalExecEnv):
        async def run(self, argv: Sequence[str], **kw: object) -> RunResult:
            raise FileNotFoundError("rg")

    h = Harness(env=_NoRg(str(ws)))
    text, err = await grep(h, pattern="x")
    assert err
    assert text == (
        "Grep: ripgrep (rg) is required but was not found in the execution "
        "environment — install it (https://github.com/BurntSushi/ripgrep) or add "
        "it to the sandbox image"
    )


async def test_reports_a_timed_out_search(ws: Path) -> None:
    class _Slow(LocalExecEnv):
        async def run(self, argv: Sequence[str], **kw: object) -> RunResult:
            return RunResult(exit_code=-1, stdout=b"", timed_out=True)

    h = Harness(env=_Slow(str(ws)))
    text, err = await grep(h, pattern="x")
    assert err
    assert text == "Grep: search timed out after 60s — narrow the pattern or path"


async def test_skips_binary_files(h: Harness, ws: Path) -> None:
    (ws / "blob.bin").write_bytes(b"\x00needle\x00")
    (ws / "ok.txt").write_text("needle\n")
    text, _ = await grep(h, pattern="needle")
    assert text.splitlines() == ["ok.txt"]


async def test_caps_long_lines(h: Harness, ws: Path) -> None:
    (ws / "wide.txt").write_text("needle " + "x" * 5000 + "\n")
    text, _ = await grep(h, pattern="needle", output_mode="content")
    line = text.splitlines()[0]
    assert line.endswith("… [line truncated]")
    assert len(line) < 2200


async def test_default_head_limit_is_250(h: Harness, ws: Path) -> None:
    (ws / "many.txt").write_text("needle\n" * 300)
    text, _ = await grep(h, pattern="needle", output_mode="content")
    assert "Showing 250 of 300 matches" in text


@pytest.mark.parametrize(
    "args",
    [
        {"pattern": ""},
        {"pattern": "x", "path": 3},
        {"pattern": "x", "glob": 3},
        {"pattern": "x", "type": 3},
        {"pattern": "x", "-A": -1},
        {"pattern": "x", "-B": True},
        {"pattern": "x", "context": -1},
        {"pattern": "x", "-C": "2"},
        {"pattern": "x", "head_limit": -1},
        {"pattern": "x", "offset": -1},
    ],
)
async def test_arg_validation(h: Harness, args: dict[str, Any]) -> None:
    text, err = await grep(h, **args)
    assert err
    assert text.startswith("Grep: ")


async def test_head_limit_error_names_zero(h: Harness) -> None:
    text, _ = await grep(h, pattern="x", head_limit=-1)
    assert text == "Grep: 'head_limit' must be a non-negative integer (0 means unlimited)"
