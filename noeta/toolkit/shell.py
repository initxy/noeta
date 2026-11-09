"""Pure shell-command classification: parsing, the metacharacter and
expansion checks, the wrapper programs, subcommand splitting, Claude Code's
long-leading-``sleep`` check, and the ``git`` flags that switch off a
repository's own helper programs.

Nothing here runs anything or reads a file.
"""

from __future__ import annotations

import re
import shlex
from typing import NamedTuple

__all__ = [
    "GIT_FILTER_DRIVERS",
    "GIT_READ_ENV",
    "GIT_READ_FLAGS",
    "LONG_SLEEP_S",
    "NO_OUTPUT",
    "SHELL_EXPANSION_CHARS",
    "SHELL_META_CHARS",
    "WRAPPER_PROGRAMS",
    "ShellRead",
    "harden_git",
    "has_shell_meta",
    "has_unquoted_expansion",
    "long_leading_sleep",
    "parse_argv",
    "read_files",
    "shell_reads",
    "subcommands",
    "untrusted_filters",
]


SHELL_META_CHARS = frozenset(";&|<>`$()\n\r")

#: The ``Bash`` result of a command that printed nothing.
NO_OUTPUT = "(Bash completed with no output)"

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
# Files a command reads
# ---------------------------------------------------------------------------


class ShellRead(NamedTuple):
    """One file a command showed (:func:`shell_reads`): lines ``start`` to
    ``end`` (``head``, ``sed -n``), the last ``tail`` lines (``tail``), or
    the whole file. ``numbered``: each line came with its number (``cat
    -n``, ``nl``). ``grep``: only the matching lines were shown, and only
    when the command found a match (it exited 0)."""

    path: str
    start: int | None = None
    end: int | None = None
    tail: int | None = None
    numbered: bool = False
    grep: bool = False

    def view(self, text: str) -> tuple[str, int, int] | None:
        """The part of the file ``text`` (``\\n`` line ends) this read
        showed, with its first line and line count (``0, 0``: the whole
        file); ``None`` when it showed nothing of it (a start past the end,
        a ``tail`` of an empty file)."""
        if self.tail is not None:
            lines = text.split("\n")
            if lines[-1] == "":
                lines.pop()
            if not lines:
                return None
            count = min(self.tail, len(lines))
            return "\n".join(lines[-count:]), len(lines) - count + 1, count
        if self.start is None:
            return text, 0, 0
        lines = text.split("\n")
        first = max(1, self.start)
        last = max(first, self.end or first)
        if first > len(lines):
            return None
        return "\n".join(lines[first - 1:last]), first, last - first + 1

    def shown_in(self, output: str, content: str) -> bool:
        """Whether ``output`` (what the command printed) holds ``content``,
        leading blank lines and trailing white space aside; for a numbered
        read, also once the line numbers are taken off."""
        wanted = re.sub(r"^(\s*\n)+", "", content).rstrip()
        output = output.replace("\r\n", "\n")
        if wanted in output:
            return True
        return self.numbered and wanted in "\n".join(
            _unnumbered_line(line) for line in output.split("\n"))


    def file_text(self, output: str) -> str:
        """What a whole-file read printed (``output``) as the file's text:
        without its line numbers when it was numbered."""
        output = output.replace("\r\n", "\n")
        if not self.numbered:
            return output
        return "\n".join(_unnumbered_line(line) for line in output.split("\n"))


def _unnumbered_line(line: str) -> str:
    numbered = re.match(r"^[ \t]*\d+[ \t]", line)
    if numbered is not None:
        return line[numbered.end():]
    return "" if re.fullmatch(r"[ \t]*", line) else line


