"""noeta's model-visible texts are its own words: none repeats a run of
Claude Code's text.

    python -m scripts.parity.own_words                      # check (make check)
    python -m scripts.parity.own_words index ~/cc-recordings/2.1.312 \\
        ~/cc-recordings/2.1.312-r2 --out parity/2.1.312/cc-windows.bin

``index`` reads recordings (``<recording>/<session>/*.json``) and keeps, of
every text Claude Code wrote into a request (system blocks, tool
descriptions and schemas, tagged user parts such as reminders, short tool
results), only hashes: a 5-byte BLAKE2b digest of each :data:`WINDOW`-
character window starting at every :data:`STRIDE`-th character, whitespace
collapsed. No text of Claude Code's enters the repository.

The check reads every text in ``noeta/`` the model can see — string literals
of the Python modules (docstrings and comments left out) and the ``.md`` /
``.js`` resources — collapses whitespace the same way, and fails on any of
its windows found in an index. A run of :data:`SHARED` or more characters in
common always contains an indexed window (``SHARED - WINDOW >= STRIDE - 1``);
a shorter shared run is caught only when it happens to hold one. Literals in
:data:`ALLOWED` (tool and parameter names, protocol strings) are cut out
before the windows are taken.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import sys
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
#: The shortest shared run the check is sure to catch.
SHARED = 45
#: Characters per indexed window, and how far apart indexed windows start.
WINDOW, STRIDE = 43, 3
DIGEST = 5
INDEX_NAME = "cc-windows.bin"
ALLOWED = ROOT / "parity" / "own-words-allowed.txt"
#: Tool results longer than this are file contents or command output, not
#: Claude Code's wording.
MAX_RESULT_CHARS = 3000
_SPACE = re.compile(r"\s+")

assert SHARED - WINDOW >= STRIDE - 1


def normalize(text: str) -> str:
    return _SPACE.sub(" ", text)


def digest(window: str) -> bytes:
    return hashlib.blake2b(window.encode("utf-8"), digest_size=DIGEST).digest()


# ---------------------------------------------------------------- the index


def _strings(node: Any, out: list[str]) -> None:
    if isinstance(node, str):
        out.append(node)
    elif isinstance(node, dict):
        for key, value in node.items():
            if key not in ("signature", "data"):  # opaque blobs
                _strings(value, out)
    elif isinstance(node, list):
        for value in node:
            _strings(value, out)


def claude_code_texts(body: dict[str, Any]) -> Iterator[str]:
    """The texts of one recorded request that Claude Code wrote."""
    found: list[str] = []
    _strings(body.get("system"), found)
    _strings(body.get("tools"), found)
    for message in body.get("messages") or []:
        if message.get("role") != "user":
            continue  # the model's side
        content = message.get("content")
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        for block in content or []:
            parts: list[str] = []
            _strings(block, parts)
            if block.get("type") == "tool_result":
                found.extend(p for p in parts if len(p) <= MAX_RESULT_CHARS)
            else:
                found.extend(p for p in parts if p.lstrip().startswith("<"))
    yield from found


def index(recordings: Iterable[Path]) -> bytes:
    digests: set[bytes] = set()
    for recording in recordings:
        for path in sorted(recording.glob("*/*.json")):
            try:
                body = json.loads(path.read_text("utf-8")).get("body")
            except (ValueError, AttributeError):
                continue
            if not isinstance(body, dict):
                continue
            for text in set(claude_code_texts(body)):
                text = normalize(text)
                for i in range(0, len(text) - WINDOW + 1, STRIDE):
                    digests.add(digest(text[i:i + WINDOW]))
    return b"".join(sorted(digests))


def load_indexes(root: Path = ROOT / "parity") -> set[bytes]:
    out: set[bytes] = set()
    for path in sorted(root.glob(f"*/{INDEX_NAME}")):
        raw = path.read_bytes()
        out.update(raw[i:i + DIGEST] for i in range(0, len(raw), DIGEST))
    return out


# ---------------------------------------------------------------- noeta's texts


def _docstrings(tree: ast.AST) -> set[int]:
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                ids.add(id(body[0].value))
    return ids


def python_texts(path: Path) -> Iterator[tuple[int, str]]:
    """``(line, text)`` of every string literal of a module but its
    docstrings; an f-string gives each literal piece between its fields."""
    tree = ast.parse(path.read_text("utf-8"), str(path))
    skip = _docstrings(tree)
    in_fstring: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            for part in node.values:
                in_fstring.add(id(part))
                if isinstance(part, ast.Constant) and isinstance(part.value, str):
                    yield node.lineno, part.value
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and id(node) not in skip and id(node) not in in_fstring:
            yield node.lineno, node.value


def noeta_texts(root: Path = ROOT / "noeta") -> Iterator[tuple[str, int, str]]:
    """``(file, line, text)`` for every text in ``root`` the model may see."""
    for path in sorted(root.rglob("*")):
        rel = str(path.relative_to(ROOT))
        if path.suffix == ".py":
            for line, text in python_texts(path):
                yield rel, line, text
        elif path.suffix in (".md", ".js"):
            yield rel, 1, path.read_text("utf-8")


def allowed(path: Path = ALLOWED) -> list[str]:
    if not path.is_file():
        return []
    lines = path.read_text("utf-8").splitlines()
    return sorted((normalize(x) for x in lines if x.strip() and not x.startswith("#")),
                  key=len, reverse=True)


def matches(text: str, indexed: set[bytes], allow: list[str]) -> list[tuple[int, str]]:
    """``(offset, snippet)`` of each shared run in ``text`` (normalized),
    one per run."""
    text = normalize(text)
    for literal in allow:
        text = text.replace(literal, "\0" * len(literal))
    found: list[tuple[int, str]] = []
    last = -WINDOW
    for i in range(len(text) - WINDOW + 1):
        window = text[i:i + WINDOW]
        if "\0" in window or digest(window) not in indexed:
            continue
        if i > last + WINDOW:
            found.append((i, text[i:i + SHARED + 20].replace("\0", "")))
        last = i
    return found


def check(indexed: set[bytes] | None = None, root: Path = ROOT / "noeta") -> list[str]:
    indexed = load_indexes() if indexed is None else indexed
    allow = allowed()
    problems = []
    for rel, line, text in noeta_texts(root):
        if len(text) < WINDOW:
            continue
        for offset, snippet in matches(text, indexed, allow):
            where = line + (text[:offset].count("\n") if rel.endswith((".md", ".js")) else 0)
            problems.append(f"{rel}:{where}: repeats Claude Code's text: {snippet!r}")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command")
    make = sub.add_parser("index", help="hash a recording's Claude Code texts")
    make.add_argument("recordings", nargs="+", type=Path)
    make.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "index":
        args.out.write_bytes(index(args.recordings))
        print(f"{args.out}: {args.out.stat().st_size // DIGEST} windows")
        return 0
    if not load_indexes():
        print("own-words: no index under parity/", file=sys.stderr)
        return 2
    problems = check()
    for problem in problems:
        print(problem)
    if problems:
        print(f"own-words: {len(problems)} text(s) repeat Claude Code; reword them "
              f"(or, for a tool name or protocol literal, list it in {ALLOWED.name})")
        return 1
    print("own-words: green")
    return 0


if __name__ == "__main__":
    sys.exit(main())
