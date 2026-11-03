"""What the ``allow`` / ``deny`` / ``ask`` rules, the working directories
and the mode say about a call, in Claude Code's order, before any
classifier."""

from __future__ import annotations

import fnmatch
import os
import posixpath
from collections.abc import Mapping, Sequence
from typing import Any

from noeta.core.extension import TaskView
from noeta.core.types import ToolUse
from noeta.plugins.permissions import deny_paths as dp
from noeta.plugins.permissions import plan as pl
from noeta.plugins.permissions import policy as p
from noeta.plugins.permissions import scope as sc
from noeta.plugins.permissions import wording as w
from noeta.plugins.permissions.decision import Decision, in_mode
from noeta.plugins.permissions.shell_check import (
    real_path,
    real_scope,
    resolve_paths,
    shell_decision,
    shell_scope,
)
from noeta.plugins.permissions.state import inherited_rules, state_of
from noeta.toolkit import permission_rules as r


class RuleCheck:
    """The settings a decision is made from; :meth:`decide` makes it."""

    def __init__(self, *, allow_rules: Sequence[r.Rule], deny_rules: Sequence[r.Rule],
                 ask_rules: Sequence[r.Rule], additional_directories: Sequence[str],
                 risk: Mapping[str, str], default_risk: str, plans_dir: str) -> None:
        self.allow_rules = tuple(allow_rules)
        self.deny_rules = tuple(deny_rules)
        self.ask_rules = tuple(ask_rules)
        self.additional_directories = tuple(additional_directories)
        self.risk = risk
        self.default_risk = default_risk
        self.plans_dir = plans_dir

    def rules(self, view: TaskView) -> tuple[r.Rule, ...]:
        """Allow rules: the host's, then those remembered (inherited and own)."""
        remembered = []
        for text in inherited_rules(view.state):
            try:
                remembered.append(r.parse_rule(text))
            except ValueError:
                continue
        return (*self.allow_rules, *remembered)

    def scope(self, view: TaskView) -> sc.Scope:
        """The working directory and the additional ones."""
        root = posixpath.normpath(view.cwd or os.getcwd())
        home = os.path.expanduser("~")
        dirs = [root]
        for d in self.additional_directories:
            if d == "~" or d.startswith("~/"):
                d = home + d[1:]
            dirs.append(posixpath.normpath(d if posixpath.isabs(d) else posixpath.join(root, d)))
        return sc.Scope(dirs=tuple(dict.fromkeys(dirs)), home=home)

    async def read_deny_rule(self, view: TaskView, path: str) -> str | None:
        """The deny rule that keeps ``Read`` from ``path`` (or from where it
        leads), as written; ``None`` when no deny rule covers it. Modes and
        ask rules play no part."""
        scope = self.scope(view)
        ctx = r.RuleContext(root=scope.cwd, home=scope.home, cwd=scope.cwd)
        target = scope.absolute(path)
        seen = [{"file_path": target}]
        real = await real_path(view, target)
        if real != target:
            seen.append({"file_path": real})
        denied = next((m for a in seen if (m := r.first_match(self.deny_rules, "Read", a, ctx))
                       is not None), None)
        return denied.text if denied is not None else None

    async def decide(self, view: TaskView, call: ToolUse, mode: str, *,
                     plan_classifier: bool = False) -> Decision:
        """Claude Code's order: deny rules, ask rules, then what the tool
        touches and the mode. ``plan_classifier``: plan mode runs with auto
        mode's classifier (``in_mode``)."""
        name, args = call.name, call.input
        scope = self.scope(view)
        ctx = r.RuleContext(root=scope.cwd, home=scope.home, cwd=scope.cwd)
        # A file tool reaches where a symlink points: a deny or ask rule on
        # either path applies.
        seen: list[Mapping[str, Any]] = [args]
        if name in r.READ_TOOLS or name in r.EDIT_TOOLS:
            target = scope.absolute(_path(args) or scope.cwd)
            real = await real_path(view, target)
            if real != target:
                seen.append(_with_path(args, real))
        aliases = await r.rule_inputs(view, name, args)
        seen += [a for a, _allow in aliases]
        denied = next((m for a in seen if (m := r.first_match(self.deny_rules, name, a, ctx))
                       is not None), None)
        if denied is not None:
            return Decision("deny", w.DENIED_BY_RULE.format(name=name, rule=denied.text))
        if name in r.SHELL_TOOLS:
            refused = await self._shell_path_denied(view, name, args, scope, ctx)
            if refused is not None:
                return refused
        asked = next((m for a in seen if (m := r.first_match(self.ask_rules, name, a, ctx))
                      is not None), None)
        if asked is not None:
            return in_mode(mode, Decision(
                "ask", w.ASK_RULE.format(rule=asked.text, name=name), firm=True, why="rule"),
                name)
        allow_rules = self.rules(view)
        classifying = mode == "auto" or (mode == "plan" and plan_classifier)
        if classifying:
            allow_rules = tuple(x for x in allow_rules if not p.bypasses_classifier(x))
        found = await self._needs(view, call, mode, scope, ctx, allow_rules,
                                  [a for a, allow in aliases if allow])
        return in_mode(mode, found, name, plan_classifier=plan_classifier)

    async def _shell_path_denied(self, view: TaskView, name: str, args: Mapping[str, Any],
                                 scope: sc.Scope, ctx: r.RuleContext) -> Decision | None:
        """A refusal when the command writes a path an ``Edit`` deny rule
        covers, reads one a ``Read`` deny rule covers, or removes or moves
        a directory holding a path an ``Edit`` deny rule names: Claude
        Code's shell path check, which no mode gets past (see
        :mod:`deny_paths`). Each path is judged as named, then (in one call
        to the task's env) with its globs expanded and its links resolved.
        A command that cannot be read even leniently is refused when its
        text names a path a deny rule covers."""
        command = args.get("command")
        rules = {kind: [x for x in self.deny_rules if x.tool == kind] for kind in ("Edit", "Read")}
        if not isinstance(command, str) or not (rules["Edit"] or rules["Read"]):
            return None
        here = shell_scope(view, scope)
        found = dp.deny_paths(command, here)
        if found is None:
            return _opaque_refusal(name, command, [*rules["Edit"], *rules["Read"]], here, ctx)
        checks: list[tuple[str, dp.Target, bool]] = []
        if rules["Edit"]:
            checks += [("Edit", t, False) for t in found.writes]
            checks += [("Edit", t, True) for t in found.trees]
        if rules["Read"]:
            checks += [("Read", t, False) for t in found.reads]
        if not checks:
            return None

        def judge(kind: str, path: str, tree: bool) -> Decision | None:
            if tree:
                for each in rules["Edit"]:
                    lit = r.pattern_literal(each.content, ctx) if each.content else None
                    if lit is not None and lit[1] and (
                            lit[0] == path or lit[0].startswith(path.rstrip("/") + "/")):
                        return Decision("deny", w.SHELL_TREE_DENIED.format(
                            name=name, path=path, inner=lit[0], rule=each.text))
                return None
            rule = r.first_match(rules[kind], kind, {"file_path": path}, ctx)
            if rule is None:
                return None
            text = w.SHELL_WRITE_DENIED if kind == "Edit" else w.SHELL_READ_DENIED
            return Decision("deny", text.format(name=name, path=path, rule=rule.text))

        # As named first: most refusals need no call to the env.
        for kind, t, tree in checks:
            if (hit := judge(kind, t.path, tree)) is not None:
                return hit
        resolved = await resolve_paths(view, list(dict.fromkeys((t.path, t.glob)
                                                                for _k, t, _tr in checks)))
        by_path: dict[str, list[tuple[str, str]]] = {}
        for named, real in resolved:
            by_path.setdefault(named, []).append((named, real))
        for kind, t, tree in checks:
            pairs = by_path.get(t.path, [])
            if t.glob:
                pairs = [x for x in resolved if _fnmatch_path(x[0], t.path)]
            for named, real in pairs:
                for each in dict.fromkeys((named, real)):
                    if (hit := judge(kind, each, tree)) is not None:
                        return hit
        return None

    async def _needs(self, view: TaskView, call: ToolUse, mode: str, scope: sc.Scope,
                     ctx: r.RuleContext, allow_rules: Sequence[r.Rule],
                     aliases: Sequence[Mapping[str, Any]] = ()) -> Decision:
        name, args = call.name, call.input
        if name == pl.EXIT_PLAN_MODE:
            return Decision("allow")
        if name == pl.ENTER_PLAN_MODE:
            # Only a person agrees to plan mode, except where the classifier
            # already judges every action (in plan mode it may let read-only
            # actions through that would otherwise be asked about).
            if mode in ("auto", "plan"):
                return Decision("allow")
            return Decision("ask", w.ASK_ENTER_PLAN, firm=True, why="plan")
        allowed = any(r.first_match(allow_rules, name, a, ctx, for_allow=True) is not None
                      for a in (args, *aliases))
        if name in r.READ_TOOLS:
            return await self._read(view, name, args, scope, ctx, allow_rules, allowed)
        if name in r.EDIT_TOOLS:
            return await self._edit(view, name, args, mode, scope, allowed)
        if name in r.SHELL_TOOLS:
            return await shell_decision(view, args, mode, scope, ctx, allow_rules)
        if name == p.WEBFETCH_TOOL:
            if allowed:
                return Decision("allow")
            url = args.get("url")
            host = r.url_host(url) if isinstance(url, str) else None
            rule = (r.format_rule("WebFetch", f"domain:{host}"),) if host else ()
            return Decision("ask", w.ASK_FETCH.format(name=name, host=host or url), why="fetch",
                            always=rule, always_label=w.CHOICE_ALWAYS_RULE.format(
                                rules=rule[0]) if rule else None, mode_choice="auto")
        if allowed:
            return Decision("allow")
        if self.risk.get(name, self.default_risk) == "low":
            if mode == "auto" and name not in p.AUTO_SAFE_TOOLS:
                return Decision("ask", w.ASK_TOOL.format(name=name), why="tool", classify=True)
            return Decision("allow")
        if mode == "plan" and name not in p.AUTO_SAFE_TOOLS:
            return Decision("ask", w.ASK_PLAN.format(name=name), why="plan")
        return Decision("ask", w.ASK_TOOL.format(name=name), why="tool", always=(name,),
                        always_label=w.CHOICE_ALWAYS_RULE.format(rules=name),
                        mode_choice="auto")

    async def _read(self, view: TaskView, name: str, args: Mapping[str, Any], scope: sc.Scope,
                    ctx: r.RuleContext, allow_rules: Sequence[r.Rule],
                    allowed: bool) -> Decision:
        path = _path(args) or scope.cwd
        literal = scope.absolute(path)
        target = await real_path(view, literal)
        if target != literal:
            # Read through a link: judged where it points (an allow rule
            # too: "always" offers one on the target's directory).
            resolved = await real_scope(view, scope)
            inside = scope.contains(literal) and resolved.contains(target)
            allowed = r.first_match(allow_rules, name, _with_path(args, target), ctx,
                                    for_allow=True) is not None
        else:
            inside = scope.contains(literal)
        plan = state_of(view).plan
        if inside or allowed or self.in_plans(literal) \
                or (plan is not None and literal == plan.get("path")) \
                or await memory_file(view, literal, target, write=False) \
                or await spilled_file(view, literal, target):
            # Claude Code reads its plan, memory and tool-result files
            # without asking too.
            return Decision("allow")
        return Decision("ask", w.ASK_READ_OUTSIDE.format(name=name, path=target),
                        why="read", always=(r.format_rule("Read", f"//{_dir_of(target)}/**"),),
                        always_label=w.CHOICE_ALWAYS_RULE.format(
                            rules=f"Read(//{_dir_of(target)}/**)"))

    async def _edit(self, view: TaskView, name: str, args: Mapping[str, Any], mode: str,
                    scope: sc.Scope, allowed: bool) -> Decision:
        written = _path(args)
        target = scope.absolute(written) if written else scope.cwd
        plan = state_of(view).plan
        if mode == "plan" and plan and target == plan.get("path"):
            return Decision("allow")
        real = await real_path(view, target)
        if target.endswith(".md") and await memory_file(view, target, real, write=True):
            # Claude Code's auto-memory carve-out: its markdown files are
            # written without asking, in every mode.
            return Decision("allow")
        if not scope.contains(target) and not self.in_plans(target):
            return Decision("deny", w.OUTSIDE_WRITE.format(
                name=name, dirs=", ".join(scope.dirs), path=target))
        if sc.is_protected(target) or sc.is_protected(real):
            # The tools write where a symlink points: judge that too.
            return Decision("ask", w.ASK_PROTECTED.format(name=name, path=target),
                            why="protected")
        if mode == "plan":
            return Decision("ask", w.ASK_PLAN.format(name=name), why="plan")
        if allowed or mode in ("acceptEdits", "auto", "bypassPermissions"):
            return Decision("allow")
        verb = w.ASK_EDIT if name != "Write" or os.path.exists(target) else w.ASK_CREATE
        return Decision("ask", verb.format(name=name, path=target), why="edit",
                        mode_choice="acceptEdits")

    def in_plans(self, path: str) -> bool:
        base = posixpath.normpath(self.plans_dir)
        return path.startswith(base.rstrip("/") + "/")


