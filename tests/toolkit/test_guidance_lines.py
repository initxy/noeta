"""The main prompt's conditional ``# Session-specific guidance`` lines: each
goes by its mark, and the heading (with its blank line) only with the last."""

from __future__ import annotations

from noeta.toolkit.guidance_lines import drop_guidance_line

BANG = " - Suggest `! <command>` for it.\n"
SKILL = " - When the user types `/<skill-name>`, run it.\n"


def _prompt(body: str) -> str:
    return f"Intro.\n\n# Session-specific guidance\n{body}\n# Environment\n - Facts.\n"


def test_each_line_goes_by_its_mark_and_the_heading_with_the_last() -> None:
    both = _prompt(BANG + SKILL)
    assert drop_guidance_line(both, "`! <command>`") == _prompt(SKILL)
    assert drop_guidance_line(both, "`/<skill-name>`") == _prompt(BANG)
    gone = "Intro.\n\n# Environment\n - Facts.\n"
    for first, second in (("`! <command>`", "`/<skill-name>`"),
                          ("`/<skill-name>`", "`! <command>`")):
        assert drop_guidance_line(drop_guidance_line(both, first), second) == gone


def test_a_line_kept_after_another_keeps_its_place() -> None:
    other = " - Other.\n"
    assert drop_guidance_line(_prompt(other + SKILL), "`/<skill-name>`") == _prompt(other)


def test_a_missing_line_or_section_leaves_the_text_alone() -> None:
    assert drop_guidance_line(_prompt(SKILL), "`! <command>`") == _prompt(SKILL)
    assert drop_guidance_line("No section. `! <command>`", "`! <command>`") == \
        "No section. `! <command>`"


def test_a_section_at_the_end_goes_whole() -> None:
    text = "Intro.\n\n# Session-specific guidance\n" + BANG.rstrip("\n")
    assert drop_guidance_line(text, "`! <command>`") == "Intro.\n\n"
