"""Instruction files: Claude Code's layers in noeta's places (``NOETA.md``:
managed, user, project and local, with ``@path`` imports and
``.noeta/rules``), the cross-vendor ``AGENTS.md``, and the auto memory
index."""

from noeta.plugins.instructions.extension import (
    DEFAULT_INSTRUCTIONS_FILENAMES,
    Instructions,
    render_file,
    render_instructions,
)

__all__ = ["DEFAULT_INSTRUCTIONS_FILENAMES", "Instructions", "render_file",
           "render_instructions"]