def _path(args: Mapping[str, Any]) -> str | None:
    for key in ("file_path", "notebook_path", "path"):
        value = args.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _with_path(args: Mapping[str, Any], path: str) -> dict[str, Any]:
    """``args`` naming ``path`` instead of the path they name."""
    key = next((k for k in ("file_path", "notebook_path", "path")
                if isinstance(args.get(k), str) and args.get(k)), "path")
    return {**args, key: path}


def _dir_of(path: str) -> str:
    return posixpath.dirname(path).lstrip("/") or ""


def _memory_dir(view: TaskView) -> str | None:
    memory = view.state.ext.get("memory")
    directory = memory.get("dir") if isinstance(memory, dict) else None
    return directory if isinstance(directory, str) and directory else None


def _under(path: str, directory: str) -> bool:
    return posixpath.normpath(path).startswith(posixpath.normpath(directory).rstrip("/") + "/")


async def memory_file(view: TaskView, path: str, real: str, *, write: bool) -> bool:
    """Whether ``path`` (``real`` once its links are resolved) is a memory
    file: both lie in the memory directory as the env resolves it (one in
    the working directory reached through no link), and for a write the
    part below it is no protected path, so a link planted in the directory
    cannot carry a read or a write elsewhere."""
    directory = _memory_dir(view)
    if directory is None or not _under(path, directory):
        return False
    directory = posixpath.normpath(directory)
    real_dir = await real_path(view, directory)
    cwd = posixpath.normpath(view.cwd or getattr(view.env, "cwd", "") or "/")
    if _under(directory, cwd):
        # Inside the working directory (an agent's project memory) every
        # link on the way is the repository's: none may be followed.
        expected = posixpath.join(await real_path(view, cwd), posixpath.relpath(directory, cwd))
        if real_dir != posixpath.normpath(expected):
            return False
    if not _under(real, real_dir):
        return False
    return not write or not sc.is_protected("/" + posixpath.relpath(real, real_dir))