#: Lines ``head`` and ``tail`` show when not told how many.
_DEFAULT_LINES = 10
#: Subcommands that may sit between reads without spoiling them.
_READ_FILLER = re.compile(r"^\s*(echo|printf|true|:)\b")
#: ``cat``-like programs and the flags each may carry.
_CAT_FLAGS: dict[str, frozenset[str]] = {
    "cat": frozenset({"-n", "--number"}),
    "nl": frozenset(),
    "bat": frozenset({"-n", "--number", "-p", "--plain"}),
    "batcat": frozenset({"-n", "--number", "-p", "--plain"}),
}
_SED_RANGE = re.compile(r"^(\d+)(,(\d+))?p$")
_GREP_CONTEXT = re.compile(r"^(-[ABC]\d+|--(after-context|before-context|context)=\d+)$")
_GREP_SHORT = re.compile(r"^-[niwxEFGPHh]+$")
_RG_SHORT = re.compile(r"^-[iSswxFnNHUP]+$")
_GREP_LONG = frozenset({
    "--line-number", "--ignore-case", "--word-regexp", "--line-regexp", "--extended-regexp",
    "--fixed-strings", "--basic-regexp", "--perl-regexp", "--with-filename", "--no-filename",
    "--color=never", "--color=auto"})
_RG_LONG = frozenset({
    "--ignore-case", "--smart-case", "--case-sensitive", "--word-regexp", "--line-regexp",
    "--fixed-strings", "--line-number", "--no-line-number", "--with-filename",
    "--no-filename", "--multiline", "--pcre2"})


def _sed_read(argv: list[str]) -> ShellRead | None:
    quiet, words = False, []
    for word in argv[1:]:
        if word.startswith("--"):
            if word.startswith("--in-place") or word == "--expression":
                return None
            quiet = quiet or word in ("--quiet", "--silent")
        elif word.startswith("-"):
            if "i" in word or word == "-e":
                return None
            quiet = quiet or "n" in word
        else:
            words.append(word)
    lines = _SED_RANGE.match(words[0]) if quiet and len(words) == 2 else None
    if lines is None:
        return None
    start = int(lines.group(1))
    return ShellRead(words[1], start=start, end=int(lines.group(3) or start))


def _cat_read(argv: list[str]) -> ShellRead | None:
    flags = _CAT_FLAGS[argv[0]]
    path = None
    for word in argv[1:]:
        if word.startswith("-") and word != "-":
            if word not in flags:
                return None
        elif path is not None:
            return None
        else:
            path = word
    if path is None or path == "-":
        return None
    return ShellRead(path, numbered=argv[0] == "nl" or "-n" in argv or "--number" in argv)


def _head_tail_read(argv: list[str]) -> ShellRead | None:
    path, count = None, None
    words = iter(argv[1:])
    for word in words:
        if word in ("-n", "--lines"):
            count = next(words, None)
            if count is None or not count.isdigit():
                return None
        elif word.startswith("--lines="):
            count = word.removeprefix("--lines=")
            if not count.isdigit():
                return None
        elif re.fullmatch(r"-n?\d+", word):
            count = word.lstrip("-n")
        elif word.startswith("-"):
            return None
        elif path is not None:
            return None
        else:
            path = word
    if path is None or path == "-" or (count is not None and int(count) == 0):
        return None
    lines = _DEFAULT_LINES if count is None else int(count)
    if argv[0] == "head":
        return ShellRead(path, start=1, end=lines)
    return ShellRead(path, tail=lines)


def _grep_read(argv: list[str]) -> str | None:
    rg = argv[0] == "rg"
    short, long = (_RG_SHORT, _RG_LONG) if rg else (_GREP_SHORT, _GREP_LONG)
    words: list[str] = []
    rest = iter(argv[1:])
    for word in rest:
        if word.startswith("-") and word != "-":
            if word in ("-A", "-B", "-C") or (rg and word in (
                    "--after-context", "--before-context", "--context")):
                value = next(rest, None)
                if value is None or not value.isdigit():
                    return None
            elif not (_GREP_CONTEXT.match(word) or short.match(word) or word in long):
                return None
        elif len(words) == 2:
            return None
        else:
            words.append(word)
    if len(words) != 2 or words[1] == "-" or any(c in words[1] for c in "*?[{"):
        return None
    return words[1]


