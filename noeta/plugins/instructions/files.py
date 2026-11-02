"""Finding and reading instruction files the way Claude Code does, in
noeta's own places.

An instruction file is a ``NOETA.md`` (or ``.noeta/NOETA.md``,
``NOETA.local.md``, a rule under ``.noeta/rules``, an imported file) read
whole. Its text is trimmed, a leading YAML frontmatter is cut (a rule's
``paths:`` globs come from it) and HTML comments that stand as blocks of
their own are dropped (Claude Code's markdown lexer: a comment inside a
paragraph, inline code, a code block or a list item stays).
``@path`` mentions outside code import that file right after the one naming
it, with the same kind, up to :data:`IMPORT_DEPTH_MAX` levels deep; a path is
read at most once per discovery. An import from a project or local file must
stay, links resolved, inside the working directory or the folder of the file
that began the import chain (Claude Code asks before following one outside
the working directory; noeta has no such prompt and skips it).

Everything is read through an ExecEnv: the task's for repository files, a
local one for the user's and the machine's own directories.
"""

from __future__ import annotations

import logging
import os
import posixpath
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field

import yaml

from noeta.core.execenv import ExecEnv
from noeta.toolkit.context import read_text
from noeta.toolkit.tools import path_within

log = logging.getLogger("noeta.instructions")

#: Kinds of instruction file, in the order a conversation's start lists them.
MANAGED = "managed"
USER = "user"
PROJECT = "project"
LOCAL = "local"
MEMORY = "memory"

#: The project files (Claude Code's ``CLAUDE.md`` and ``.claude/CLAUDE.md``
#: in noeta's places), read in each directory from the outermost ancestor of
#: the working directory down to it. Claude Code's own files are never read.
PROJECT_FILES = ("NOETA.md", ".noeta/NOETA.md")
LOCAL_FILE = "NOETA.local.md"
RULES_DIR = ".noeta/rules"
#: Read in a directory that has none of :data:`PROJECT_FILES`, the first
#: non-blank one only: the cross-vendor convention.
FALLBACK_FILES = ("AGENTS.md",)
#: An imported file at this depth is not read (the file a directory names is
#: depth 0), as in Claude Code.
IMPORT_DEPTH_MAX = 5
#: An import whose extension is not one of these is skipped (Claude Code's
#: list of text extensions); a name with no extension is read.
TEXT_EXTENSIONS = frozenset((
    ".md .txt .text .json .yaml .yml .toml .xml .csv .html .htm .css .scss .sass .less .js "
    ".ts .tsx .jsx .mjs .cjs .mts .cts .py .pyi .pyw .rb .erb .rake .go .rs .java .kt .kts "
    ".scala .c .cpp .cc .cxx .h .hpp .hxx .cs .swift .sh .bash .zsh .fish .ps1 .bat .cmd .env "
    ".ini .cfg .conf .config .properties .sql .graphql .gql .proto .vue .svelte .astro .ejs "
    ".hbs .pug .jade .php .pl .pm .lua .r .dart .ex .exs .erl .hrl .clj .cljs .cljc .edn .hs "
    ".lhs .elm .ml .mli .f .f90 .f95 .for .cmake .make .makefile .gradle .sbt .rst .adoc "
    ".asciidoc .org .tex .latex .lock .log .diff .patch").split())
#: Bound on one ``find`` listing of a rules directory.
_LIST_OUTPUT_CAP = 4 * 1024 * 1024

_FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
_COMMENT = re.compile(r"<!--.*?-->", re.S)
_INLINE_CODE = re.compile(r"`+[^`\n]*`+")
#: A line opening a comment block: at most three spaces, then ``<!--``.
_COMMENT_START = re.compile(r"^ {0,3}<!--")
_LIST_ITEM = re.compile(r"^ {0,3}(?:[*+-]|\d{1,9}[.)])(?:[ \t]|$)")
_IMPORT = re.compile(r"(?:^|\s)@((?:[^\s\\]|\\ )+)")
_FRONTMATTER = re.compile(r"\A---[ \t]*\n(.*?)\n---[ \t]*(?:\n|\Z)", re.S)


@dataclass
class InstructionFile:
    """One file as read: its absolute path, kind, text (cleaned), the globs
    that make it conditional (a rule with ``paths:``), and the file that
    imported it."""

    path: str
    kind: str
    text: str
    globs: tuple[str, ...] = ()
    parent: str | None = None


