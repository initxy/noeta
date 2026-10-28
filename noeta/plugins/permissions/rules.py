"""Claude Code's permission rules: ``Tool`` or ``Tool(content)`` strings in
``allow`` / ``deny`` / ``ask`` lists.

* ``Bash(npm run test)`` matches that command exactly; ``Bash(npm run *)``
  (a ``*`` anywhere matches any text; a `` *`` at the end also matches the
  bare prefix) and the older ``Bash(npm run:*)`` match by prefix. A compound
  command (``a && b | c``) is judged per subcommand: a deny or ask rule
  matching any part applies, an allow rule must cover every part.
* ``Read(path)`` / ``Edit(path)`` take gitignore-style patterns: ``//abs``
  is an absolute path, ``~/p`` is under the home directory, ``/p`` and
  ``p`` / ``./p`` are relative to the project root (a pattern with no ``/``
  matches at any depth); ``*`` stays inside one directory, ``**`` crosses
  them. ``Edit`` rules cover every tool that writes files; ``Read`` rules
  every tool that reads them.
* ``WebFetch(domain:example.com)`` (or ``*.example.com``) names a host.
* ``mcp__server`` / ``mcp__server__*`` cover a server's tools,
  ``mcp__server__tool`` one tool; ``Agent(Explore)`` a sub-agent type;
  ``Skill(name)`` a skill.

A rule string that names a tool with no content covers every call of it.
Nothing here touches a task: :func:`match` is a pure function of the rule,
the call and the paths it is judged against.
"""

from __future__ import annotations

import fnmatch
import posixpath
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from noeta.toolkit.shell import parse_argv, subcommands

__all__ = [
    "EDIT_TOOLS",
    "READ_TOOLS",
    "Rule",
    "RuleContext",
    "format_rule",
    "match",
    "parse_rule",
    "parse_rules",
    "path_pattern_matches",
]

#: Tools an ``Edit(...)`` rule covers.
EDIT_TOOLS = frozenset({"Edit", "Write", "NotebookEdit", "MultiEdit"})
#: Tools a ``Read(...)`` rule covers.
READ_TOOLS = frozenset({"Read", "Glob", "Grep", "LS", "NotebookRead"})
#: Tools whose ``command`` a ``Bash(...)`` rule covers.
SHELL_TOOLS = frozenset({"Bash", "Monitor"})
#: Old tool names a rule may use.
ALIASES = {"Task": "Agent"}
#: Leading programs that only run the rest of the command (stripped before
#: matching, so ``timeout 5 rm x`` meets a rule on ``rm``).
_TRANSPARENT_WRAPPERS = frozenset({"timeout", "time", "nice", "nohup", "stdbuf"})


@dataclass(frozen=True)
class Rule:
    """One parsed rule. ``content`` is ``None`` for a whole-tool rule."""

    tool: str
    content: str | None
    text: str


@dataclass(frozen=True)
class RuleContext:
    """The paths a rule is judged against: the project root (where ``/p``
    and relative patterns are anchored), the home directory, and the
    working directory relative paths of a call resolve against."""

    root: str
    home: str
    cwd: str


def _unescape(text: str) -> str:
    return re.sub(r"\\([()\\])", r"\1", text)


def _escape(text: str) -> str:
    return re.sub(r"([()\\])", r"\\\1", text)