def _one_read(part: str, alone: bool) -> ShellRead | None:
    argv = parse_argv(part)
    if not argv:
        return None
    name = argv[0]
    if name == "sed":
        return _sed_read(argv)
    if name in _CAT_FLAGS:
        return _cat_read(argv)
    if name in ("head", "tail"):
        return _head_tail_read(argv)
    if alone and name in ("grep", "egrep", "fgrep", "rg"):
        path = _grep_read(argv)
        return None if path is None else ShellRead(path, grep=True)
    return None


def shell_reads(command: str) -> list[ShellRead]:
    """The files ``command`` shows the model, as Claude Code counts a
    ``Bash`` call among the files read: ``cat`` / ``nl`` / ``bat`` of one
    file, ``head`` / ``tail`` of one file, ``sed -n 'N[,M]p' file``, and,
    as the whole command, ``grep`` / ``rg`` of a pattern in one file; joined
    by ``&&`` / ``;`` with nothing but ``echo`` / ``printf`` / ``true`` /
    ``:`` between them. A pipe or a redirection counts nothing. Each path
    is given as written, with the part of the file the command showed."""
    if any(c in command for c in "|<>"):
        return []
    parts = subcommands(command)
    out: list[ShellRead] = []
    for part in parts:
        read = _one_read(part, alone=len(parts) == 1)
        if read is not None:
            out.append(read)
        elif len(parts) > 1 and not _READ_FILLER.match(part):
            return []
    return out


def read_files(command: str) -> list[tuple[str, bool]]:
    """:func:`shell_reads` as ``(path, counts only when the command found a
    match)`` pairs (``grep`` exits 0 only then)."""
    return [(read.path, read.grep) for read in shell_reads(command)]


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


#: Transports git knows, each named so a repository's own
#: ``protocol.<name>.allow`` cannot outrank the ``never`` given for all. A
#: ``git-remote-<name>`` helper installed here is stopped only by
#: :data:`GIT_READ_ENV` (git 2.45 and later, or a patched older one).
_GIT_PROTOCOLS = ("ext", "ssh", "git", "file", "http", "https", "ftp", "ftps")
#: Global flags for the git commands noeta runs itself to read a repository's
#: state: no fsmonitor hook, no hooks, no signature program, no index refresh
#: written back, and no transport — a partial clone fetches a missing object
#: when a read meets one, through the repository's own ``core.sshCommand``,
#: ``ext::`` URL or credential helper.
GIT_READ_FLAGS = ("-c", "core.fsmonitor=", "-c", "core.hooksPath=/dev/null",
                  "-c", "log.showSignature=false", "-c", "protocol.allow=never",
                  *(f for name in _GIT_PROTOCOLS for f in ("-c", f"protocol.{name}.allow=never")),
                  "--no-optional-locks")
#: Environment for those commands: no fetch of a partial clone's missing
#: objects (git 2.45 and later), no transport git does not know by name.
GIT_READ_ENV = {"GIT_NO_LAZY_FETCH": "1", "GIT_PROTOCOL_FROM_USER": "0"}
#: ``git`` arguments that list every filter driver configured, each with the
#: config scope that sets it (:func:`untrusted_filters` reads the answer).
GIT_FILTER_DRIVERS = ("config", "--show-scope", "--get-regexp", r"^filter\.")
#: Config scopes a person set up themselves (a filter there is trusted).
_TRUSTED_SCOPES = ("system", "global")


def untrusted_filters(exit_code: int, output: str) -> bool:
    """Whether the answer to :data:`GIT_FILTER_DRIVERS` names a filter driver
    (whose ``clean`` program ``git status`` would run) set up anywhere but the
    system or global config: by the repository itself. A failure counts as yes."""
    if exit_code == 1 and not output.strip():
        return False  # no filter configured anywhere
    if exit_code != 0:
        return True
    return any(line.strip() and line.split("\t", 1)[0].strip() not in _TRUSTED_SCOPES
               for line in output.splitlines())
