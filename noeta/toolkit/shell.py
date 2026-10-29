"""Pure shell-command classification: parsing, the metacharacter and
expansion checks, the wrapper programs, subcommand splitting, Claude Code's
long-leading-``sleep`` check, and the ``git`` flags that switch off a
repository's own helper programs.

Nothing here runs anything or reads a file.
"""

from __future__ import annotations

import re
import shlex

__all__ = [
    "LONG_SLEEP_S",
    "SHELL_EXPANSION_CHARS",
    "SHELL_META_CHARS",
    "WRAPPER_PROGRAMS",
    "harden_git",
    "has_shell_meta",
    "has_unquoted_expansion",
    "long_leading_sleep",
    "parse_argv",
    "subcommands",
]


SHELL_META_CHARS = frozenset(";&|<>`$()\n\r")

#: Characters bash expands in an UNQUOTED word — brace expansion, globbing,
#: tilde expansion — that ``shlex`` leaves literal. A command carrying one
#: unquoted would be judged on an argv bash never runs
#: (``find . {-exec,touch} x {} +`` hides ``-exec`` from the validator).
SHELL_EXPANSION_CHARS = frozenset("{*?[~")


# ---------------------------------------------------------------------------
# Parsing and classification
# ---------------------------------------------------------------------------


def parse_argv(command: str) -> list[str] | None:
    """POSIX ``shlex`` split, or ``None`` for unbalanced quotes."""
    try:
        return shlex.split(command, posix=True)
    except ValueError:
        return None


def has_shell_meta(command: str) -> bool:
    return any(c in SHELL_META_CHARS for c in command)


def has_unquoted_expansion(command: str) -> bool:
    """True iff a :data:`SHELL_EXPANSION_CHARS` character sits outside quotes.

    Mirrors bash's quoting: a backslash escapes the next character, nothing
    inside single quotes is special, and inside double quotes brace, glob and
    tilde expansion do not happen. ``~`` expands only as a tilde prefix — at
    the start of a word or after ``=`` / ``:`` — so ``HEAD~1`` stays allowed.
    Unbalanced quotes are left to ``shlex``, which rejects them.
    """
    quote: str | None = None
    escaped = False
    prev = " "
    for ch in command:
        if escaped:
            escaped = False
        elif quote == "'":
            if ch == "'":
                quote = None
        elif ch == "\\":
            escaped = True
        elif quote == '"':
            if ch == '"':
                quote = None
        elif ch in ("'", '"'):
            quote = ch
        elif ch == "~":
            if prev.isspace() or prev in "=:":
                return True
        elif ch in SHELL_EXPANSION_CHARS:
            return True
        prev = ch
    return False


#: Programs that run another command (Claude Code's list): a prefix rule on
#: one would allow whatever it wraps.
WRAPPER_PROGRAMS = frozenset({
    "sh", "bash", "zsh", "fish", "csh", "tcsh", "ksh", "dash", "cmd", "powershell", "pwsh",
    "env", "xargs", "command", "builtin", "noglob", "nice", "stdbuf", "nohup", "timeout",
    "time", "watch", "ionice", "chrt", "setsid", "taskset", "strace", "ltrace", "script",
    "flock", "unshare", "nsenter", "sudo", "doas", "pkexec", "su", "runuser",
})

# ---------------------------------------------------------------------------
# Long leading sleep (Claude Code 2.1.300)
# ---------------------------------------------------------------------------

#: Claude Code's threshold for a leading ``sleep`` it refuses, in seconds.
LONG_SLEEP_S = 25.0

#: The shell's control operators — where a command splits into subcommands.
_CONTROL_OPERATORS = ("&&", "||", ";", "|", "&", "\n")


def subcommands(command: str) -> list[str]:
    """``command`` split at its unquoted control operators (empty parts
    dropped). Quoting is honoured just far enough that an operator inside
    quotes does not split."""
    parts: list[str] = []
    current: list[str] = []
    quote = ""
    i = 0
    while i < len(command):
        ch = command[i]
        if quote:
            current.append(ch)
            if ch == "\\" and quote == '"' and i + 1 < len(command):
                current.append(command[i + 1])
                i += 1
            elif ch == quote:
                quote = ""
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            current.append(ch)
            i += 1
            continue
        operator = next((op for op in _CONTROL_OPERATORS if command.startswith(op, i)), None)
        if operator is not None:
            parts.append("".join(current))
            current = []
            i += len(operator)
            continue
        current.append(ch)
        i += 1
    parts.append("".join(current))
    return [part.strip() for part in parts if part.strip()]


def _js_number(value: float) -> str:
    """How JavaScript prints a parsed float: ``110``, never ``110.0``."""
    return str(int(value)) if value.is_integer() else repr(value)


def long_leading_sleep(command: str) -> str | None:
    """Claude Code's description of a command whose first subcommand is
    ``sleep N`` with ``N >= LONG_SLEEP_S`` — ``standalone sleep N`` or
    ``sleep N followed by: <the rest>`` — else ``None``."""
    parts = subcommands(command)
    if not parts:
        return None
    match = re.match(r"^sleep\s+(\d+(?:\.\d*)?)\s*$", parts[0])
    if match is None:
        return None
    seconds = float(match.group(1))
    if seconds < LONG_SLEEP_S:
        return None
    rest = " ".join(parts[1:]).strip()
    shown = _js_number(seconds)
    return f"sleep {shown} followed by: {rest}" if rest else f"standalone sleep {shown}"


# ---------------------------------------------------------------------------
# Git hardening
# ---------------------------------------------------------------------------


#: Per-subcommand flags that switch off the helper programs a repository's
#: own ``.git/config`` can name: the ``core.fsmonitor`` hook, an external diff
#: driver and ``textconv`` filters. Global ``-c`` flags go before the
#: subcommand, the rest right after it. Residual: a ``filter.<driver>.clean``
#: program has no global off switch.
_GIT_HARDENING: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "status": (("-c", "core.fsmonitor="), ()),
    "diff": (("-c", "core.fsmonitor="), ("--no-ext-diff", "--no-textconv")),
    "log": ((), ("--no-ext-diff", "--no-textconv")),
}


def harden_git(argv: list[str]) -> list[str]:
    """``git <sub> ...`` with :data:`_GIT_HARDENING` applied."""
    before, after = _GIT_HARDENING[argv[1]]
    return [argv[0], *before, argv[1], *after, *argv[2:]]
