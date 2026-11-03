"""The paths a shell command's own words name for writing and for reading,
as Claude Code's shell path check reads them (2.1.312): the programs it
checks, and for each where its paths sit among the arguments.

Only deny rules use these (``Edit`` rules the writes, ``Read`` rules the
reads), so a word read as a path that is not one can only refuse more.
Words are returned as written; the caller resolves them and leaves out
what bash would expand.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence

from noeta.toolkit.shell_syntax import Word

__all__ = ["READERS", "WRITERS", "command_paths", "unwrap"]

Args = Sequence[Word]

_ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_DURATION = re.compile(r"^\d+(?:\.\d+)?[smhd]?$")


def unwrap(words: Args) -> list[Word]:
    """``words`` without leading assignments and the programs that only run
    the rest (``time``, ``nohup``, ``timeout <duration>``, ``nice``,
    ``stdbuf``, ``env``, ``command``, ``builtin``, ``noglob``)."""
    w = list(words)
    while w:
        head = w[0].text
        if _ENV_ASSIGN.match(head):
            w = w[1:]
        elif head in ("time", "nohup"):
            w = w[2:] if w[1:2] and w[1].text == "--" else w[1:]
        elif head == "timeout":
            i = 1
            while i < len(w) and w[i].text.startswith("-"):
                i += 2 if w[i].text in ("-s", "--signal", "-k", "--kill-after") else 1
            if i >= len(w) or not _DURATION.match(w[i].text):
                return w
            w = w[i + 1:]
        elif head == "nice":
            if w[1:2] and w[1].text == "-n" and len(w) > 2:
                w = w[3:]
            elif w[1:2] and re.match(r"^-\d+$", w[1].text):
                w = w[2:]
            else:
                w = w[1:]
            if w and w[0].text == "--":
                w = w[1:]
        elif head == "stdbuf":
            i = 1
            while i < len(w) and w[i].text.startswith("-"):
                i += 2 if w[i].text in ("-i", "-o", "-e") else 1
            w = w[i:]
        elif head == "env":
            i = 1
            while i < len(w) and (w[i].text.startswith("-") or _ENV_ASSIGN.match(w[i].text)):
                i += 2 if w[i].text in ("-u", "--unset", "-C", "--chdir", "-S") else 1
            w = w[i:]
        elif head == "command":
            i = 1
            while i < len(w) and re.match(r"^-p+$", w[i].text):
                i += 1
            if i < len(w) and w[i].text == "--":
                i += 1
            if i >= len(w) or w[i].text.startswith("-"):
                return w
            w = w[i:]
        elif head in ("builtin", "noglob"):
            w = w[1:]
        else:
            return w
    return w


def _positional(args: Args) -> list[Word]:
    """Flags are skipped up to the first operand; from there every word
    counts (``--`` ends the flags)."""
    out: list[Word] = []
    started = ended = False
    for a in args:
        if started or ended:
            out.append(a)
        elif a.text == "--":
            ended = True
        elif a.text == "-" or not a.text.startswith("-"):
            out.append(a)
            started = True
    return out


def _pattern_first(valued: frozenset[str], files: frozenset[str] = frozenset(),
                   default: tuple[str, ...] = ()) -> Callable[[Args], list[Word]]:
    """A search's paths: its first operand is the pattern unless ``-e`` /
    ``-f`` gave one; ``-f`` and the ``files`` flags name read files."""
    def paths(args: Args) -> list[Word]:
        out: list[Word] = []
        have_pattern = ended = seen_operand = False
        i = 0
        while i < len(args):
            a = args[i].text
            if not ended and not seen_operand and a == "--":
                ended = True
            elif not ended and not seen_operand and a != "-" and a.startswith("-"):
                name, eq, value = a.partition("=")
                if name in ("-e", "--regexp", "-f", "--file"):
                    have_pattern = True
                    if name in ("-f", "--file"):
                        if eq:
                            out.append(Word(value))
                        elif i + 1 < len(args):
                            out.append(args[i + 1])
                elif re.match(r"^-[a-zA-Z]*f$", name) and not eq and i + 1 < len(args):
                    have_pattern = True
                    out.append(args[i + 1])
                    i += 2
                    continue
                elif name in files:
                    if eq:
                        out.append(Word(value))
                    elif i + 1 < len(args):
                        out.append(args[i + 1])
                        i += 1
                    i += 1
                    continue
                elif not eq and a.startswith("-f") and len(a) > 2:
                    have_pattern = True
                    out.append(Word(a[2:]))
                    i += 1
                    continue
                elif not eq and a.startswith("-e") and len(a) > 2:
                    have_pattern = True
                    i += 1
                    continue
                if name in valued and not eq:
                    i += 1
            elif not have_pattern:
                have_pattern = seen_operand = True
            else:
                seen_operand = True
                out.append(args[i])
            i += 1
        return out or [Word(d) for d in default]
    return paths


_GREP_VALUED = frozenset({"-e", "--regexp", "-f", "--file", "--exclude", "--include",
                          "--exclude-dir", "--include-dir", "-m", "--max-count", "-A",
                          "--after-context", "-B", "--before-context", "-C", "--context"})
_RG_VALUED = frozenset({"-e", "--regexp", "-f", "--file", "-t", "--type", "-T", "--type-not",
                        "-g", "--glob", "-m", "--max-count", "--max-depth", "-r", "--replace",
                        "-A", "--after-context", "-B", "--before-context", "-C", "--context"})
_GIT_GREP_VALUED = frozenset({"-e", "--regexp", "-f", "--file", "-A", "--after-context", "-B",
                              "--before-context", "-C", "--context", "-m", "--max-count",
                              "--max-depth", "--threads"})
_SEARCH_FILES = frozenset({"--exclude-from", "--include-from", "--ignore-file"})


def _grep(args: Args) -> list[Word]:
    found = _pattern_first(_GREP_VALUED, _SEARCH_FILES)(args)
    if not found and any(a.text in ("-r", "-R", "--recursive") for a in args):
        return [Word(".")]
    return found


def _program_first(valued: frozenset[str], program_flags: frozenset[str],
                   file_flags: frozenset[str]) -> Callable[[Args], list[Word]]:
    """``awk`` / ``jq``: the first operand is the program unless a flag gave
    one; a program file counts as read."""
    def paths(args: Args) -> list[Word]:
        out: list[Word] = []
        have_program = ended = seen_operand = False
        i = 0
        while i < len(args):
            a = args[i].text
            if not ended and not seen_operand and a == "--":
                ended = True
            elif not ended and not seen_operand and a != "-" and a.startswith("-"):
                name, eq, value = a.partition("=")
                if name in program_flags:
                    have_program = True
                if name in file_flags:
                    have_program = True
                    if eq:
                        out.append(Word(value))
                    elif i + 1 < len(args):
                        out.append(args[i + 1])
                        i += 1
                elif name in ("--slurpfile", "--rawfile"):
                    if i + 2 < len(args):
                        out.append(args[i + 2])
                    i += 2
                elif name in valued and not eq:
                    i += 1
            elif not have_program:
                have_program = seen_operand = True
            else:
                seen_operand = True
                out.append(args[i])
            i += 1
        return out
    return paths


_awk = _program_first(frozenset({"-F", "--field-separator", "-v", "--assign", "-e", "--source"}),
                      frozenset({"-e", "--source"}), frozenset({"-f", "--file", "-E", "--exec"}))
_jq = _program_first(frozenset({"-e", "--expression", "--arg", "--argjson", "--args",
                                "--jsonargs", "-L", "--library-path", "--indent", "--tab"}),
                     frozenset({"-e", "--expression"}), frozenset({"-f", "--from-file"}))


def _sed(args: Args) -> list[Word]:
    """The files ``sed`` reads (and with ``-i`` writes): operands after the
    script, and a script file."""
    out: list[Word] = []
    have_script = ended = seen_operand = False
    i = 0
    while i < len(args):
        a = args[i].text
        if not ended and not seen_operand and a == "--":
            ended = True
        elif not ended and not seen_operand and a != "-" and a.startswith("-"):
            if a in ("-f", "--file"):
                if i + 1 < len(args):
                    out.append(args[i + 1])
                i += 1
                have_script = True
            elif a in ("-e", "--expression"):
                i += 1
                have_script = True
            elif "e" in a or "f" in a:
                have_script = True
        else:
            seen_operand = True
            if not have_script:
                have_script = True
            else:
                out.append(args[i])
        i += 1
    return out


def _sed_in_place(args: Args) -> bool:
    for a in args:
        t = a.text
        if t == "--":
            return False
        if t.startswith("--in-place") or (t.startswith("-") and not t.startswith("--")
                                         and "i" in t):
            return True
    return False


def _find(args: Args) -> list[Word]:
    valued = {"-newer", "-anewer", "-cnewer", "-mnewer", "-samefile", "-path", "-wholename",
              "-ilname", "-lname", "-ipath", "-iwholename"}
    out: list[Word] = []
    in_expr = ended = False
    i = 0
    while i < len(args):
        a = args[i].text
        if ended:
            out.append(args[i])
        elif a == "--":
            ended = True
        elif a.startswith("-"):
            if a not in ("-H", "-L", "-P"):
                in_expr = True
                if (a in valued or re.match(r"^-newer[acmBt][acmtB]$", a)) and i + 1 < len(args):
                    out.append(args[i + 1])
                    i += 1
        elif not in_expr:
            out.append(args[i])
        i += 1
    return out or [Word(".")]


def _git(args: Args) -> list[Word]:
    """``git grep`` and ``git diff`` paths (under ``-C`` / ``--work-tree``)."""
    base = ""
    i = 0
    while i < len(args) and args[i].text.startswith("-"):
        a = args[i].text
        if a.startswith("--work-tree="):
            base = _join(base, a.partition("=")[2])
        elif a in ("-C", "--work-tree") and i + 1 < len(args):
            base = _join(base, args[i + 1].text)
            i += 1
        elif a in ("-c", "--git-dir", "--namespace", "--super-prefix", "--config-env",
                   "--attr-source", "--shallow-file") and i + 1 < len(args):
            i += 1
        i += 1
    if i >= len(args):
        return []
    sub, rest = args[i].text, args[i + 1:]

    def under(w: Word) -> Word:
        return w if not base else Word(_join(base, w.text), w.expands, w.glob, w.lead_glob,
                                       w.brace, w.tilde and not base)
    if sub == "grep":
        found = _pattern_first(_GIT_GREP_VALUED)(rest)
        return [under(w) for w in found] if found else [Word(base or ".")]
    if sub != "diff":
        return []
    out: list[Word] = []
    ended = False
    for w in rest:
        if not ended and w.text == "--":
            ended = True
        elif ended or w.text == "-" or not w.text.startswith("-"):
            out.append(under(w))
    return out


def _join(base: str, path: str) -> str:
    if not base or path.startswith(("/", "~")):
        return path
    return base.rstrip("/") + "/" + path


def _man(args: Args) -> list[Word]:
    local = any(a.text == "--local-file" or (re.match(r"^-[^-]", a.text) and "l" in a.text)
                for a in args)
    return [a for a in args if not a.text.startswith("-") and (
        local or "/" in a.text or a.text.startswith(("~", ".")))]


def _tr(args: Args) -> list[Word]:
    delete = any(a.text in ("-d", "--delete") or (a.text.startswith("-") and "d" in a.text)
                 for a in args)
    return _positional(args)[1 if delete else 2:]


def _cd(args: Args) -> list[Word]:
    found = _positional(args)
    return found[:1] if found else [Word("~", tilde=True)]


def _ls(args: Args) -> list[Word]:
    return _positional(args) or [Word(".")]


#: Programs whose operands Claude Code reads paths from (for reading), with
#: how. Its programs with a per-flag table (``cut``, ``paste``, ``column``,
#: ``tac``, ``fold``, ``expand``, ``unexpand``, ``fmt``, ``comm``, ``cmp``,
#: ``pr``, ``numfmt``) are read as plain operands here: a flag's value may
#: count as a path too (``parity/deviations.md``).
READERS: dict[str, Callable[[Args], list[Word]]] = {
    **{p: _positional for p in (
        "cat", "head", "tail", "sort", "uniq", "wc", "file", "stat", "diff", "strings",
        "hexdump", "od", "base64", "nl", "rev", "tsort", "sha256sum", "sha1sum", "md5sum",
        "cut", "paste", "column", "tac", "fold", "expand", "unexpand", "fmt", "comm", "cmp",
        "pr", "numfmt")},
    "cd": _cd, "ls": _ls, "find": _find, "awk": _awk, "gawk": _awk, "mawk": _awk,
    "nawk": _awk, "grep": _grep, "egrep": _grep, "fgrep": _grep,
    "rg": lambda a: _pattern_first(_RG_VALUED, _SEARCH_FILES, (".",))(a),
    "sed": _sed, "jq": _jq, "git": _git, "man": _man, "tr": _tr,
}

#: Programs whose operands Claude Code holds to ``Edit`` deny rules.
WRITERS = frozenset({"mkdir", "touch", "rm", "rmdir", "mv", "cp", "sed", "tee"})

_DEVICES = frozenset({"/dev/null", "/dev/stdout", "/dev/stderr", "/dev/tty"})


def command_paths(words: Args) -> tuple[list[Word], list[Word]]:
    """``(writes, reads)``: the words of one simple command that name paths
    it writes and paths it reads. A ``cp`` / ``mv`` source and a
    ``sed -i`` file count as both; ``sed`` without ``-i`` only reads."""
    w = unwrap(words)
    if not w or not w[0].plain:
        return [], []
    head, args = w[0].text, w[1:]
    if head == "sed":
        files = _sed(args)
        return (files, files) if _sed_in_place(args) else ([], files)
    if head in WRITERS:
        found = _positional(args)
        if head in ("cp", "mv"):
            sources = found[:-1] if not _target_dir_given(args) else found
            extra = [Word(a.text.partition("=")[2]) for a in args
                     if a.text.startswith("--target-directory=")]
            return found + extra, sources
        if head == "tee":
            found = [x for x in found if x.text not in _DEVICES]
        return found, []
    reader = READERS.get(head)
    return ([], reader(args)) if reader is not None else ([], [])


def _target_dir_given(args: Args) -> bool:
    for a in args:
        t = a.text
        if t.startswith("--"):
            if t[2:].partition("=")[0] and "target-directory".startswith(t[2:].partition("=")[0]):
                return True
        elif t.startswith("-") and t.find("t") > 0:
            return True
    return False
