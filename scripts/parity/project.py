"""The scenario project both Claude Code (when recording) and noeta (when
replaying) work in: a tiny git repo with a project instruction file, a skill
that takes arguments, an MCP server and a data file for the compaction
scenario.

The files live here as data rather than as a directory, because a checked-in
``CLAUDE.md`` / ``.claude/`` / ``.mcp.json`` would be picked up by whatever
coding agent works on this repository.

    python -m scripts.parity.project DEST     # writes DEST/proj and DEST/mcp_echo.py
"""

from __future__ import annotations

import argparse
import json
import random
import subprocess
import sys
from pathlib import Path

INSTRUCTIONS = """\
# Project notes
- Python 3.11 project; run tests with `python -m pytest`.
- `src/calc.py` provides `add`, `mul`, and `div` (`div` raises `ValueError` on a zero divisor).
"""

CALC = """\
def add(a, b):
    return a + b


def div(a, b):
    return a / b
"""

SKILL = """\
---
name: release-notes
description: Draft release notes from the git log. Use when asked for release notes or a changelog entry.
---

Run `git log --oneline -20`, group the commits into Added / Fixed / Changed, and print the notes.
Arguments: $ARGUMENTS
"""

#: A stdio MCP server with one tool (``echo``) and one resource (``echo://readme``).
MCP_ECHO = '''\
import json, sys
def send(o): sys.stdout.write(json.dumps(o)+"\\n"); sys.stdout.flush()
for line in sys.stdin:
    m=json.loads(line); i=m.get("id"); meth=m.get("method")
    if meth=="initialize": send({"jsonrpc":"2.0","id":i,"result":{"protocolVersion":"2025-06-18","capabilities":{"tools":{},"resources":{},"prompts":{}},"serverInfo":{"name":"echo","version":"1"}}})
    elif meth=="tools/list": send({"jsonrpc":"2.0","id":i,"result":{"tools":[{"name":"echo","description":"Echo text back.","inputSchema":{"type":"object","properties":{"text":{"type":"string"}},"required":["text"]}}]}})
    elif meth=="tools/call": send({"jsonrpc":"2.0","id":i,"result":{"content":[{"type":"text","text":"echo: "+m["params"]["arguments"].get("text","")}]}})
    elif meth=="resources/list": send({"jsonrpc":"2.0","id":i,"result":{"resources":[{"uri":"echo://readme","name":"readme","mimeType":"text/plain"}]}})
    elif meth=="resources/read": send({"jsonrpc":"2.0","id":i,"result":{"contents":[{"uri":"echo://readme","text":"hello resource"}]}})
    elif meth=="prompts/list": send({"jsonrpc":"2.0","id":i,"result":{"prompts":[]}})
    elif i is not None: send({"jsonrpc":"2.0","id":i,"result":{}})
'''


def data_rows() -> str:
    """``data.txt`` of the auto-compaction scenario: 4000 rows of four words."""
    rng = random.Random(1)
    words = ["alpha", "beta", "gamma", "delta"]
    return "\n".join(f"row {i}: " + " ".join(rng.choice(words) for _ in range(12))
                     for i in range(4000))


def write_project(dest: Path, *, data: bool = False, python: str = "python3",
                  git_user: str = "parity") -> Path:
    """Write the project under ``dest/proj`` (committed as ``init calc``) and
    the MCP server beside it; returns the project directory. With ``data``,
    ``data.txt`` is added after the commit, untracked, as the recording had it."""
    dest = dest.resolve()
    proj = dest / "proj"
    files = {
        "CLAUDE.md": INSTRUCTIONS,
        "src/calc.py": CALC,
        ".claude/skills/release-notes/SKILL.md": SKILL,
        ".mcp.json": json.dumps({"mcpServers": {"echo": {
            "command": python, "args": [str(dest / "mcp_echo.py")]}}}, indent=2) + "\n",
    }
    for rel, text in files.items():
        path = proj / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, "utf-8")
    (dest / "mcp_echo.py").write_text(MCP_ECHO, "utf-8")
    git = ["git", "-c", f"user.name={git_user}", "-c", f"user.email={git_user}@example.invalid",
           "-c", "init.defaultBranch=main", "-c", "commit.gpgsign=false"]
    subprocess.run([*git, "init", "-q"], cwd=proj, check=True)
    subprocess.run([*git, "add", "-A"], cwd=proj, check=True)
    subprocess.run([*git, "commit", "-q", "-m", "init calc"], cwd=proj, check=True)
    if data:
        (proj / "data.txt").write_text(data_rows(), "utf-8")
    return proj


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("dest", type=Path)
    ap.add_argument("--data", action="store_true", help="add data.txt (auto-compaction scenario)")
    ap.add_argument("--python", default="python3", help="interpreter .mcp.json starts the server with")
    args = ap.parse_args(argv)
    print(write_project(args.dest.expanduser(), data=args.data, python=args.python))


if __name__ == "__main__":
    main(sys.argv[1:])
