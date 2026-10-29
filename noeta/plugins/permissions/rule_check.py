"""What the ``allow`` / ``deny`` / ``ask`` rules, the working directories
and the mode say about a call, in Claude Code's order, before any
classifier."""

from __future__ import annotations

import os
import posixpath
from collections.abc import Mapping, Sequence
from typing import Any

from noeta.core.extension import TaskView
from noeta.core.types import ToolUse
from noeta.plugins.permissions import plan as pl
from noeta.plugins.permissions import policy as p
from noeta.plugins.permissions import scope as sc
from noeta.plugins.permissions import wording as w
from noeta.plugins.permissions.decision import Decision, in_mode
from noeta.plugins.permissions.shell_check import real_path, real_scope, shell_decision
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

    async def decide(self, view: TaskView, call: ToolUse, mode: str) -> Decision:
        """Claude Code's order: deny rules, ask rules, then what the tool
        touches and the mode."""
        name, args = call.name, call.input
        scope = self.scope(view)
        ctx = r.RuleContext(root=scope.cwd, home=scope.home, cwd=scope.cwd)
        # A file tool reaches where a symlink points: a deny or ask rule on
        # either path applies.
        seen = [args]
        if name in r.READ_TOOLS or name in r.EDIT_TOOLS:
            target = scope.absolute(_path(args) or scope.cwd)
            real = await real_path(view, target)
            if real != target:
                seen.append(_with_path(args, real))
        denied = next((m for a in seen if (m := r.first_match(self.deny_rules, name, a, ctx))
                       is not None), None)
        if denied is not None:
            return Decision("deny", w.DENIED_BY_RULE.format(name=name, rule=denied.text))
        asked = next((m for a in seen if (m := r.first_match(self.ask_rules, name, a, ctx))
                      is not None), None)
        if asked is not None:
            return in_mode(mode, Decision(
                "ask", w.ASK_RULE.format(rule=asked.text, name=name), firm=True, why="rule"),
                name)
        allow_rules = self.rules(view)
        if mode == "auto":
            allow_rules = tuple(x for x in allow_rules if not p.bypasses_classifier(x))
        found = await self._needs(view, call, mode, scope, ctx, allow_rules)
        return in_mode(mode, found, name)

    async def _needs(self, view: TaskView, call: ToolUse, mode: str, scope: sc.Scope,
                     ctx: r.RuleContext, allow_rules: Sequence[r.Rule]) -> Decision:
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
        allowed = r.first_match(allow_rules, name, args, ctx, for_allow=True) is not None
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
        if inside or allowed or self.in_plans(literal):
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
        if not scope.contains(target) and not self.in_plans(target):
            return Decision("deny", w.OUTSIDE_WRITE.format(
                name=name, dirs=", ".join(scope.dirs), path=target))
        if sc.is_protected(target) or sc.is_protected(await real_path(view, target)):
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