def parse_rule(text: str) -> Rule:
    """``Tool`` or ``Tool(content)`` (``\\(`` / ``\\)`` escape parentheses
    inside the content). Raises ``ValueError`` on anything else, so a typo
    in a security setting never passes silently."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError(f"a permission rule must be a non-empty string; got {text!r}")
    raw = text.strip()
    open_at = -1
    for i, ch in enumerate(raw):
        if ch == "(" and (i == 0 or raw[i - 1] != "\\"):
            open_at = i
            break
    if open_at < 0:
        tool, content = raw, None
    else:
        if not raw.endswith(")"):
            raise ValueError(f"permission rule {text!r} opens '(' without closing it at the end")
        tool, content = raw[:open_at], _unescape(raw[open_at + 1:-1])
        if content in ("", "*"):
            content = None
    tool = tool.strip()
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.*-]*", tool):
        raise ValueError(f"permission rule {text!r} does not start with a tool name")
    return Rule(tool=ALIASES.get(tool, tool), content=content, text=raw)


def parse_rules(texts: Iterable[str], where: str = "rules") -> tuple[Rule, ...]:
    out = []
    for i, text in enumerate(texts):
        try:
            out.append(parse_rule(text))
        except ValueError as exc:
            raise ValueError(f"{where}[{i}]: {exc}") from None
    return tuple(out)


def format_rule(tool: str, content: str | None = None) -> str:
    """The rule string for ``tool`` and ``content`` (the inverse of
    :func:`parse_rule`)."""
    return tool if content is None else f"{tool}({_escape(content)})"


# ---------------------------------------------------------------- matching


def _tool_matches(rule_tool: str, name: str) -> bool:
    if rule_tool == name:
        return True
    if rule_tool == "Bash" and name in SHELL_TOOLS:
        return True
    if rule_tool == "Edit" and name in EDIT_TOOLS:
        return True
    if rule_tool == "Read" and name in READ_TOOLS:
        return True
    if rule_tool.startswith("mcp__"):
        parts = rule_tool.split("__")
        if len(parts) == 2 or (len(parts) == 3 and parts[2] in ("", "*")):
            return name.startswith(f"mcp__{parts[1]}__")
        return "*" in rule_tool and fnmatch.fnmatchcase(name, rule_tool)
    return False


def match(rule: Rule, name: str, input: Mapping[str, Any], ctx: RuleContext, *,
          for_allow: bool = False) -> bool:
    """Whether ``rule`` covers the call ``name(input)``. ``for_allow``: a
    shell rule must cover every subcommand (deny and ask rules match any)."""
    name = ALIASES.get(name, name)
    if not _tool_matches(rule.tool, name):
        return False
    if rule.content is None:
        return True
    if name in SHELL_TOOLS:
        command = input.get("command")
        return isinstance(command, str) and shell_rule_matches(rule.content, command,
                                                               every=for_allow)
    if name in EDIT_TOOLS or name in READ_TOOLS:
        path = _call_path(input)
        if path is None:
            # A search with no path searches the working directory.
            path = ctx.cwd if name in ("Glob", "Grep", "LS") else None
        return path is not None and path_pattern_matches(rule.content, path, ctx)
    if name == "WebFetch":
        return _domain_matches(rule.content, input.get("url"))
    if name == "Agent":
        kind = input.get("subagent_type") or "general-purpose"
        return isinstance(kind, str) and fnmatch.fnmatchcase(kind, rule.content)
    if name == "Skill":
        skill = input.get("skill") or input.get("name")
        if not isinstance(skill, str):
            return False
        args = input.get("args")
        full = f"{skill} {args}".strip() if isinstance(args, str) else skill
        return _command_matches(rule.content, skill) or _command_matches(rule.content, full)
    return False


def _call_path(input: Mapping[str, Any]) -> str | None:
    for key in ("file_path", "notebook_path", "path"):
        value = input.get(key)
        if isinstance(value, str) and value:
            return value
    return None


# ---------------------------------------------------------------- shell


def _strip_wrappers(command: str) -> str:
    argv = parse_argv(command)
    if not argv:
        return command
    words = command.split()
    i = 0
    while i < len(argv) and argv[i] in _TRANSPARENT_WRAPPERS:
        i += 1
        if argv[i - 1] == "timeout":
            while i < len(argv) and argv[i].startswith("-"):
                i += 1
            if i < len(argv) and re.fullmatch(r"\d+(\.\d+)?[smhd]?", argv[i]):
                i += 1
        elif argv[i - 1] in ("nice", "stdbuf"):
            while i < len(argv) and argv[i].startswith("-"):
                i += 2 if argv[i] == "-n" else 1
    if i == 0 or i >= len(argv) or len(words) != len(argv):
        return command
    return " ".join(words[i:])


def _command_matches(content: str, command: str) -> bool:
    """One rule content against one (sub)command."""
    command = command.strip()
    if content.endswith(":*"):
        prefix = content[:-2].rstrip()
        return command == prefix or command.startswith(prefix + " ")
    if "*" not in content:
        return command == content.strip()
    if content.endswith(" *") and command == content[:-2].rstrip() and "*" not in content[:-2]:
        return True
    pattern = "".join(".*" if part == "*" else re.escape(part)
                      for part in re.split(r"(\*)", content))
    return re.fullmatch(pattern, command, flags=re.DOTALL) is not None


def shell_rule_matches(content: str, command: str, *, every: bool) -> bool:
    """A ``Bash(content)`` rule against ``command``. ``every``: each
    subcommand must match (an allow rule); otherwise the whole command or
    any part matching is enough (a deny or ask rule)."""
    parts = [_strip_wrappers(p) for p in subcommands(command)] or [command]
    if every:
        return all(_command_matches(content, p) for p in parts)
    if _command_matches(content, command.strip()):
        return True
    return any(_command_matches(content, p) for p in parts)


# ---------------------------------------------------------------- paths


def _anchor(pattern: str, ctx: RuleContext) -> tuple[str, str]:
    """``(base directory, pattern relative to it)``."""
    if pattern.startswith("//"):
        return "/", pattern[2:]
    if pattern == "~" or pattern.startswith("~/"):
        return ctx.home, pattern[2:]
    if pattern.startswith("/"):
        return ctx.root, pattern[1:]
    if pattern.startswith("./"):
        return ctx.root, pattern[2:]
    return ctx.root, pattern


def _glob_regex(pattern: str) -> str:
    out = []
    i = 0
    while i < len(pattern):
        ch = pattern[i]
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif ch == "*":
            out.append("[^/]*")
            i += 1
        elif ch == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(ch))
            i += 1
    return "".join(out)


def path_pattern_matches(pattern: str, path: str, ctx: RuleContext) -> bool:
    """A ``Read`` / ``Edit`` rule pattern against a path (relative paths
    resolve against ``ctx.cwd``). A pattern names a file or a directory: a
    directory covers everything under it."""
    target = path if posixpath.isabs(path) else posixpath.join(ctx.cwd, path)
    if target.startswith("~/"):
        target = posixpath.join(ctx.home, target[2:])
    target = posixpath.normpath(target)
    base, rel_pattern = _anchor(pattern, ctx)
    base = posixpath.normpath(base)
    if base != "/" and target != base and not target.startswith(base.rstrip("/") + "/"):
        return False
    rel = posixpath.relpath(target, base) if target != base else ""
    rel_pattern = rel_pattern.rstrip("/") or "**"
    floating = "/" not in rel_pattern and base == ctx.root and not pattern.startswith(
        ("/", "./"))
    body = _glob_regex(rel_pattern)
    regex = f"(?:.*/)?{body}(?:/.*)?" if floating else f"{body}(?:/.*)?"
    return re.fullmatch(regex, rel, flags=re.DOTALL) is not None


# ---------------------------------------------------------------- web


def _domain_matches(content: str, url: Any) -> bool:
    if not content.startswith("domain:") or not isinstance(url, str):
        return False
    from noeta.plugins.permissions.policy import url_host

    host = url_host(url)
    entry = content[len("domain:"):].strip().lower().rstrip(".")
    if not host or not entry:
        return False
    if entry.startswith("*."):
        return host.endswith(entry[1:]) and len(host) > len(entry) - 1
    return host == entry


def first_match(rules: Sequence[Rule], name: str, input: Mapping[str, Any],
                ctx: RuleContext, *, for_allow: bool = False) -> Rule | None:
    for rule in rules:
        if match(rule, name, input, ctx, for_allow=for_allow):
            return rule
    return None
