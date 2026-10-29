"""What a shell command needs: read-only and in-scope file commands run,
the rest is asked about with the rules an "always" answer remembers — and
how a path looks from the task's env (its links resolved, its globs
expanded), which the file tools' check shares."""

from __future__ import annotations

import asyncio
import os
import posixpath
import re
import shlex
import subprocess
from collections.abc import Mapping, Sequence
from typing import Any

from noeta.core.extension import TaskView
from noeta.plugins.permissions import policy as p
from noeta.plugins.permissions import scope as sc
from noeta.plugins.permissions import wording as w
from noeta.plugins.permissions.decision import Decision
from noeta.plugins.permissions.rule_specs import rule_spec_from_command
from noeta.toolkit import permission_rules as r
from noeta.toolkit.sandbox import leaves_sandbox, runs_sandboxed, sandbox_of
from noeta.toolkit.shell import parse_argv

#: The plugin whose ``cwd`` record says where the next shell command starts.
SHELL_PLUGIN = "shell"

#: A path with glob characters: judged by what it expands to.
_GLOB = re.compile(r"[*?\[]")


async def shell_decision(view: TaskView, args: Mapping[str, Any], mode: str,
                         scope: sc.Scope, ctx: r.RuleContext,
                         allow_rules: Sequence[r.Rule]) -> Decision:
    """What ``Bash`` with ``args`` needs in ``mode``."""
    command = args.get("command")
    if not isinstance(command, str) or not command.strip():
        return Decision("ask", w.ASK_COMMAND, why="command")
    if p.CONFIG_DIR in command and mode != "bypassPermissions":
        return Decision("ask", w.ASK_PROTECTED.format(name="Bash", path=p.CONFIG_DIR),
                        why="protected")
    sandbox = sandbox_of(view.env)
    disable = args.get("dangerouslyDisableSandbox")
    if leaves_sandbox(sandbox, command, disable):
        # Leaving the sandbox is asked about whatever the allow rules say.
        return Decision("ask", w.ASK_UNSANDBOXED.format(name="Bash"), why="sandbox")
    if (sandbox is not None and sandbox.settings.auto_allow_bash and mode != "plan"
            and runs_sandboxed(sandbox, command, disable)):
        return Decision("allow")
    here = shell_scope(view, scope)
    real = await real_scope(view, scope)
    found = sc.parts(command, here)
    if found is not None and all(x.read_only for x in found) and all(
            [await _reads_inside(view, x, real) for x in found]):
        return Decision("allow")
    if any(x.tool == "Bash" and x.content is None for x in allow_rules):
        return Decision("allow")
    open_parts = await _open_parts(
        view, found if found is not None else [sc.Part(command.strip(), False)],
        mode, scope, real, ctx, allow_rules)
    if not open_parts:
        return Decision("allow")
    for part in open_parts:
        for x in (*(part.writes or ()), *part.redirects):
            if sc.is_protected(x) or sc.is_protected(await real_path(view, x)):
                # Asked in bypass mode too, as the file tools' writes there.
                return Decision("ask", w.ASK_PROTECTED.format(name="Bash", path=x),
                                why="protected")
    if mode == "plan":
        return Decision("ask", w.ASK_PLAN.format(name="Bash"), why="plan")
    always, label = shell_always(open_parts, here)
    return Decision("ask", w.ASK_COMMAND, why="command", always=always,
                    always_label=label, mode_choice="auto")


async def _open_parts(view: TaskView, parts: Sequence[sc.Part], mode: str, scope: sc.Scope,
                      real: sc.Scope, ctx: r.RuleContext,
                      allow_rules: Sequence[r.Rule]) -> list[sc.Part]:
    """The subcommands neither read-only in scope, nor let through by a
    rule, nor writing only where they may."""
    bash_rules = [x for x in allow_rules if x.tool == "Bash" and x.content is not None]
    edit_rules = [x for x in allow_rules if x.tool == "Edit" and x.content is not None]
    # Every target is judged on the disk as it is before the command
    # runs: a link an earlier part copies or moves into place (or that a
    # part a rule lets through may make) is not there yet, so a later
    # write (or read) under what such a part wrote is not judged here.
    placed: list[str] = []
    unknown = False

    def under_placed(path: str, *, reading: bool = False) -> bool:
        return (unknown and not reading) or any(
            path == x or path.startswith(x.rstrip("/") + "/") for x in placed)

    open_parts: list[sc.Part] = []
    for part in parts:
        if part.read_only and not any(under_placed(x, reading=True) for x in part.reads) \
                and await _reads_inside(view, part, real):
            continue
        if any(r.shell_rule_matches(x.content or "", part.text, every=True)
               for x in bash_rules):
            unknown = True
            continue
        if part.writes is not None and all(
                [not under_placed(x) and await _writable(view, x, mode, scope, real, ctx,
                                                         edit_rules)
                 for x in part.writes]):
            if _may_place_links(part.argv):
                placed.extend(part.writes)
            continue
        open_parts.append(part)
    return open_parts


async def _reads_inside(view: TaskView, part: sc.Part, real: sc.Scope) -> bool:
    # A path inside as written may be a link out of the directory; a glob
    # is judged by what it expands to, and as written too (a quoted ``[``
    # names a file literally).
    for x in part.reads:
        targets: list[str] | None = [x]
        if _GLOB.search(x):
            expanded = await expand_glob(view, x)
            targets = None if expanded is None else [*expanded, x]
        if targets is None or not all(
                [real.contains(await real_path(view, t)) for t in targets]):
            return False
    return True