async def spilled_file(view: TaskView, path: str, real: str) -> bool:
    """Whether ``path`` (``real`` once its links are resolved) is a saved
    tool output: both below the spill root as the env resolves it."""
    root = view.spill_dir
    if not root:
        return False
    root = posixpath.normpath(root)
    return _under(path, root) and _under(real, await real_path(view, root))


def _fnmatch_path(path: str, pattern: str) -> bool:
    """``path`` is one of the names bash expands ``pattern`` to."""
    return path == pattern or (path.count("/") == pattern.count("/")
                               and fnmatch.fnmatchcase(path, pattern))


def _opaque_refusal(name: str, command: str, rules: Sequence[r.Rule], scope: sc.Scope,
                    ctx: r.RuleContext) -> Decision | None:
    """A command whose parts cannot be told apart: refused when its text
    names what a deny rule covers (the path absolute, or relative to the
    working directory or the project root)."""
    for rule in rules:
        lit = r.pattern_literal(rule.content, ctx) if rule.content else None
        if lit is None:
            continue
        text, anchored = lit
        forms = {text}
        if anchored:
            for base in (scope.cwd, ctx.root):
                if text.startswith(base.rstrip("/") + "/"):
                    forms.add(posixpath.relpath(text, base))
        if any(f and f not in (".", "/") and f in command for f in forms):
            return Decision("deny", w.SHELL_OPAQUE_DENIED.format(
                name=name, path=text, rule=rule.text))
    return None
