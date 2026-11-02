"""Claude Code's permission rules: ``Tool`` or ``Tool(content)`` strings in
``allow`` / ``deny`` / ``ask`` lists.

* ``Bash(npm run test)`` matches that command exactly; ``Bash(npm run *)``
  (a ``*`` anywhere matches any text; a `` *`` at the end also matches the
  bare prefix) and the older ``Bash(npm run:*)`` match by prefix. A compound
  command (``a && b | c``) is judged per subcommand: a deny or ask rule
  matching any part applies, an allow rule must cover every part and never
  covers a substitution or a redirection to a file (``npm test $(id)``).
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
The permissions plugin and hook ``if`` conditions share this module;
:func:`match` is a pure function of the rule, the call and the paths it is
judged against.
"""

from __future__ import annotations

import fnmatch
import posixpath
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote, urlsplit

from noeta.toolkit.shell import parse_argv, subcommands
from noeta.toolkit.shell_syntax import hides_effects, parse

if TYPE_CHECKING:
    from noeta.core.extension import TaskView

__all__ = [
    "EDIT_TOOLS",
    "READ_TOOLS",
    "SHELL_TOOLS",
    "Rule",
    "RuleContext",
    "first_match",
    "format_rule",
    "idna",
    "match",
    "parse_rule",
    "parse_rules",
    "path_pattern_matches",
    "rule_inputs",
    "shell_rule_matches",
    "url_host",
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
        skill = skill.strip().removeprefix("/")
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


#: Leading programs a deny or ask rule looks through: each runs the program
#: named after its own flags (and, for ``env``, assignments).
_RUNNERS = frozenset({"command", "builtin", "exec", "env", "nohup", "time", "timeout",
                      "nice", "stdbuf"})
#: Of their flags, those taking the next word as a value.
_RUNNER_VALUED = {"env": {"-u", "--unset", "-C", "--chdir", "-S", "--split-string"},
                  "nice": {"-n", "--adjustment"}, "timeout": {"-s", "--signal", "-k",
                                                               "--kill-after"},
                  "stdbuf": {"-i", "-o", "-e"}, "exec": {"-a"}}
#: ``git``'s options before its subcommand that take the next word as a
#: value; the others (``--no-pager``, ``-P``, ``--git-dir=x``) are one word.
_GIT_GLOBAL_VALUED = frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace",
                                "--config-env", "--super-prefix", "--attr-source"})


def _program_argv(argv: Sequence[str]) -> list[str]:
    """``argv`` from the program that runs: leading assignments and the
    :data:`_RUNNERS` in front of it (with their flags) dropped."""
    i = 0
    while i < len(argv):
        word = argv[i]
        if re.match(r"[A-Za-z_][A-Za-z0-9_]*=", word):
            i += 1
            continue
        if word not in _RUNNERS or (word == "command" and argv[i + 1:i + 2] in (["-v"],
                                                                                  ["-V"])):
            break  # ``command -v`` only looks the name up
        runner = word
        i += 1
        while i < len(argv) and argv[i].startswith("-") and argv[i] != "-":
            if argv[i] == "--":
                i += 1
                break
            i += 2 if argv[i] in _RUNNER_VALUED.get(runner, ()) else 1
        if runner == "timeout" and i < len(argv) and re.fullmatch(r"\d+(\.\d+)?[smhd]?",
                                                                  argv[i]):
            i += 1
    if argv[i:i + 1] == ["git"]:
        # ``git -C . push`` runs ``git push``.
        j = i + 1
        while j < len(argv) and argv[j].startswith("-") and argv[j] != "--":
            j += 2 if argv[j] in _GIT_GLOBAL_VALUED else 1
        return ["git", *argv[j:]]
    return list(argv[i:])


def _normalized_parts(command: str) -> list[str]:
    """Each subcommand as the words bash passes on (quotes and escapes
    removed), from the program that runs."""
    parsed = parse(command)
    if parsed.opaque:
        argvs = [a for a in (parse_argv(t) for t in subcommands(command)) if a]
    else:
        argvs = [c.argv for c in parsed.commands]
    return [" ".join(a) for a in (_program_argv(v) for v in argvs) if a]


def shell_rule_matches(content: str, command: str, *, every: bool) -> bool:
    """A ``Bash(content)`` rule against ``command``. ``every``: each
    subcommand must match (an allow rule), and a command whose words do not
    show all it does (a substitution, a heredoc, a redirection to or from a
    file) matches no allow rule; otherwise the whole command or any part
    matching is enough (a deny or ask rule), each part read both as written
    and as the words that run (``\\rm``, ``'rm'``, ``env X=1 rm`` meet a
    rule on ``rm``)."""
    parsed = parse(command)
    texts = subcommands(command) if parsed.opaque else [c.text for c in parsed.commands]
    parts = [_strip_wrappers(p) for p in texts] or [command]
    if every:
        return not hides_effects(command) and all(_command_matches(content, p) for p in parts)
    if _command_matches(content, command.strip()):
        return True
    return any(_command_matches(content, p) for p in [*parts, *_normalized_parts(command)])


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


def idna(host: str) -> str:
    try:
        return host.encode("idna").decode("ascii").lower()
    except (UnicodeError, ValueError):
        return host.lower()


def url_host(url: str) -> str | None:
    """The URL's real host, normalised (userinfo dropped, escapes decoded,
    trailing dot dropped, IDNA); ``None`` when it names none."""
    try:
        host = urlsplit(url).hostname
    except ValueError:
        return None
    if not host:
        return None
    host = unquote(host).strip().rstrip(".").lower()
    return idna(host) if host else None


def _domain_matches(content: str, url: Any) -> bool:
    if not content.startswith("domain:") or not isinstance(url, str):
        return False
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


async def rule_inputs(view: TaskView, name: str,
                      input: Mapping[str, Any]) -> list[tuple[Mapping[str, Any], bool]]:
    """Other inputs the called tool says rules also judge its call as
    (``Tool.rule_inputs``: a ``Skill`` call under its skill's former name),
    each with whether an allow rule may match it; ``[]`` when it says
    nothing. Permission rules and hook ``if`` conditions both read them."""
    tool = view.find_tool(name)
    get = getattr(tool, "rule_inputs", None)
    if get is None:
        return []
    return list(await get(input, view))
