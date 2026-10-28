"""The ``Permissions`` extension: Claude Code's permission system over every
tool call — modes, ``allow`` / ``deny`` / ``ask`` rules, the working
directories, protected paths, the auto-mode classifier and plan mode — plus
noeta's own guards (budget caps, the repetition guard, host PreToolUse rules,
``can_use_tool``)."""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import posixpath
import shlex
import sys
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from noeta.core.events import (
    Answer,
    Compacted,
    ExtensionRecorded,
    External,
    InputTaken,
    ModelReplied,
    Recorded,
    Say,
)
from noeta.core.extension import (
    Allow,
    Ask,
    Command,
    CommandContext,
    Deny,
    Extension,
    Gate,
    Reminder,
    TaskView,
)
from noeta.core.pending import MODE_SIGNAL as _MODE_SIGNAL
from noeta.core.tool import Tool, ToolOutput
from noeta.core.types import ModelRequest, ToolResult, ToolUse
from noeta.plugins.permissions import classifier as c
from noeta.plugins.permissions import guards as g
from noeta.plugins.permissions import plan as pl
from noeta.plugins.permissions import policy as p
from noeta.plugins.permissions import scope as sc
from noeta.plugins.permissions import wording as w
from noeta.toolkit import permission_rules as r
from noeta.toolkit.sandbox import leaves_sandbox, runs_sandboxed, sandbox_of
from noeta.toolkit.shell import parse_argv, rule_spec_from_command, rule_spec_text

log = logging.getLogger("noeta.permissions")

NAME = "permissions"
#: The plugin whose ``cwd`` record says where the next shell command starts.
SHELL_PLUGIN = "shell"

#: The approval callback: ``(tool_name, input) -> bool`` (or awaitable).
CanUseTool = Callable[[str, dict[str, Any]], "bool | Awaitable[bool]"]

#: Text a call refused by ``can_use_tool`` returns (0.6's deny default).
DENIED_BY_HUMAN = "denied by human"

#: Decisions kept in the folded state (per call id).
_KEEP = 256
#: Task config key a child reads its parent's remembered rules from.
INHERITED_RULES_KEY = "permission_rules"
#: The signal front ends post to switch the mode (``Client.signal``).
MODE_SIGNAL = _MODE_SIGNAL
#: The signal the plan-name side request answers with.
PLAN_NAME_SIGNAL = "plan_name"
#: Claude Code's denial limits before auto mode falls back to asking.
MAX_CONSECUTIVE_DENIALS = 3
MAX_TOTAL_DENIALS = 20
#: Places in the joined system turn (``Reminder.order``): before the token
#: budget, after the skill list.
ORDER_PLAN = 64
ORDER_AUTO = 70
#: Betas Claude Code's auto-mode requests carry: every main-loop and
#: sub-agent request names auto mode; the main loop also the server-side
#: classifier one, until its first compaction (as recorded).
AUTO_BETAS = ("afk-mode-2026-01-31",)
MAIN_AUTO_BETAS = ("dangerous-tool-use-2026-09-03", "afk-mode-2026-01-31")
#: The local command wrapper ``/plan`` sends (Claude Code's tags).
PLAN_COMMAND = ("<command-name>/plan</command-name>\n            <command-message>plan"
                "</command-message>\n            <command-args>{args}</command-args>\n\n"
                "<local-command-stdout>Enabled plan mode</local-command-stdout>")
_PLAN_COMMAND_HEAD = "<command-name>/plan</command-name>"


class UntrustedProjectShellAllowlistWarning(UserWarning):
    """Kept for hosts that filter it; the project allowlist file is gone
    (Claude Code keeps remembered rules in ``.noeta/settings.local.json``)."""


@dataclass(frozen=True)
class PermState:
    """Folded from this extension's records."""

    #: Allow rules remembered from "always" answers (rule strings).
    rules: tuple[str, ...] = ()
    #: The mode switched to during the task (``None``: the starting one).
    mode: str | None = None
    #: Plan mode: ``{"path", "pre_mode", "entered_seq"}``, kept after it ends.
    plan: dict[str, Any] | None = None
    steps: int = 0
    tool_calls: int = 0
    cost_usd: float = 0.0
    #: ``call_id -> "allow" | "deny"`` decided by ``can_use_tool``.
    auto: dict[str, str] = field(default_factory=dict)
    #: ``call_id -> {"rules": [...]}``: what an "always" answer remembers.
    asked: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: ``call_id -> {"decision", "reason", "category"}`` from the classifier.
    classified: dict[str, dict[str, Any]] = field(default_factory=dict)
    consecutive_denials: int = 0
    total_denials: int = 0
    #: The classifier moved to the main model.
    demoted: bool = False
    #: Notes owed to the next request: ``auto_guidance``, ``plan_exited``,
    #: ``plan_active``.
    due: tuple[str, ...] = ()
    #: The seq of the newest prompt the plan-mode text was sent for.
    plan_noted: int = 0
    #: After a compaction, the auto-mode note waits for a prompt or a tool
    #: result newer than this seq (Claude Code's).
    guidance_after: int = 0


def inherited_rules(state: Any) -> list[str]:
    """A task's remembered rules, its own and those it inherited: what a
    child it spawns starts with (``config["permission_rules"]``)."""
    st = state.ext.get(NAME)
    own = list(st.rules) if isinstance(st, PermState) else []
    parent = state.config.get(INHERITED_RULES_KEY)
    rules = [x for x in parent if isinstance(x, str)] if isinstance(parent, list) else []
    return [*rules, *(x for x in own if x not in rules)]


def _directory(entry: object) -> str:
    if not isinstance(entry, str) or not entry.strip():
        raise ValueError(f"additional_directories entries must be non-empty paths; got {entry!r}")
    return entry.strip()


@dataclass(frozen=True)
class _Decision:
    """What the permission system says about a call before any classifier."""

    kind: str  # "allow" | "deny" | "ask"
    reason: str = ""
    firm: bool = False
    #: The person-facing kind of ask: ``edit`` / ``command`` / ``read`` /
    #: ``protected`` / ``plan`` / ``tool`` / ``fetch``.
    why: str = ""
    always: tuple[str, ...] = ()
    always_label: str | None = None
    mode_choice: str | None = None
    #: In auto mode, an ask the classifier answers.
    classify: bool = False