@dataclass
class Reader:
    """One discovery: the env repository files are read through, a local env
    for the user's and the machine's directories, and the paths read so far
    (a file is read once, whatever names it)."""

    env: ExecEnv
    local: ExecEnv
    cwd: str
    seen: set[str] = field(default_factory=set)

    def env_for(self, kind: str) -> ExecEnv:
        return self.local if kind in (MANAGED, USER) else self.env

    async def _canonical(self, env: ExecEnv, path: str) -> str:
        try:
            return await env.realpath(path)
        except OSError:
            return posixpath.normpath(path)

    async def file(self, path: str, kind: str, *, fence: str | None = None,
                   depth: int = 0, parent: str | None = None) -> list[InstructionFile]:
        """``path`` and the files it imports, depth first; ``[]`` when it is
        missing, blank, already read or too deep."""
        if depth >= IMPORT_DEPTH_MAX:
            return []
        extension = posixpath.splitext(path)[1].lower()
        if parent is not None and extension and extension not in TEXT_EXTENSIONS:
            log.info("instructions: %s imports %s, not a text file; skipped", parent, path)
            return []
        env = self.env_for(kind)
        real = await self._canonical(env, path)
        if real in self.seen:
            return []
        raw = await read_text(env, path)
        if raw is None:
            return []
        self.seen.add(real)
        text, globs = clean(raw)
        if not text:
            return []
        out = [InstructionFile(path=path, kind=kind, text=text, globs=globs, parent=parent)]
        base = fence if fence is not None else posixpath.dirname(path)
        # Matched resolved: a link inside the repository may lead out of it.
        within = [await self._canonical(env, d) for d in (base, self.cwd)] \
            if kind not in (MANAGED, USER) else []
        for target in imports(raw, posixpath.dirname(path)):
            real_target = await self._canonical(env, target) if within else target
            if within and not any(path_within(real_target, d) for d in within):
                log.info("instructions: %s imports %s, outside %s; skipped", path, target, base)
                continue
            out += await self.file(target, kind, fence=base, depth=depth + 1, parent=path)
        return out

    async def rules(self, directory: str, kind: str, *,
                    match: str | None = None) -> list[InstructionFile]:
        """The rules (every ``*.md`` under ``directory``, subdirectories
        too, sorted by path) with their imports: those without ``paths:``,
        or, with ``match``, those whose globs match that relative path."""
        out: list[InstructionFile] = []
        env = self.env_for(kind)
        for path in await list_markdown(env, directory):
            if await self._canonical(env, path) in self.seen:
                continue
            raw = await read_text(env, path)
            if raw is None:
                continue
            _text, globs = clean(raw)
            if (match is None and not globs) or (
                    match is not None and globs and matches(globs, match)):
                out += await self.file(path, kind)
        return out

    async def directory(self, directory: str, fallbacks: Sequence[str]) -> list[InstructionFile]:
        """A directory's project files (or its first fallback file), then its
        local file."""
        out: list[InstructionFile] = []
        found = False
        for name in PROJECT_FILES:
            path = posixpath.join(directory, name)
            if await read_text(self.env, path) is not None:
                found = True
            out += await self.file(path, PROJECT)
        if not found:
            for name in fallbacks:
                got = await self.file(posixpath.join(directory, name), PROJECT)
                if got:
                    out += got
                    break
        return out


def clean(raw: str) -> tuple[str, tuple[str, ...]]:
    """The text a file contributes and its ``paths:`` globs."""
    text = raw
    globs: tuple[str, ...] = ()
    m = _FRONTMATTER.match(text)
    if m:
        try:
            meta = yaml.safe_load(m.group(1))
        except yaml.YAMLError:
            meta = None
        if isinstance(meta, dict):
            globs = _globs(meta.get("paths"))
            text = text[m.end():]
    if "<!--" in text:
        text = _drop_comment_blocks(text)
    return text.strip(), globs


def _drop_comment_blocks(text: str) -> str:
    """``text`` without its top-level HTML comment blocks, as Claude Code's
    markdown lexer finds them: a line that opens with ``<!--`` (at most
    three spaces in, outside a fenced block and a list item) starts a block
    that runs to the line holding ``-->``. Its comments are cut; a block
    with nothing else left goes with the empty lines after it, one with
    text after a comment keeps that text. An unclosed comment and a comment
    inside a paragraph stay."""
    lines = text.split("\n")
    out: list[str] = []
    fence: str | None = None
    in_list = False
    after_blank = True
    i = 0
    while i < len(lines):
        line = lines[i]
        if fence is not None:
            out.append(line)
            fence = None if line.strip().startswith(fence) else fence
            i += 1
            continue
        if not line.strip():
            out.append(line)
            after_blank = True
            i += 1
            continue
        indent = len(line) - len(line.lstrip(" "))
        if _LIST_ITEM.match(line):
            in_list = True
        elif indent == 0 and after_blank:
            in_list = False
        after_blank = False
        opened = _FENCE.match(line)
        if opened:
            fence = opened.group(1)[0] * 3
            out.append(line)
            i += 1
            continue
        if not _COMMENT_START.match(line) or (in_list and indent > 0):
            out.append(line)
            i += 1
            continue
        end, close = i, line.find("-->", line.index("<!--") + 4)
        while close < 0 and end + 1 < len(lines):
            end += 1
            close = lines[end].find("-->")
        if close < 0:
            out += lines[i:]
            break
        rest = _COMMENT.sub("", "\n".join(lines[i:end + 1]))
        i = end + 1
        if rest.strip():
            out += rest.split("\n")
            continue
        while i < len(lines) and lines[i] == "":
            i += 1
        after_blank = True
    return "\n".join(out)