async def _writable(view: TaskView, path: str, mode: str, scope: sc.Scope, real: sc.Scope,
                    ctx: r.RuleContext, edit_rules: Sequence[r.Rule]) -> bool:
    resolved = await real_path(view, path)
    if path in scope.dirs or sc.is_protected(path) or sc.is_protected(resolved):
        return False
    if mode in ("acceptEdits", "bypassPermissions"):
        return scope.contains(path) and real.contains(resolved)
    return any(r.path_pattern_matches(x.content or "", path, ctx)
               and r.path_pattern_matches(x.content or "", resolved, ctx)
               for x in edit_rules)


def shell_scope(view: TaskView, scope: sc.Scope) -> sc.Scope:
    """``scope`` at the directory the shell's next command starts in
    (the shell plugin's ``cwd`` record)."""
    cwd = getattr(view.ext_state(SHELL_PLUGIN), "cwd", None)
    if isinstance(cwd, str) and cwd and posixpath.isabs(cwd):
        cwd = posixpath.normpath(cwd)
        if scope.contains(cwd):
            return scope.at(cwd)
    return scope


async def real_path(view: TaskView, path: str) -> str:
    """``path`` with its symlinks resolved as the task's env sees them
    (a missing tail kept as named)."""
    try:
        if view.env is not None:
            return posixpath.normpath(await view.env.realpath(path))
        return await asyncio.to_thread(os.path.realpath, path)
    except Exception:  # noqa: BLE001 — unresolvable: judged as written
        return path


async def expand_glob(view: TaskView, pattern: str) -> list[str] | None:
    """What ``pattern`` (absolute) expands to in the task's env, as bash
    expands it for the command; ``None`` when that cannot be told."""
    script = 'IFS=; shopt -s nullglob; for f in $1; do printf "%s\\0" "$f"; done'
    try:
        if view.env is not None:
            result = await view.env.run(["bash", "-c", script, "_", pattern], timeout=10)
            if result.exit_code != 0 or result.timed_out or result.stdout_truncated:
                return None
            out = result.stdout.decode("utf-8", "surrogateescape")
        else:
            done = await asyncio.to_thread(
                subprocess.run, ["bash", "-c", script, "_", pattern],
                capture_output=True, text=True, timeout=10)
            if done.returncode != 0:
                return None
            out = done.stdout
    except Exception:  # noqa: BLE001 — unknown expansion: ask
        return None
    return [x for x in out.split("\0") if x]


async def real_scope(view: TaskView, scope: sc.Scope) -> sc.Scope:
    """``scope`` with its directories' links resolved."""
    dirs = [await real_path(view, d) for d in scope.dirs]
    return sc.Scope(dirs=tuple(dict.fromkeys(dirs)), home=scope.home)


#: ``cp`` flags that copy what links point to, never a link itself.
_CP_PLAIN_FLAGS = frozenset({"--force", "--interactive", "--no-clobber", "--verbose",
                             "--update", "--dereference"})


def _may_place_links(argv: Sequence[str]) -> bool:
    """A file command that may leave a symlink (or a hard link) at what it
    writes: ``mv``, and ``cp`` unless only plain-copy flags are given."""
    if argv[:1] == ("mv",):
        return True
    if argv[:1] != ("cp",):
        return False
    for word in argv[1:]:
        if word == "--":
            break
        if word.startswith("--"):
            if word not in _CP_PLAIN_FLAGS:
                return True
        elif word.startswith("-") and word != "-" and set(word[1:]) - set("finvupL"):
            return True
    return False


def shell_always(parts: Sequence[sc.Part],
                 scope: sc.Scope) -> tuple[tuple[str, ...], str | None]:
    """The rules an "always" answer to these subcommands remembers, and how
    the card says it: a file command's directory, or each command's rule."""
    dirs: list[str] = []
    for part in parts:
        if part.writes is None or not all(scope.contains(x) for x in part.writes):
            dirs = []
            break
        dirs += [posixpath.dirname(x) if x not in scope.dirs else x for x in part.writes]
    if dirs and len(set(dirs)) == 1:
        target = dirs[0]
        # ``//`` anchors the pattern at the filesystem root.
        return (r.format_rule("Edit", f"/{target}/**"),), w.CHOICE_ALWAYS_DIR.format(dir=target)
    rules: list[str] = []
    for part in parts:
        spec = _remembered_rule(part.text)
        if spec is None:
            return (), None
        rules.append(r.format_rule("Bash", spec))
    shown = ", ".join(x[5:-1] for x in rules)
    label = (w.CHOICE_ALWAYS_COMMANDS.format(rules=f"{shown} commands", dir=scope.cwd)
             if len(rules) > 1 else w.CHOICE_ALWAYS_RULE.format(rules=f"{shown}"))
    return tuple(dict.fromkeys(rules)), label


#: Programs whose arguments can run code or write anywhere (a script, an
#: ``-exec``, a ``w`` command): an "always" answer remembers the exact
#: command, never a prefix.
_EXACT_RULE_PROGRAMS = frozenset({"awk", "gawk", "mawk", "nawk", "sed", "gsed", "find", "perl",
                                  "make", "gmake", "npx", "bunx", "uvx", "ssh", "tar", "rsync"})


def _remembered_rule(command: str) -> str | None:
    """What an "always" answer to ``command`` remembers: Claude Code's
    seeded rule (``git push *``, ``ls *``), narrowed to the exact command
    when a flag comes before any subcommand (``git -c x=y log``) or the
    program's arguments can run code (``awk``, ``sed``, ``find``)."""
    spec = rule_spec_from_command(command)
    if spec is None:
        return None
    rule = str(spec["rule"])
    argv = parse_argv(command) or []
    if not rule.endswith(" *") or not argv:
        return rule
    if argv[0] in _EXACT_RULE_PROGRAMS or (len(argv) > 1 and argv[1].startswith("-")):
        return shlex.join(argv)
    return rule
