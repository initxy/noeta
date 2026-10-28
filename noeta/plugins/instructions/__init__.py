"""Instruction files: Claude Code's ``CLAUDE.md`` layers (managed, user,
project and local, with ``@path`` imports and ``.claude/rules``), noeta's
``NOETA.md`` / ``AGENTS.md``, and the auto memory index."""

from noeta.plugins.instructions.extension import (
    DEFAULT_INSTRUCTIONS_FILENAMES,
    Instructions,
    render_file,
    render_instructions,
)

__all__ = ["DEFAULT_INSTRUCTIONS_FILENAMES", "Instructions", "render_file",
           "render_instructions"]
