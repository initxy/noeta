"""What an "always" approval of a shell command remembers, as Claude Code
seeds its rule, and how such a rule reads to a person."""

from __future__ import annotations

import re
import shlex
from collections.abc import Mapping
from typing import Any

from noeta.toolkit.shell import (
    WRAPPER_PROGRAMS,
    has_shell_meta,
    has_unquoted_expansion,
    parse_argv,
)

#: Programs that delete or overwrite the paths they are given (Claude Code
#: backs a prefix rule on these with a protected-path check noeta lacks) and
#: interpreters, whose ``-c`` / ``-e`` runs any code: an approval of one is
#: remembered as the exact command.
EXACT_ONLY_PROGRAMS = frozenset({
    "rm", "rmdir", "mv", "cp", "dd", "ln", "chmod", "chown", "chgrp", "truncate", "shred",
    "python", "python3", "node", "perl", "ruby", "php", "deno", "bun", "lua", "osascript",
})

_WORD_RE = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$")


def rule_spec_from_command(command: str) -> dict[str, str] | None:
    """What an "always" approval of ``command`` remembers, seeded the way
    Claude Code seeds its rule: ``program subcommand *`` when the second word
    is a plain subcommand (``git push origin x`` -> ``git push *``), else
    ``program *`` for a plain program name (``ls -la`` -> ``ls *``), else the
    exact command. A wrapper (``sudo``, ``bash``, ...), a destructive file
    program (``rm``, ``mv``, ...) or a leading ``VAR=value`` gets the exact
    command. ``None`` for a command with metacharacters, unquoted expansions
    or unbalanced quotes, which no rule matches."""
    if has_shell_meta(command) or has_unquoted_expansion(command):
        return None
    argv = parse_argv(command)
    if not argv:
        return None
    program = argv[0]
    if (program in WRAPPER_PROGRAMS or program in EXACT_ONLY_PROGRAMS
            or not _WORD_RE.match(program)):
        return {"rule": shlex.join(argv)}
    if len(argv) >= 2 and _WORD_RE.match(argv[1]):
        return {"rule": f"{shlex.join(argv[:2])} *"}
    return {"rule": f"{program} *"}


def rule_spec_text(spec: Mapping[str, Any]) -> str:
    """How a rule spec reads to a person: ``git push *``, ``rm -rf build``."""
    text = spec.get("rule")
    if isinstance(text, str):
        return text
    words = [w for w in (spec.get("program"), spec.get("subcommand")) if isinstance(w, str)]
    return f"{shlex.join(words)} *"