def _globs(value: object) -> tuple[str, ...]:
    items: Iterable[object]
    if isinstance(value, str):
        items = value.split(",")
    elif isinstance(value, list):
        items = value
    else:
        return ()
    return tuple(s for s in (str(i).strip() for i in items) if s)


def _outside_code(lines: list[str], change: Callable[[str], str]) -> list[str]:
    """``lines`` with ``change`` applied to every run of lines outside fenced
    code blocks."""
    out: list[str] = []
    chunk: list[str] = []
    fence: str | None = None
    for line in lines:
        m = _FENCE.match(line)
        if fence is None and m:
            if chunk:
                out += change("\n".join(chunk)).split("\n")
                chunk = []
            fence = m.group(1)[0] * 3
            out.append(line)
        elif fence is not None:
            out.append(line)
            if line.strip().startswith(fence):
                fence = None
        else:
            chunk.append(line)
    if chunk:
        out += change("\n".join(chunk)).split("\n")
    return out


def imports(raw: str, directory: str) -> list[str]:
    """The absolute paths ``raw``'s ``@path`` mentions name (outside code
    blocks, inline code and HTML comments), resolved against ``directory``."""
    found: list[str] = []

    def scan(text: str) -> str:
        text = _INLINE_CODE.sub(" ", _COMMENT.sub(" ", text))
        for m in _IMPORT.finditer(text):
            target = m.group(1).split("#", 1)[0].replace("\\ ", " ")
            if not target or not _importable(target):
                continue
            if target.startswith("~/"):
                resolved = os.path.expanduser(target)
            elif target.startswith("/"):
                resolved = target
            else:
                resolved = posixpath.join(directory, target)
            resolved = posixpath.normpath(resolved)
            if resolved not in found:
                found.append(resolved)
        return text

    _outside_code(raw.split("\n"), scan)
    return found


def _importable(target: str) -> bool:
    if target.startswith(("./", "~/")):
        return True
    if target.startswith("/"):
        return target != "/"
    return (not target.startswith("@") and re.match(r"^[#%^&*()]+", target) is None
            and re.match(r"^[a-zA-Z0-9._-]", target) is not None)


async def list_markdown(env: ExecEnv, directory: str) -> list[str]:
    """Every ``*.md`` file under ``directory``, sorted; ``[]`` when it is not
    a directory."""
    try:
        st = await env.stat(directory)
    except OSError:
        return []
    if st is None or not st.is_dir:
        return []
    try:
        out = await env.run(["find", "-L", directory, "-type", "f", "-name", "*.md"],
                            timeout=30, output_cap=_LIST_OUTPUT_CAP)
    except (OSError, ValueError) as exc:
        log.warning("instructions: listing %s failed (%s); skipped", directory, exc)
        return []
    return sorted(line for line in out.stdout.decode("utf-8", errors="replace").split("\n")
                  if line)


# ------------------------------------------------------------------ globs


def _expand_braces(pattern: str) -> list[str]:
    m = re.search(r"\{([^{}]*)\}", pattern)
    if m is None:
        return [pattern]
    head, tail = pattern[:m.start()], pattern[m.end():]
    return [p for alt in m.group(1).split(",") for p in _expand_braces(head + alt + tail)]


def _glob_regex(pattern: str) -> re.Pattern[str] | None:
    """A gitignore-style pattern as a regex over a ``/``-separated relative
    path: a pattern with no inner ``/`` matches at any depth, ``**`` spans
    directories, and a match on a directory covers everything below it."""
    p = pattern.strip()
    if not p or p.startswith("#"):
        return None
    anchored = "/" in p.rstrip("/")
    p = p.lstrip("/").rstrip("/")
    out = []
    i = 0
    while i < len(p):
        if p.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif p.startswith("/**", i) and i + 3 == len(p):
            out.append("(?:/.*)?")
            i += 3
        elif p.startswith("**", i):
            out.append(".*")
            i += 2
        elif p[i] == "*":
            out.append("[^/]*")
            i += 1
        elif p[i] == "?":
            out.append("[^/]")
            i += 1
        elif p[i] == "[":
            end = p.find("]", i + 1)
            if end < 0:
                out.append(re.escape(p[i]))
                i += 1
            else:
                body = p[i + 1:end]
                out.append("[" + ("^" + body[1:] if body.startswith("!") else body) + "]")
                i = end + 1
        else:
            out.append(re.escape(p[i]))
            i += 1
    prefix = "^" if anchored else "^(?:.*/)?"
    return re.compile(prefix + "".join(out) + "(?:/.*)?$")


def matches(globs: Sequence[str], rel: str) -> bool:
    """Whether the relative path ``rel`` matches any of ``globs``."""
    if not rel or rel.startswith("..") or posixpath.isabs(rel):
        return False
    for glob in globs:
        for expanded in _expand_braces(glob):
            rx = _glob_regex(expanded)
            if rx is not None and rx.match(rel):
                return True
    return False
