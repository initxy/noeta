"""A shell command's words, redirections and control operators as bash reads
them, for judging a command without running it.

Only what a judgment needs: quoting, the operators that split subcommands,
redirections (attached or not: ``x>f`` writes ``f``), and what makes the
text say less than what runs. Command and process substitution, backticks,
heredocs, subshells, brace groups, comments, named file descriptors and
unbalanced quotes make the whole command *opaque*; a word that expands
(a ``$`` outside single quotes, an unquoted glob, brace or tilde) is marked
so a caller can refuse it. Nothing here runs anything.
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = [
    "Command",
    "Parsed",
    "Redirect",
    "Word",
    "harmless_redirect",
    "hides_effects",
    "parse",
]

#: Control operators, longest first: where a command splits.
_CONTROL = ("&&", "||", ";;", "|&", ";", "|", "&", "\n")
_GLOB = frozenset("*?[")
_BLANK = frozenset(" \t")


@dataclass(frozen=True)
class Word:
    """One word after quote removal, with what bash would expand in it."""

    text: str
    #: A ``$`` outside single quotes (a parameter, ``$'...'``).
    expands: bool = False
    #: An unquoted ``*``, ``?`` or ``[``.
    glob: bool = False
    #: The word starts with an unquoted glob character (it may expand to a
    #: name that reads as a flag).
    lead_glob: bool = False
    #: An unquoted ``{`` or ``}`` (brace expansion).
    brace: bool = False
    #: An unquoted leading ``~``.
    tilde: bool = False

    @property
    def plain(self) -> bool:
        """Bash passes the word on exactly as :attr:`text` reads."""
        return not (self.expands or self.glob or self.brace or self.tilde)


@dataclass(frozen=True)
class Redirect:
    """``fd`` (digits, ``&`` for ``&>``, or empty) ``op`` ``target``."""

    fd: str
    op: str
    target: Word


@dataclass
class Command:
    """One simple command: its words, its redirections, its text and the
    control operators around it (``""`` at either end of the line)."""

    words: list[Word] = field(default_factory=list)
    redirects: list[Redirect] = field(default_factory=list)
    text: str = ""
    before: str = ""
    after: str = ""

    @property
    def argv(self) -> list[str]:
        return [w.text for w in self.words]


@dataclass
class Parsed:
    commands: list[Command]
    #: What runs cannot be read off the text (see the module docstring).
    opaque: bool = False


class _Opaque(Exception):
    pass


def harmless_redirect(r: Redirect) -> bool:
    """A redirection that writes no file and reads none: output to
    ``/dev/null``, or a descriptor duplicated or closed."""
    if r.op in (">", ">>", ">|", "&>", "&>>"):
        return r.target.plain and r.target.text == "/dev/null"
    if r.op in (">&", "<&"):
        return r.target.plain and (r.target.text.isdigit() or r.target.text == "-")
    return False


def hides_effects(command: str) -> bool:
    """``command`` does something its words do not show: it is opaque, or
    redirects anything to or from a file."""
    parsed = parse(command)
    return parsed.opaque or any(not harmless_redirect(r) for c in parsed.commands
                                for r in c.redirects)


class _Lexer:
    def __init__(self, text: str) -> None:
        self.s = text
        self.i = 0
        self.commands: list[Command] = []
        self.cur = Command()
        self.start = 0
        self.op_before = ""
        self.redirect: tuple[str, str] | None = None
        self._reset_word()

    def _reset_word(self) -> None:
        self.buf: list[str] = []
        self.in_word = False
        self.quoted = False
        self.expands = False
        self.glob = False
        self.lead_glob = False
        self.brace = False
        self.tilde = False

    def _end_word(self) -> None:
        if not self.in_word:
            return
        word = Word("".join(self.buf), self.expands, self.glob, self.lead_glob, self.brace,
                    self.tilde)
        if self.redirect is not None:
            fd, op = self.redirect
            self.cur.redirects.append(Redirect(fd, op, word))
            self.redirect = None
        else:
            self.cur.words.append(word)
        self._reset_word()

    def _end_command(self, op: str, at: int) -> None:
        self._end_word()
        if self.redirect is not None:
            raise _Opaque
        self.cur.text = self.s[self.start:at].strip()
        self.cur.before, self.cur.after = self.op_before, op
        if self.cur.words or self.cur.redirects:
            self.commands.append(self.cur)
        self.cur = Command()
        self.op_before = op

    def _char(self, ch: str, *, quoted: bool) -> None:
        self.in_word = True
        self.buf.append(ch)
        if quoted:
            self.quoted = True

    def run(self) -> list[Command]:
        s, n = self.s, len(self.s)
        while self.i < n:
            ch = s[self.i]
            if ch == "\\":
                if self.i + 1 < n and s[self.i + 1] == "\n":
                    self.i += 2  # a line continuation
                    continue
                if self.i + 1 >= n:
                    raise _Opaque
                self._char(s[self.i + 1], quoted=True)
                self.i += 2
                continue
            if ch == "'":
                end = s.find("'", self.i + 1)
                if end < 0:
                    raise _Opaque
                self.in_word = True
                self.quoted = True
                self.buf.append(s[self.i + 1:end])
                self.i = end + 1
                continue
            if ch == '"':
                self._double()
                continue
            if ch == "`":
                raise _Opaque
            if ch == "$":
                nxt = s[self.i + 1] if self.i + 1 < n else ""
                if nxt == "(":
                    raise _Opaque  # command substitution or arithmetic
                self.expands = True
                self._char(ch, quoted=False)
                self.i += 1
                continue
            if ch in _BLANK:
                self._end_word()
                self.i += 1
                continue
            op = next((o for o in _CONTROL if s.startswith(o, self.i)), None)
            if op is not None and not (op == "&" and s.startswith("&>", self.i)):
                self._end_command(op, self.i)
                self.i += len(op)
                self.start = self.i
                continue
            if ch in "<>" or s.startswith("&>", self.i):
                self._redirect()
                continue
            if ch in "()":
                raise _Opaque  # a subshell, a function or process substitution
            if ch == "#" and not self.in_word:
                raise _Opaque  # a comment
            if ch in "{}":
                if not self.in_word and (self.i + 1 >= n or s[self.i + 1] in " \t\n;&|"):
                    raise _Opaque  # a brace group
                self.brace = True
            elif ch in _GLOB:
                self.glob = True
                if not self.in_word:
                    self.lead_glob = True
            elif ch == "~" and not self.in_word:
                self.tilde = True
            self._char(ch, quoted=False)
            self.i += 1
        self._end_command("", n)
        return self.commands

    def _double(self) -> None:
        s, n = self.s, len(self.s)
        self.in_word = True
        self.quoted = True
        self.i += 1
        while self.i < n:
            ch = s[self.i]
            if ch == '"':
                self.i += 1
                return
            if ch == "\\" and self.i + 1 < n and s[self.i + 1] in '"\\$`\n':
                if s[self.i + 1] != "\n":
                    self.buf.append(s[self.i + 1])
                self.i += 2
                continue
            if ch == "`" or s.startswith("$(", self.i):
                raise _Opaque
            if ch == "$":
                self.expands = True
            self.buf.append(ch)
            self.i += 1
        raise _Opaque

    def _redirect(self) -> None:
        s = self.s
        fd = ""
        if self.in_word:
            text = "".join(self.buf)
            if not text.isdigit() or self.quoted or self.expands:
                if text.startswith("{") and text.endswith("}"):
                    raise _Opaque  # a named descriptor
                self._end_word()
            else:
                fd = text
                self._reset_word()
        if self.redirect is not None:
            raise _Opaque
        for op in ("&>>", "&>", "<<<", "<<", "<>", "<&", "<(", "<", ">>", ">|", ">&", ">(",
                   ">"):
            if s.startswith(op, self.i):
                break
        if op in ("<<<", "<<", "<(", ">("):
            raise _Opaque  # a heredoc, a here-string or process substitution
        if op.startswith("&"):
            if fd:
                raise _Opaque
            fd = "&"
        self.i += len(op)
        self.redirect = (fd, op)


def parse(command: str) -> Parsed:
    """``command``'s simple commands, in order; ``opaque`` (with no
    commands) when what runs cannot be read off the text."""
    try:
        return Parsed(_Lexer(command).run())
    except _Opaque:
        return Parsed([], opaque=True)