class Permissions(Extension):
    """Constructor arguments. The first group is Claude Code's ``permissions``
    settings object as plain data (a host reading ``.noeta/settings*.json``
    passes ``defaultMode`` as ``mode``, ``additionalDirectories`` as
    ``additional_directories`` and ``autoMode`` as ``auto_mode``; the rule
    lists of every settings file concatenated):

    * ``mode`` — the starting permission mode, one of
      :data:`policy.PERMISSION_MODES` (default ``auto``, as Claude Code). A
      task can start in another with ``config["permission_mode"]``; front
      ends switch it with the ``permission_mode`` signal
      (``Client.signal(task_id, "permission_mode", {"mode": ...})``).
    * ``allow`` / ``deny`` / ``ask`` — rule strings
      (:mod:`noeta.toolkit.permission_rules`: ``Bash(git push *)``,
      ``Read(./.env)``, ``WebFetch(domain:example.com)``, ``mcp__server``).
      A deny rule wins over everything, then an ask rule (which no mode
      skips), then an allow rule.
    * ``additional_directories`` — directories beyond the working directory
      that file tools may read and edit as if they were inside it
      (absolute, ``~/...``, or relative to the working directory).
    * ``auto_mode`` — Claude Code's ``autoMode`` object for the auto-mode
      classifier: ``allow``, ``soft_deny``, ``hard_deny`` and
      ``environment``, each a list of plain-language lines.
    * ``disable_bypass_mode`` — refuse ``bypassPermissions`` (Claude Code's
      ``disableBypassPermissionsMode``): as the starting mode, from a task's
      config (an agent file's ``permissionMode``) and as a switch.

    The rest are noeta's own:

    * ``classifier_model`` — the auto-mode classifier's model (default:
      ``claude-sonnet-5`` when the task runs a Claude model, else the task's
      model; a failing classifier model falls back to the task's).
    * ``plans_dir`` — where plan files go (default ``<noeta home>/plans``).
    * ``trusted_workspace`` / ``remember_to_project`` — "always" answers are
      also written to the project's ``.noeta/settings.local.json`` (trusted
      workspaces only). Only in a trusted workspace do the instruction files
      count as the person's intent to the auto-mode classifier.
    * ``shell_allowlist`` (``{"rule": "git push *"}`` specs) and
      ``webfetch_allowed_hosts`` (``example.com`` / ``*.example.com``) —
      shorthands for ``Bash(...)`` and ``WebFetch(domain:...)`` allow rules.
    * ``risk`` / ``default_risk`` / ``max_risk`` — which tools need
      permission at all (``low`` does not; :data:`policy.DEFAULT_RISK`), and
      a ceiling that denies tools above it.
    * ``allowed_tools`` / ``disallowed_tools`` — filter the offered tools by
      name (removed from the request, and denied if called anyway).
      Disallowing ``Bash`` also removes Monitor and TaskStop.
    * ``budget`` (:class:`guards.Budget`) or ``max_turns``; ``spawn_tools``;
      ``repetition_threshold`` / ``repetition_action`` /
      ``repetition_window``; ``pre_tool_use`` (:class:`guards.PreToolUseRule`).
    * ``can_use_tool`` — ``(tool, input) -> bool`` (sync or async) that
      answers this extension's asks instead of a person; recorded with
      ``resolver="can_use_tool"``.

    Remembered rules are per task; a child spawned by ``Subtasks`` starts
    with its parent's (``config["permission_rules"]``).
    """

    name = NAME

    def __init__(
        self,
        *,
        mode: str = "auto",
        allow: Sequence[str] = (),
        deny: Sequence[str] = (),
        ask: Sequence[str] = (),
        additional_directories: Sequence[str] = (),
        auto_mode: Mapping[str, Sequence[str]] | None = None,
        disable_bypass_mode: bool = False,
        classifier_model: str | None = None,
        plans_dir: str | None = None,
        risk: Mapping[str, str] | None = None,
        default_risk: str = "high",
        max_risk: str | None = None,
        allowed_tools: Iterable[str] | None = None,
        disallowed_tools: Iterable[str] = (),
        shell_allowlist: Sequence[Mapping[str, Any]] = (),
        trusted_workspace: bool = False,
        remember_to_project: bool = True,
        webfetch_allowed_hosts: Iterable[str] = (),
        budget: g.Budget | None = None,
        max_turns: int | None = None,
        spawn_tools: Iterable[str] = ("Agent", "Task"),
        repetition_threshold: int = 0,
        repetition_action: g.RepetitionAction = "require_approval",
        repetition_window: int = 8,
        pre_tool_use: Sequence[g.PreToolUseRule] = (),
        can_use_tool: CanUseTool | None = None,
    ) -> None:
        self.mode = p.check_mode(mode)
        self.disable_bypass = bool(disable_bypass_mode)
        if self.disable_bypass and self.mode == "bypassPermissions":
            raise ValueError("mode 'bypassPermissions' is disabled (disable_bypass_mode)")
        extra_allow = [f"Bash({rule_spec_text(s)})" for s in shell_allowlist]
        extra_allow += [f"WebFetch(domain:{h})" for h in
                        p.normalize_allowed_hosts(webfetch_allowed_hosts)]
        self.allow_rules = r.parse_rules([*allow, *extra_allow], "allow")
        self.deny_rules = r.parse_rules(deny, "deny")
        self.ask_rules = r.parse_rules(ask, "ask")
        self.additional_directories = tuple(_directory(d) for d in additional_directories)
        self.auto_mode = p.check_auto_mode(auto_mode)
        self.classifier_model = classifier_model
        self.plans_dir = plans_dir or pl.plans_dir()
        self.risk = {**p.DEFAULT_RISK, **dict(risk or {})}
        for name, level in [*self.risk.items(), ("<default>", default_risk)]:
            if level not in p.KNOWN_RISK_LEVELS:
                raise ValueError(f"risk of {name!r} must be one of "
                                 f"{p.KNOWN_RISK_LEVELS}; got {level!r}")
        if max_risk is not None and max_risk not in p.KNOWN_RISK_LEVELS:
            raise ValueError(f"max_risk must be one of {p.KNOWN_RISK_LEVELS}; got {max_risk!r}")
        self.default_risk = default_risk
        self.max_risk = max_risk
        self.allowed = frozenset(allowed_tools) if allowed_tools is not None else None
        self.disallowed = p.expand_disallowed(disallowed_tools)
        self.trusted = trusted_workspace
        self.remember_to_project = remember_to_project
        budget = budget or g.Budget()
        if max_turns is not None:
            if budget.max_iterations is not None:
                raise ValueError(
                    "Both `budget.max_iterations` and `max_turns` are set — they express "
                    "the same iteration cap and cannot be supplied together (ambiguous).")
            budget = replace(budget, max_iterations=max_turns)
        self.budget = budget
        self.spawn_tools = frozenset(spawn_tools)
        self.repetition = g.RepetitionPolicy(repetition_threshold, repetition_action,
                                             repetition_window)
        self.pre_rules = tuple(g.check_pre_rule(f"pre_tool_use[{i}]", rule)
                               for i, rule in enumerate(pre_tool_use))
        self.can_use_tool = can_use_tool
        self._exit_plan = pl.ExitPlanModeTool()
        self._background: set[asyncio.Future[Any]] = set()
        self._template: str | None = None
        #: Tasks this process sent the safeguards offer for, and compacted.
        self._probed: OrderedDict[str, None] = OrderedDict()
        self._compacted: OrderedDict[str, None] = OrderedDict()

    # ------------------------------------------------------------ state

    def inherit(self, state: Any) -> dict[str, Any]:
        """A child task starts with this task's remembered rules and its
        current mode (Claude Code's sub-agents run in their parent's)."""
        out: dict[str, Any] = {}
        rules = inherited_rules(state)
        if rules:
            out[INHERITED_RULES_KEY] = rules
        st = state.ext.get(NAME)
        mode = st.mode if isinstance(st, PermState) else None
        if mode is not None and mode != "plan":
            out["permission_mode"] = mode
        return out

    def initial_state(self) -> PermState:
        return PermState()

    def reduce(self, state: Any, kind: str, data: Any) -> Any:
        st: PermState = state if isinstance(state, PermState) else PermState()
        d = data if isinstance(data, dict) else {}
        if kind == "rule":
            new = [x for x in d.get("rules") or [] if isinstance(x, str) and x not in st.rules]
            return replace(st, rules=(*st.rules, *new)) if new else st
        if kind == "mode":
            mode = str(d.get("mode"))
            due = st.due
            if mode == "auto" and st.mode != "auto":
                due = (*due, "auto_guidance")
            plan = st.plan
            if mode == "plan" and isinstance(d.get("plan"), dict):
                plan = dict(d["plan"])
                due = (*due, "plan_active")
            return replace(st, mode=mode, plan=plan, due=due, consecutive_denials=0)
        if kind == "plan_exit":
            mode = str(d.get("mode") or "default")
            # No auto-mode note again: Claude Code sends only the plan-exit
            # note (r2 m1).
            due = (*st.due, "plan_exited")
            return replace(st, mode=mode, due=due)
        if kind == "noted":
            sent = set(d.get("kinds") or ())
            return replace(st, due=tuple(k for k in st.due if k not in sent),
                           plan_noted=int(d.get("plan_noted") or st.plan_noted))
        if kind == "compacted":
            extra: tuple[str, ...] = ("auto_guidance",) if d.get("auto") else ()
            extra += ("plan_active",) if d.get("plan") else ()
            return replace(st, due=(*st.due, *extra),
                           guidance_after=int(d.get("seq") or st.guidance_after))
        if kind == "step":
            return replace(st, steps=st.steps + 1)
        if kind == "tool_call":
            return replace(st, tool_calls=st.tool_calls + 1)
        if kind == "cost":
            return replace(st, cost_usd=st.cost_usd + float(d.get("usd") or 0.0))
        if kind == "auto":
            return replace(st, auto=_keep({**st.auto, str(d["call_id"]): str(d["decision"])}))
        if kind == "asked":
            return replace(st, asked=_keep({**st.asked, str(d["call_id"]): dict(d)}))
        if kind == "classified":
            classified = _keep({**st.classified, str(d["call_id"]): dict(d)})
            if d.get("decision") == "deny" and d.get("counted", True):
                cons, total = st.consecutive_denials + 1, st.total_denials + 1
            elif d.get("decision") == "allow":
                cons, total = 0, st.total_denials
            else:
                cons, total = st.consecutive_denials, st.total_denials
            if d.get("reset_total"):
                cons, total = 0, 0
            return replace(st, classified=classified, consecutive_denials=cons,
                           total_denials=total)
        if kind == "demoted":
            return replace(st, demoted=True)
        return st

    def _state(self, view: TaskView) -> PermState:
        st = view.ext_state(NAME)
        return st if isinstance(st, PermState) else PermState()

    def mode_of(self, view: TaskView) -> str:
        """The task's current permission mode."""
        st = self._state(view)
        if st.mode is not None:
            return st.mode
        mode = view.state.config.get("permission_mode")
        if isinstance(mode, str) and mode in p.PERMISSION_MODES and not (
                mode == "bypassPermissions" and self.disable_bypass):
            return mode
        return self.mode

    def _rules(self, view: TaskView) -> tuple[r.Rule, ...]:
        """Allow rules: the host's, then those remembered (inherited and own)."""
        remembered = []
        for text in inherited_rules(view.state):
            try:
                remembered.append(r.parse_rule(text))
            except ValueError:
                continue
        return (*self.allow_rules, *remembered)

    def _root(self, view: TaskView) -> str:
        cwd = view.state.config.get("cwd")
        if isinstance(cwd, str) and cwd:
            return cwd
        env_cwd = getattr(view.env, "cwd", None)
        return env_cwd if isinstance(env_cwd, str) and env_cwd else os.getcwd()

    def _scope(self, view: TaskView) -> sc.Scope:
        root = posixpath.normpath(self._root(view))
        home = os.path.expanduser("~")
        dirs = [root]
        for d in self.additional_directories:
            if d == "~" or d.startswith("~/"):
                d = home + d[1:]
            dirs.append(posixpath.normpath(d if posixpath.isabs(d) else posixpath.join(root, d)))
        return sc.Scope(dirs=tuple(dict.fromkeys(dirs)), home=home)

    # ------------------------------------------------------------ tools and commands

    def tools(self, view: TaskView) -> Sequence[Tool]:
        # Sub-agents do not plan for approval (none is offered it).
        return () if view.state.parent_id else (self._exit_plan,)

    async def commands(self, ctx: CommandContext) -> Sequence[Command]:
        return (Command(name="plan", description="Switch to plan mode; with a request, plan it",
                        argument_hint="[request]"),)

    async def run_command(self, ctx: CommandContext, name: str, args: str) -> str:
        return PLAN_COMMAND.format(args=args)

    # ------------------------------------------------------------ the gate

    async def before_tool(self, view: TaskView, call: ToolUse) -> Gate | None:
        name = call.name
        if name in self.disallowed:
            return Deny(reason=p.DENIED_BY_POLICY.format(name=name))
        if self.allowed is not None and name not in self.allowed:
            return Deny(reason=p.NOT_IN_ALLOWLIST.format(name=name))
        st = self._state(view)
        reason = g.budget_verdict(self.budget, self._counters(view, st, call),
                                  spawns=name in self.spawn_tools)
        if reason is not None:
            return Deny(reason=reason)
        risk = self.risk.get(name, self.default_risk)
        if self.max_risk is not None and (
                p.KNOWN_RISK_LEVELS.index(risk) > p.KNOWN_RISK_LEVELS.index(self.max_risk)):
            return Deny(reason=p.RISK_EXCEEDS.format(name=name, risk=risk, ceiling=self.max_risk))
        if self.repetition.threshold > 0:
            history = g.recent_calls(view.state.messages, call.id, self.repetition.window)
            rep = g.repetition_verdict(self.repetition, call, history)
            if rep is not None and rep[0] == "deny":
                return Deny(reason=rep[1])
            if rep is not None:
                return Ask(prompt=rep[1], detail={"by": NAME, "tool": name}, firm=True)
        hook = g.pre_tool_use_verdict(self.pre_rules, call)
        if hook is not None and hook[0] == "deny":
            return Deny(reason=hook[1])
        if hook is not None and hook[0] == "require_approval":
            return Ask(prompt=hook[1], detail={"by": NAME, "tool": name}, firm=True)

        decision = await self.decide(view, call)
        if decision.kind == "allow":
            return None
        if decision.kind == "deny":
            return Deny(reason=decision.reason)
        rules = list(decision.always)
        if rules and st.asked.get(call.id, {}).get("rules") != rules:
            view.record(NAME, "asked", {"call_id": call.id, "rules": rules})
        return Ask(prompt=decision.reason, firm=decision.firm, detail={
            "by": NAME, "tool": name, "reason": decision.reason,
            "title": w.TITLES.get(name, name),
            "always": rules or None, "always_label": decision.always_label,
            "choices": _choices(decision), "classify": decision.classify})

    async def decide(self, view: TaskView, call: ToolUse) -> _Decision:
        """Claude Code's order: deny rules, ask rules, then what the tool
        touches and the mode."""
        name, args = call.name, call.input
        mode = self.mode_of(view)
        scope = self._scope(view)
        ctx = r.RuleContext(root=scope.cwd, home=scope.home, cwd=scope.cwd)
        denied = r.first_match(self.deny_rules, name, args, ctx)
        if denied is not None:
            return _Decision("deny", w.DENIED_BY_RULE.format(name=name, rule=denied.text))
        asked = r.first_match(self.ask_rules, name, args, ctx)
        if asked is not None:
            return self._in_mode(mode, _Decision(
                "ask", w.ASK_RULE.format(rule=asked.text, name=name), firm=True, why="rule"),
                name)
        allow_rules = self._rules(view)
        if mode == "auto":
            allow_rules = tuple(x for x in allow_rules if not p.bypasses_classifier(x))
        found = await self._needs(view, call, mode, scope, ctx, allow_rules)
        return self._in_mode(mode, found, name)

    def _in_mode(self, mode: str, d: _Decision, name: str) -> _Decision:
        """What the mode makes of an ask."""
        if d.kind != "ask":
            return d
        if mode == "dontAsk":
            return _Decision("deny", w.DONT_ASK_DENIED.format(name=name))
        if mode == "bypassPermissions" and not d.firm and d.why != "protected":
            return _Decision("allow")
        if mode == "auto" and not d.firm:
            return replace(d, classify=True)
        return d

    async def _needs(self, view: TaskView, call: ToolUse, mode: str, scope: sc.Scope,
                     ctx: r.RuleContext, allow_rules: Sequence[r.Rule]) -> _Decision:
        name, args = call.name, call.input
        if name == pl.EXIT_PLAN_MODE:
            return _Decision("allow")
        allowed = r.first_match(allow_rules, name, args, ctx, for_allow=True) is not None
        if name in r.READ_TOOLS:
            path = _path(args) or scope.cwd
            if scope.contains(path) or allowed or self._in_plans(scope.absolute(path)):
                return _Decision("allow")
            target = scope.absolute(path)
            return _Decision("ask", w.ASK_READ_OUTSIDE.format(name=name, path=target),
                             why="read", always=(r.format_rule("Read", f"//{_dir_of(target)}/**"),),
                             always_label=w.CHOICE_ALWAYS_RULE.format(
                                 rules=f"Read(//{_dir_of(target)}/**)"))
        if name in r.EDIT_TOOLS:
            written = _path(args)
            target = scope.absolute(written) if written else scope.cwd
            plan = self._state(view).plan
            if mode == "plan" and plan and target == plan.get("path"):
                return _Decision("allow")
            if not scope.contains(target) and not self._in_plans(target):
                return _Decision("deny", w.OUTSIDE_WRITE.format(
                    name=name, dirs=", ".join(scope.dirs), path=target))
            if sc.is_protected(target) or sc.is_protected(await self._real(view, target)):
                # The tools write where a symlink points: judge that too.
                return _Decision("ask", w.ASK_PROTECTED.format(name=name, path=target),
                                 why="protected")
            if mode == "plan":
                return _Decision("ask", w.ASK_PLAN.format(name=name), why="plan")
            if allowed or mode in ("acceptEdits", "auto", "bypassPermissions"):
                return _Decision("allow")
            verb = w.ASK_EDIT if name != "Write" or os.path.exists(target) else w.ASK_CREATE
            return _Decision("ask", verb.format(name=name, path=target), why="edit",
                             mode_choice="acceptEdits")
        if name in r.SHELL_TOOLS:
            return await self._shell(view, args, mode, scope, ctx, allow_rules)
        if name == p.WEBFETCH_TOOL:
            if allowed:
                return _Decision("allow")
            url = args.get("url")
            host = r.url_host(url) if isinstance(url, str) else None
            rule = (r.format_rule("WebFetch", f"domain:{host}"),) if host else ()
            return _Decision("ask", w.ASK_FETCH.format(name=name, host=host or url), why="fetch",
                             always=rule, always_label=w.CHOICE_ALWAYS_RULE.format(
                                 rules=rule[0]) if rule else None, mode_choice="auto")
        if allowed:
            return _Decision("allow")
        if self.risk.get(name, self.default_risk) == "low":
            if mode == "auto" and name not in p.AUTO_SAFE_TOOLS:
                return _Decision("ask", w.ASK_TOOL.format(name=name), why="tool", classify=True)
            return _Decision("allow")
        if mode == "plan" and name not in p.AUTO_SAFE_TOOLS:
            return _Decision("ask", w.ASK_PLAN.format(name=name), why="plan")
        return _Decision("ask", w.ASK_TOOL.format(name=name), why="tool", always=(name,),
                         always_label=w.CHOICE_ALWAYS_RULE.format(rules=name),
                         mode_choice="auto")

    async def _shell(self, view: TaskView, args: Mapping[str, Any], mode: str,
                     scope: sc.Scope, ctx: r.RuleContext,
                     allow_rules: Sequence[r.Rule]) -> _Decision:
        command = args.get("command")
        if not isinstance(command, str) or not command.strip():
            return _Decision("ask", w.ASK_COMMAND, why="command")
        if p.CONFIG_DIR in command and mode != "bypassPermissions":
            return _Decision("ask", w.ASK_PROTECTED.format(name="Bash", path=p.CONFIG_DIR),
                             why="protected")
        sandbox = sandbox_of(view.env)
        disable = args.get("dangerouslyDisableSandbox")
        if leaves_sandbox(sandbox, command, disable):
            # Leaving the sandbox is asked about whatever the allow rules say.
            return _Decision("ask", w.ASK_UNSANDBOXED.format(name="Bash"), why="sandbox")
        if (sandbox is not None and sandbox.settings.auto_allow_bash and mode != "plan"
                and runs_sandboxed(sandbox, command, disable)):
            return _Decision("allow")
        here = self._shell_scope(view, scope)
        if sc.read_only(command, here):
            return _Decision("allow")
        bash_rules = [x for x in allow_rules if x.tool == "Bash" and x.content is not None]
        if any(x.tool == "Bash" and x.content is None for x in allow_rules):
            return _Decision("allow")
        edit_rules = [x for x in allow_rules if x.tool == "Edit" and x.content is not None]
        real_scope = await self._real_scope(view, scope)

        async def writable(path: str) -> bool:
            real = await self._real(view, path)
            if path in scope.dirs or sc.is_protected(path) or sc.is_protected(real):
                return False
            if mode in ("acceptEdits", "bypassPermissions"):
                return scope.contains(path) and real_scope.contains(real)
            return any(r.path_pattern_matches(x.content or "", path, ctx)
                       and r.path_pattern_matches(x.content or "", real, ctx)
                       for x in edit_rules)

        open_parts: list[sc.Part] = []
        for part in sc.shell_parts(command, here):
            if part.read_only or any(r.shell_rule_matches(x.content or "", part.text, every=True)
                                     for x in bash_rules):
                continue
            if part.writes is not None and all([await writable(x) for x in part.writes]):
                continue
            open_parts.append(part)
        if not open_parts:
            return _Decision("allow")
        if mode == "plan":
            return _Decision("ask", w.ASK_PLAN.format(name="Bash"), why="plan")
        always, label = _shell_always(open_parts, here)
        return _Decision("ask", w.ASK_COMMAND, why="command", always=always,
                         always_label=label, mode_choice="auto")

    def _shell_scope(self, view: TaskView, scope: sc.Scope) -> sc.Scope:
        """``scope`` at the directory the shell's next command starts in
        (the shell plugin's ``cwd`` record)."""
        cwd = getattr(view.ext_state(SHELL_PLUGIN), "cwd", None)
        if isinstance(cwd, str) and cwd and posixpath.isabs(cwd):
            cwd = posixpath.normpath(cwd)
            if scope.contains(cwd):
                return scope.at(cwd)
        return scope

    async def _real(self, view: TaskView, path: str) -> str:
        """``path`` with its symlinks resolved as the task's env sees them
        (a missing tail kept as named)."""
        try:
            if view.env is not None:
                return posixpath.normpath(await view.env.realpath(path))
            return await asyncio.to_thread(os.path.realpath, path)
        except Exception:  # noqa: BLE001 — unresolvable: judged as written
            return path

    async def _real_scope(self, view: TaskView, scope: sc.Scope) -> sc.Scope:
        dirs = [await self._real(view, d) for d in scope.dirs]
        return sc.Scope(dirs=tuple(dict.fromkeys(dirs)), home=scope.home)

    def _in_plans(self, path: str) -> bool:
        base = posixpath.normpath(self.plans_dir)
        return path.startswith(base.rstrip("/") + "/")

    def _counters(self, view: TaskView, st: PermState, call: ToolUse) -> g.Counters:
        before = 0
        for cl in view.state.calls:
            if cl.use.id == call.id:
                break
            if cl.result is None:
                before += 1
        depth = view.state.config.get("subtask_depth")
        if not isinstance(depth, int):
            depth = 1 if view.state.parent_id else 0
        return g.Counters(iterations=st.steps, tool_calls=st.tool_calls + before,
                          cost_usd=st.cost_usd, spawned=len(view.state.spawned), depth=depth)

    # ------------------------------------------------------------ answering asks

    async def resolve_ask(self, view: TaskView, call: ToolUse,
                          asks: Sequence[Ask]) -> Gate | None:
        mine = [a for a in asks if a.detail.get("by") == NAME]
        if not mine or len(mine) != len(asks):
            return None
        if self.can_use_tool is not None:
            return await self._auto(view, call)
        if not all(a.detail.get("classify") for a in mine):
            return None
        return await self._classify(view, call, mine[0])

    async def _auto(self, view: TaskView, call: ToolUse) -> Gate:
        decision = self._state(view).auto.get(call.id)
        if decision is None:
            assert self.can_use_tool is not None
            verdict = self.can_use_tool(call.name, dict(call.input))
            if inspect.isawaitable(verdict):
                verdict = await verdict
            decision = "allow" if verdict else "deny"
            view.record(NAME, "auto", {"call_id": call.id, "decision": decision,
                                       "resolver": "can_use_tool"})
        return Allow() if decision == "allow" else Deny(reason=DENIED_BY_HUMAN)

    async def _classify(self, view: TaskView, call: ToolUse, ask: Ask) -> Gate | None:
        st = self._state(view)
        done = st.classified.get(call.id)
        if done is None:
            done = await self._run_classifier(view, call, st)
            view.record(NAME, "classified", done)
        if done.get("fallback"):
            return Ask(prompt=str(done.get("fallback")), detail={**ask.detail, "classify": False},
                       firm=True)
        if done["decision"] == "allow":
            return Allow()
        if done.get("counted", True):
            return Deny(reason=w.CLASSIFIER_BLOCKED.format(reason=done.get("reason") or ""))
        return Deny(reason=w.CLASSIFIER_NO_VERDICT.format(reason=done.get("reason") or ""))

    async def _run_classifier(self, view: TaskView, call: ToolUse,
                              st: PermState) -> dict[str, Any]:
        out: dict[str, Any] = {"call_id": call.id}
        try:
            verdict, demoted = await self._verdict(view, call, st)
        except Exception as exc:  # noqa: BLE001 — no verdict holds the call back
            log.warning("auto-mode classifier failed for %s: %s", call.name, exc)
            return {**out, "decision": "deny", "counted": False,
                    "reason": w.CLASSIFIER_UNAVAILABLE}
        if demoted:
            view.record(NAME, "demoted")
        if verdict.block is None:
            return {**out, "decision": "deny", "counted": False,
                    "reason": w.CLASSIFIER_UNPARSED}
        if not verdict.block:
            return {**out, "decision": "allow"}
        out.update(decision="deny", reason=verdict.reason, category=verdict.category)
        cons, total = st.consecutive_denials + 1, st.total_denials + 1
        if cons >= MAX_CONSECUTIVE_DENIALS or total >= MAX_TOTAL_DENIALS:
            text = (w.DENIAL_LIMIT_TOTAL.format(n=total, reason=verdict.reason)
                    if total >= MAX_TOTAL_DENIALS else
                    w.DENIAL_LIMIT_CONSECUTIVE.format(n=cons, reason=verdict.reason))
            out.update(fallback=text, reset_total=total >= MAX_TOTAL_DENIALS)
        return out

    def _model(self, view: TaskView, st: PermState) -> tuple[str, bool]:
        """The classifier model and whether it is the fast one."""
        main = view.model or str(view.state.config.get("model") or "")
        if self.classifier_model:
            return self.classifier_model, self.classifier_model != main
        if st.demoted or not main.startswith("claude"):
            return main, False
        return c.FAST_MODEL, True

    async def _verdict(self, view: TaskView, call: ToolUse,
                       st: PermState) -> tuple[c.Verdict, bool]:
        if view.complete is None:
            raise RuntimeError("no model is available to the classifier")
        transcript = self._transcript(view, call, st)
        instructions = await self._instructions(view)
        prompt = c.render_prompt(self._prompt(view), self.auto_mode, self._deny_texts(view))
        model, fast = self._model(view, st)
        demoted = False

        async def stage(n: int) -> str:
            nonlocal model, fast, demoted
            assert view.complete is not None
            request = c.classifier_request(model=model, prompt=prompt, instructions=instructions,
                                           transcript=transcript, stage=n, fast=fast,
                                           trusted=self.trusted)
            try:
                response = await view.complete(request)
            except Exception:
                main = view.model or str(view.state.config.get("model") or "")
                if not fast or model == main or self.classifier_model:
                    raise
                model, fast, demoted = main, False, True
                response = await view.complete(c.classifier_request(
                    model=model, prompt=prompt, instructions=instructions,
                    transcript=transcript, stage=n, fast=False, trusted=self.trusted))
            return response.message.text

        first = c.parse_stage1(await stage(1))
        if first.block is False:
            return first, demoted
        return c.parse_stage2(await stage(2)), demoted

    def _transcript(self, view: TaskView, call: ToolUse, st: PermState) -> list[str]:
        summary_at = len(view.state.compaction.lead) if view.state.compaction else None
        entries = c.transcript_entries(view.state.messages, call.id, summary_at=summary_at,
                                       shown=st.classified)
        if not entries or entries[-1].call_id != call.id:
            rendered = c.render_call(call)
            entries.append(c.Entry(c._entry(call.name, rendered), call.id))
        return c.chunks(entries, [k for k in st.classified if k != call.id])

    async def _instructions(self, view: TaskView) -> str:
        texts = [m.text for m in await view.residents(lead=True) if m.text.strip()]
        return "\n\n".join(texts)

    def _deny_texts(self, view: TaskView) -> list[str]:
        return [x.text for x in self.deny_rules] + list(view.denied_tools)

    def _prompt(self, view: TaskView) -> str:
        if self._template is None:
            self._template = c.prompt_template()
        return view.prompt(f"{NAME}/auto_mode_classifier", self._template)

    # ------------------------------------------------------------ events

    async def on_start(self, view: TaskView) -> None:
        if self._state(view).mode is None:
            view.record(NAME, "mode", {"mode": self.mode_of(view)})

    async def on_event(self, view: TaskView, recorded: Recorded) -> None:
        ev = recorded.event
        if isinstance(ev, ModelReplied) and self.budget.max_cost_usd is not None:
            from noeta.core.models import price

            model = ev.response.model or str(view.state.config.get("model") or "")
            view.record(NAME, "cost", {"usd": price(model, ev.response.usage) if model else 0.0})
        elif isinstance(ev, InputTaken):
            for item in ev.items:
                body = item.body
                if isinstance(body, Answer):
                    await self._remember(view, body)
                elif isinstance(body, External) and body.name == MODE_SIGNAL:
                    data = body.data if isinstance(body.data, dict) else {}
                    self._switch(view, data.get("mode"), recorded.seq)
                elif isinstance(body, External) and body.name == PLAN_NAME_SIGNAL:
                    name = (body.data or {}).get("name") if isinstance(body.data, dict) else None
                    if isinstance(name, str) and name:
                        view.record(NAME, "plan_name", {"name": name})
                        view.notify(f"plan: {name}")
                elif isinstance(body, Say) and body.message.role == "user" and \
                        body.message.text.startswith(_PLAN_COMMAND_HEAD):
                    self._switch(view, "plan", recorded.seq)
        elif isinstance(ev, Compacted):
            self._compacted[view.task_id] = None
            _trim(self._compacted)
            mode = self.mode_of(view)
            view.record(NAME, "compacted", {"auto": mode == "auto", "plan": mode == "plan",
                                            "seq": recorded.seq})
        elif isinstance(ev, ExtensionRecorded) and ev.extension == "core" and \
                ev.kind == "parked" and isinstance(ev.data, dict):
            self._name_plan(view, ev.data)

    def _switch(self, view: TaskView, mode: Any, seq: int) -> None:
        if not isinstance(mode, str) or mode not in p.PERMISSION_MODES:
            return
        if mode == "bypassPermissions" and self.disable_bypass:
            view.notify("bypassPermissions is disabled here")
            return
        current = self.mode_of(view)
        if mode == current:
            return
        data: dict[str, Any] = {"mode": mode}
        if mode == "plan":
            st = self._state(view)
            path = (st.plan or {}).get("path") or pl.new_plan_path(self.plans_dir)
            data["plan"] = {"path": path, "pre_mode": current, "entered_seq": seq}
        view.record(NAME, "mode", data)

    def _name_plan(self, view: TaskView, parked: Mapping[str, Any]) -> None:
        """The plan-name side request, when ``ExitPlanMode`` shows the plan."""
        call = next((cl.use for cl in view.state.calls
                     if cl.use.id == parked.get("call_id")), None)
        if call is None or call.name != pl.EXIT_PLAN_MODE or view.complete is None \
                or view.post is None:
            return
        questions = (parked.get("detail") or {}).get("questions") or [{}]
        text = str(questions[0].get("question") or "")
        complete, post = view.complete, view.post
        model = view.model or str(view.state.config.get("model") or "")

        async def name_it() -> None:
            try:
                response = await complete(pl.plan_name_request(model, text))
                name = pl.parse_plan_name(response.message.text)
                if name:
                    await post(External(name=PLAN_NAME_SIGNAL, data={"name": name}),
                               dedupe_key=f"{PLAN_NAME_SIGNAL}:{call.id}")
            except Exception:
                log.debug("plan name failed", exc_info=True)

        job = asyncio.ensure_future(name_it())
        self._background.add(job)
        job.add_done_callback(self._background.discard)

    async def _remember(self, view: TaskView, answer: Answer) -> None:
        """An "always" allow remembers the rules the card showed; a choice
        to switch modes switches."""
        if answer.decision != "allow":
            return
        st = self._state(view)
        waiting = view.state.waiting
        detail = waiting.detail.get(answer.request_id) if waiting is not None else None
        if isinstance(detail, dict) and detail.get("by") not in (None, NAME):
            return
        data = answer.data if isinstance(answer.data, dict) else {}
        mode = data.get("mode")
        if isinstance(mode, str):
            self._switch(view, mode, 0)
        if not answer.always:
            return
        rules = [x for x in (st.asked.get(answer.request_id) or {}).get("rules") or []
                 if isinstance(x, str) and x not in st.rules]
        if not rules:
            return
        view.record(NAME, "rule", {"rules": rules})
        if self.trusted and self.remember_to_project:
            try:
                await self._append_project(view, rules)
            except Exception:
                log.warning("could not save rules to %s", p.LOCAL_SETTINGS, exc_info=True)

    async def _append_project(self, view: TaskView, rules: Sequence[str]) -> None:
        path = os.path.join(self._root(view), p.LOCAL_SETTINGS)
        raw = await self._read(view, path)
        text = p.add_allow_rules(raw, rules)
        if text is None:
            return
        data = text.encode("utf-8")
        if view.env is not None:
            await view.env.write_bytes(path, data)
        else:
            def write() -> None:
                Path(path).parent.mkdir(parents=True, exist_ok=True)
                Path(path).write_bytes(data)

            await asyncio.to_thread(write)

    async def _read(self, view: TaskView, path: str) -> bytes | None:
        try:
            if view.env is not None:
                return await view.env.read_bytes(path)
            return await asyncio.to_thread(Path(path).read_bytes)
        except Exception:  # noqa: BLE001 — absent or unreadable
            return None

    # ------------------------------------------------------------ requests

    async def before_model(self, view: TaskView, request: ModelRequest) -> ModelRequest:
        import msgspec

        if self.budget.max_iterations is not None:
            view.record(NAME, "step")
        if self.allowed is not None or self.disallowed:
            tools = [t for t in request.tools if self._offered(t.name)]
            if len(tools) != len(request.tools):
                request = msgspec.structs.replace(request, tools=tools)
        mode = self.mode_of(view)
        plan = self._state(view).plan or {}
        if mode != "auto" and not (mode == "plan" and plan.get("pre_mode") == "auto"):
            return request
        # Per process, as Claude Code per session: the first main request
        # offers the server-side check (the API answers an API-key session
        # that it does not run it), and its beta rides along until the first
        # compaction.
        main = view.state.parent_id is None
        task = view.task_id
        wanted = MAIN_AUTO_BETAS if main and task not in self._compacted else AUTO_BETAS
        betas = [*request.betas, *(b for b in wanted if b not in request.betas)]
        fields = dict(request.vendor_fields)
        if main and task not in self._probed:
            fields["safeguards"] = [{"type": "dangerous_tool_use",
                                     "classifier_context": self._context(view)}]
            self._probed[task] = None
            _trim(self._probed)
        return msgspec.structs.replace(request, betas=betas, vendor_fields=fields)

    def _offered(self, name: str) -> bool:
        return name not in self.disallowed and (self.allowed is None or name in self.allowed)

    def _context(self, view: TaskView) -> dict[str, Any]:
        """What Claude Code tells the API about its permission state on the
        first auto-mode request (the server answers an API-key session that
        it does not classify, and the classifier runs here)."""
        scope = self._scope(view)
        return {
            "v": 1, "permission_mode": "auto", "platform": _platform(), "live_cwd": scope.cwd,
            "home_dir": scope.home,
            "rule_roots": {"userSettings": str(Path(self.plans_dir).parent),
                           "projectSettings": scope.cwd, "localSettings": scope.cwd},
            "trusted_directories": {
                "primary": {"path": scope.cwd, "resolved": [scope.cwd]},
                "additional": list(scope.dirs[1:]), "network": [],
                "block_reads_outside_working_directories": False},
            "rules": {"allow": [x.text for x in self._rules(view)],
                      "deny": [x.text for x in self.deny_rules],
                      "ask": [x.text for x in self.ask_rules]},
            "auto_mode": {k: list(v) for k, v in self.auto_mode.items()},
            "artifact_consent_holdback": False, "case_insensitive_paths":
                sys.platform == "darwin",
            "restricted": False, "is_remote_mode": False, "classify_all_shell": False,
            "user_identity": c._user_name(),
            "git_state": {"cwd": scope.cwd, "root": scope.cwd, "branch": None,
                          "default_branch": None, "status": None, "visibility": None},
        }

    async def after_tool(self, view: TaskView, call: ToolUse, output: ToolOutput) -> ToolOutput:
        if self.budget.max_tool_calls is not None:
            view.record(NAME, "tool_call")
        return output

    # ------------------------------------------------------------ reminders

    async def reminders(self, view: TaskView) -> Sequence[Reminder]:
        st = self._state(view)
        mode = self.mode_of(view)
        out: list[Reminder] = []
        sent: list[str] = []
        plan_noted = st.plan_noted
        if mode == "plan" and st.plan is not None:
            newest = _newest_prompt(view)
            entered = int(st.plan.get("entered_seq") or 0)
            same_turn = view.state.steps_in_turn == 0 and newest == entered
            if ("plan_active" in st.due and not same_turn) or (
                    newest > max(st.plan_noted, entered) and view.state.steps_in_turn == 0):
                out.append(Reminder(text=pl.plan_active_text(
                    st.plan["path"], await self._exists(view, st.plan["path"])),
                    kind="plan_active", role="system", order=ORDER_PLAN))
                sent.append("plan_active")
                plan_noted = max(newest, entered)
        if "plan_exited" in st.due and st.plan is not None:
            out.append(Reminder(text=pl.plan_exited_text(st.plan["path"]), kind="plan_exited",
                                role="system", order=ORDER_PLAN))
            sent.append("plan_exited")
        # The main conversation's first request; a sub-agent's second (as
        # Claude Code sends them).
        replies = sum(1 for m in view.state.history if m.role == "assistant")
        first = replies == (1 if view.state.parent_id else 0)
        guidance = "auto_guidance" in st.due and _new_input(view, st.guidance_after) \
            and view.state.parent_id is None
        if mode == "auto" and (guidance or first):
            out.append(Reminder(text=w.AUTO_MODE_GUIDANCE, kind="auto_guidance", role="system",
                                order=ORDER_AUTO))
            sent.append("auto_guidance")
        stale = [k for k in st.due if k not in sent and (
            (k == "auto_guidance" and (mode != "auto" or view.state.parent_id is not None))
            or (k == "plan_active" and mode != "plan"))]
        if sent or stale or plan_noted != st.plan_noted:
            view.record(NAME, "noted", {"kinds": [*sent, *stale], "plan_noted": plan_noted})
        return out

    async def _exists(self, view: TaskView, path: str) -> bool:
        try:
            if view.env is not None:
                stat = await view.env.stat(path)
                return stat is not None
        except Exception:  # noqa: BLE001
            pass
        return await asyncio.to_thread(os.path.isfile, path)


