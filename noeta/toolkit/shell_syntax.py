"""A shell command's words, redirections and control operators as bash reads
them, for judging a command without running it.

Only what a judgment needs: quoting, the operators that split subcommands,
redirections (attached or not: ``x>f`` writes ``f``), and what makes the
text say less than what runs. Command and process substitution, backticks,
heredocs, subshells, brace groups, comments, named file descriptors and
unbalanced quotes make the whole command *opaque*; a word that expands
(a ``$`` outside single quotes, an unquoted glob, brace or tilde) is marked
so a caller can refuse it. Nothing here runs anything.

``parse(command, lenient=True)`` reads past most of that for a caller that
only looks for what a command may touch (the deny rules): a comment is
skipped, a heredoc body is data, a subshell's and a brace group's commands
and those inside a command or process substitution are listed with the
rest (a substitution's word is marked as expanding), and a named descriptor
counts as one. Only unbalanced quotes or parentheses leave it opaque.
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
    def __init__(self, text: str, *, lenient: bool = False) -> None:
        self.s = text
        self.lenient = lenient
        #: Heredoc delimiters whose bodies start after the next newline:
        #: ``(word, strip leading tabs, body expands)``.
        self.heredocs: list[tuple[str, bool, bool]] = []
        #: Commands found inside substitutions (lenient).
        self.inner: list[Command] = []
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
                if not self.lenient:
                    raise _Opaque
                self._backticks()
                continue
            if ch == "$":
                nxt = s[self.i + 1] if self.i + 1 < n else ""
                if nxt == "(":
                    if not self.lenient:
                        raise _Opaque  # command substitution or arithmetic
                    self._substitution()
                    continue
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
                if op == "\n" and self.heredocs:
                    self._heredoc_bodies()
                continue
            if ch in "<>" or s.startswith("&>", self.i):
                self._redirect()
                continue
            if ch in "()":
                if not self.lenient:
                    raise _Opaque  # a subshell, a function or process substitution
                # A subshell's commands are listed with the rest.
                self._end_command(ch, self.i)
                self.i += 1
                self.start = self.i
                continue
            if ch == "#" and not self.in_word:
                if not self.lenient:
                    raise _Opaque  # a comment
                end = s.find("\n", self.i)
                self.i = n if end < 0 else end
                continue
            if ch in "{}":
                if not self.in_word and (self.i + 1 >= n or s[self.i + 1] in " \t\n;&|"):
                    if not self.lenient:
                        raise _Opaque  # a brace group
                    self._end_command(ch, self.i)
                    self.i += 1
                    self.start = self.i
                    continue
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

    def _inner(self, text: str) -> None:
        lexer = _Lexer(text, lenient=True)
        self.inner += [*lexer.run(), *lexer.inner]

    def _substitution(self, at: int | None = None) -> None:
        """``$(...)`` / ``$((...))`` (``at``: the ``(`` of a process
        substitution): its commands are listed, its word expands."""
        s = self.s
        start = self.i + 1 if at is None else at
        end = _closing(s, start)
        body = s[start + 1:end]
        if not (body.startswith("(") and body.endswith(")")):
            self._inner(body)  # not arithmetic
        self.in_word = True
        self.expands = True
        self.buf.append(s[self.i:end + 1])
        self.i = end + 1

    def _backticks(self) -> None:
        s = self.s
        j = self.i + 1
        while j < len(s) and s[j] != "`":
            j += 2 if s[j] == "\\" else 1
        if j >= len(s):
            raise _Opaque
        self._inner(s[self.i + 1:j].replace("\\`", "`"))
        self.in_word = True
        self.expands = True
        self.buf.append(s[self.i:j + 1])
        self.i = j + 1

    def _here_word(self, strip: bool) -> None:
        """A heredoc's delimiter: noted, its body skipped at the next newline."""
        s, n = self.s, len(self.s)
        while self.i < n and s[self.i] in _BLANK:
            self.i += 1
        word: list[str] = []
        quoted = False
        while self.i < n and s[self.i] not in " \t\n;&|<>()":
            ch = s[self.i]
            if ch in "'\"\\":
                quoted = True
            if ch in "'\"":
                end = s.find(ch, self.i + 1)
                if end < 0:
                    raise _Opaque
                word.append(s[self.i + 1:end])
                self.i = end + 1
            elif ch == "\\" and self.i + 1 < n:
                word.append(s[self.i + 1])
                self.i += 2
            else:
                word.append(ch)
                self.i += 1
        if not word:
            raise _Opaque
        self.cur.redirects.append(Redirect("", "<<", Word("".join(word))))
        self.heredocs.append(("".join(word), strip, not quoted))

    def _substitutions_in(self, text: str) -> None:
        """The commands of the substitutions in an unquoted heredoc's body."""
        i = 0
        while i < len(text):
            if text.startswith("\\", i):
                i += 2
            elif text.startswith("$(", i):
                end = _closing(text, i + 1)
                body = text[i + 2:end]
                if not (body.startswith("(") and body.endswith(")")):
                    self._inner(body)
                i = end + 1
            elif text[i] == "`":
                end = text.find("`", i + 1)
                if end < 0:
                    raise _Opaque
                self._inner(text[i + 1:end])
                i = end + 1
            else:
                i += 1

    def _heredoc_bodies(self) -> None:
        s = self.s
        for word, strip, expands in self.heredocs:
            body: list[str] = []
            while self.i < len(s):
                end = s.find("\n", self.i)
                line = s[self.i:] if end < 0 else s[self.i:end]
                self.i = len(s) if end < 0 else end + 1
                if (line.lstrip("\t") if strip else line) == word:
                    break
                body.append(line)
            if expands:
                self._substitutions_in("\n".join(body))
        self.heredocs = []
        self.start = self.i

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
                if not self.lenient:
                    raise _Opaque
                if ch == "`":
                    self._backticks()
                else:
                    self._substitution()
                continue
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
                    if not self.lenient:
                        raise _Opaque  # a named descriptor
                    fd = text
                    self._reset_word()
                else:
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
            if not self.lenient:
                raise _Opaque  # a heredoc, a here-string or process substitution
            if op in ("<(", ">("):
                # A word naming a pipe; its commands are listed.
                self._substitution(at=self.i + 1)
                return
            self.i += len(op)
            if op == "<<":
                strip = s.startswith("-", self.i)
                self.i += strip
                self._here_word(strip)
                return
            self.redirect = (fd, op)  # a here-string's word is data
            return
        if op.startswith("&"):
            if fd:
                raise _Opaque
            fd = "&"
        self.i += len(op)
        self.redirect = (fd, op)


def _closing(s: str, i: int) -> int:
    """The index of the ``)`` that closes the ``(`` at ``i`` (quotes and
    nesting respected); raises when there is none."""
    depth, n = 0, len(s)
    while i < n:
        ch = s[i]
        if ch == "\\":
            i += 2
            continue
        if ch == "'":
            end = s.find("'", i + 1)
            if end < 0:
                raise _Opaque
            i = end + 1
            continue
        if ch == '"':
            i += 1
            while i < n and s[i] != '"':
                i += 2 if s[i] == "\\" else 1
            if i >= n:
                raise _Opaque
            i += 1
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    raise _Opaque


def parse(command: str, *, lenient: bool = False) -> Parsed:
    """``command``'s simple commands, in order; ``opaque`` (with no
    commands) when what runs cannot be read off the text. ``lenient``: see
    the module docstring (the commands of substitutions follow the rest)."""
    try:
        lexer = _Lexer(command, lenient=lenient)
        return Parsed([*lexer.run(), *lexer.inner])
    except _Opaque:
        return Parsed([], opaque=True)