# ---------------------------------------------------------------- helpers


def _trim(d: OrderedDict[str, None]) -> None:
    while len(d) > 4096:
        d.popitem(last=False)


def _keep(d: dict[str, Any]) -> dict[str, Any]:
    while len(d) > _KEEP:
        d.pop(next(iter(d)))
    return d


def _path(args: Mapping[str, Any]) -> str | None:
    for key in ("file_path", "notebook_path", "path"):
        value = args.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _dir_of(path: str) -> str:
    return posixpath.dirname(path).lstrip("/") or ""


def _new_input(view: TaskView, after: int) -> bool:
    """Whether a prompt or a tool result came in after log seq ``after``."""
    return any(seq > after and m.role == "user" and (
        m.origin in (None, "human") or any(isinstance(b, ToolResult) for b in m.content))
        for seq, m in zip(view.state.message_seqs, view.state.messages, strict=False))


def _newest_prompt(view: TaskView) -> int:
    return max((seq for seq, m in zip(view.state.message_seqs, view.state.messages,
                                      strict=False)
                if m.role == "user" and m.origin in (None, "human") and seq), default=0)


def _platform() -> str:
    return {"darwin": "macos", "win32": "windows"}.get(sys.platform, sys.platform)


def _shell_always(parts: Sequence[sc.Part],
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


def _choices(d: _Decision) -> list[dict[str, str]]:
    """The card's options (``id`` is what the answer does)."""
    out = [{"id": "allow", "label": w.CHOICE_YES}]
    if d.always and d.always_label:
        out.append({"id": "always", "label": d.always_label})
    if d.mode_choice and not d.firm:
        out.append({"id": f"mode:{d.mode_choice}", "label": w.CHOICE_MODE[d.mode_choice]})
    out.append({"id": "deny", "label": w.CHOICE_NO})
    return out


