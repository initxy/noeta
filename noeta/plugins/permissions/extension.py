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
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from noeta.core import records
from noeta.core.errors import TurnFailed
from noeta.core.events import (
    DEFAULT_TENANT,
    Answer,
    Compacted,
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
    Rewrite,
    TaskView,
)
from noeta.core.tool import Tool, ToolContext, ToolOutput
from noeta.core.types import ModelRequest, ToolUse
from noeta.plugins.permissions import auto as a
from noeta.plugins.permissions import fence as fc
from noeta.plugins.permissions import guards as g
from noeta.plugins.permissions import modes as md
from noeta.plugins.permissions import plan as pl
from noeta.plugins.permissions import policy as p
from noeta.plugins.permissions import wording as w
from noeta.plugins.permissions.decision import Decision, choices
from noeta.plugins.permissions.rule_check import RuleCheck
from noeta.plugins.permissions.rule_specs import rule_spec_text
from noeta.plugins.permissions.state import (
    INHERITED_RULES_KEY,
    NAME,
    PermState,
    fold,
    inherited_rules,
    state_of,
)
from noeta.toolkit import permission_modes as pm
from noeta.toolkit import permission_rules as r
from noeta.toolkit.paths import plans_dir as default_plans_dir
from noeta.toolkit.permission_modes import MODE_SIGNAL as MODE_SIGNAL
from noeta.toolkit.signals import RESUMED_SIGNAL
from noeta.toolkit.tenant_fence import TenantFence

__all__ = ["DENIED_BY_HUMAN", "MODE_SIGNAL", "CanUseTool", "PermState", "Permissions",
           "UntrustedProjectShellAllowlistWarning", "inherited_rules"]

log = logging.getLogger("noeta.permissions")

#: The approval callback: ``(tool_name, input) -> bool`` (or awaitable).
CanUseTool = Callable[[str, dict[str, Any]], "bool | Awaitable[bool]"]

#: Text a call refused by ``can_use_tool`` returns (0.6's deny default).
DENIED_BY_HUMAN = "denied by human"

#: The reason a turn fails with on a worker that may not run the task's
#: tenant (``tenant_refusal``).
TENANT_NOT_ISOLATED = "tenant_not_isolated"
#: The reason a tenant's turn fails with when its working directory now
#: resolves outside its workspace or into a place it may not write.
TENANT_CWD_REFUSED = "tenant_cwd_refused"


class UntrustedProjectShellAllowlistWarning(UserWarning):
    """Kept for hosts that filter it; the project allowlist file is gone
    (Claude Code keeps remembered rules in ``.noeta/settings.local.json``)."""


def _directory(entry: object) -> str:
    if not isinstance(entry, str) or not entry.strip():
        raise ValueError(f"additional_directories entries must be non-empty paths; got {entry!r}")
    return entry.strip()


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
      count as the person's intent to the auto-mode classifier. A callable
      ``(cwd) -> bool`` decides it for each task's working directory.
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
    * ``tenant_workspaces`` (tenant -> workspace), ``store_files``,
      ``spill_root``, ``server_dirs`` (the default tenant's workspace) and
      ``deny_read`` (the operator's ``denyRead``) — the file tools of a task
      of a tenant other than the default one never reach the protected set
      of :mod:`noeta.toolkit.tenant_fence` and write only in its workspace
      and its own places; no rule, mode or answer lifts it, and
      ``additional_directories`` are not its. Such a task keeps its plan
      files under ``plans_dir/<tenant>``.
    * ``tenant_refusal`` — why this worker may not run tools of tenants
      other than the default one (``noeta worker`` without an OS-level
      boundary): such a task's turn fails with ``tenant_not_isolated`` and
      this reason before any model call, and its calls are denied.

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
        trusted_workspace: bool | Callable[[str], bool] = False,
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
        tenant_workspaces: Mapping[str, str] | None = None,
        store_files: Sequence[str] = (),
        spill_root: str | None = None,
        server_dirs: Sequence[str] = (),
        deny_read: Sequence[str] = (),
        tenant_refusal: str | None = None,
    ) -> None:
        self.tenant_refusal = tenant_refusal
        self.mode = p.check_mode(mode)
        self.disable_bypass = bool(disable_bypass_mode)
        #: What front ends offer for this task (published on start).
        self._modes = pm.Modes().without("bypassPermissions") if self.disable_bypass \
            else pm.Modes()
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
        self.plans_dir = plans_dir or str(default_plans_dir())
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
        self.fence = TenantFence(spill_root=spill_root, plans_dir=self.plans_dir,
                                 store_files=tuple(store_files),
                                 workspaces=dict(tenant_workspaces or {}),
                                 server_dirs=tuple(server_dirs),
                                 deny_read=tuple(deny_read))
        self._check = RuleCheck(
            allow_rules=self.allow_rules, deny_rules=self.deny_rules, ask_rules=self.ask_rules,
            additional_directories=self.additional_directories, risk=self.risk,
            default_risk=self.default_risk, plans_dir=self.plans_dir)
        self._classifier = a.Classifier(model=classifier_model, auto_mode=self.auto_mode,
                                        deny_rules=self.deny_rules, trusted=self.trusts)
        self._exit_plan = pl.ExitPlanModeTool()
        self._enter_plan = pl.EnterPlanModeTool(self._enter_plan_mode)
        self._background: set[asyncio.Future[Any]] = set()

    def trusts(self, view: TaskView) -> bool:
        """Whether this task's working directory is a trusted workspace."""
        trusted = self.trusted
        if not callable(trusted):
            return bool(trusted)
        cwd = view.cwd
        if not cwd:
            return False
        try:
            return bool(trusted(cwd))
        except Exception:
            log.warning("permissions: the trust check for %s failed", cwd, exc_info=True)
            return False

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
        return fold(state, kind, data)

    def continue_state(self, state: Any) -> Any:
        """A continuing task (a fork, a rewind) keeps the source's mode,
        except bypassPermissions where it is disabled: the source may have
        run where it was allowed."""
        if not (self.disable_bypass and isinstance(state, PermState)):
            return state
        plan = state.plan
        if plan is not None and plan.get("pre_mode") == "bypassPermissions":
            plan = {**plan, "pre_mode": self.mode}
        mode = None if state.mode == "bypassPermissions" else state.mode
        return replace(state, mode=mode, plan=plan)

    def mode_of(self, view: TaskView) -> str:
        """The task's current permission mode."""
        st = state_of(view)
        for mode in (st.mode, view.state.config.get("permission_mode")):
            if isinstance(mode, str) and mode in p.PERMISSION_MODES and not (
                    mode == "bypassPermissions" and self.disable_bypass):
                return mode
        return self.mode

    # ------------------------------------------------------------ tools and commands

    def tools(self, view: TaskView) -> Sequence[Tool]:
        # Sub-agents do not plan for approval (none is offered it).
        if view.state.parent_id:
            return ()
        if view.state.tenant == DEFAULT_TENANT:
            return (self._enter_plan, self._exit_plan)
        plans = self._plans_dir(view)
        return (pl.EnterPlanModeTool(lambda ctx: md.enter_plan(
            ctx, default_mode=self.mode, plans_dir=plans)), self._exit_plan)

    def _plans_dir(self, view: TaskView) -> str:
        """Where this task's plan files go: a tenant's own directory for a
        task of a tenant other than the default one."""
        tenant = view.state.tenant
        return self.plans_dir if tenant == DEFAULT_TENANT \
            else os.path.join(self.plans_dir, tenant)

    async def commands(self, ctx: CommandContext) -> Sequence[Command]:
        return (Command(name="plan", description="Switch to plan mode; with a request, plan it",
                        argument_hint="[request]"),)

    async def run_command(self, ctx: CommandContext, name: str, args: str) -> str:
        return md.PLAN_COMMAND.format(args=args)

    # ------------------------------------------------------------ the gate

    async def before_tool(self, view: TaskView, call: ToolUse) -> Gate | None:
        refused = self._tenant_refused(view)
        if refused is not None:
            return Deny(reason=refused)
        fenced = await fc.refusal(self.fence, view, call)
        if fenced is not None:
            return Deny(reason=fenced)
        updated = _updated_input(view, call)
        if updated is not None:
            # Approved with changed input (a headless client's
            # ``updatedInput``): that input runs, and is gated again.
            return Rewrite(input=updated)
        name = call.name
        if name in self.disallowed:
            return Deny(reason=p.DENIED_BY_POLICY.format(name=name))
        if self.allowed is not None and name not in self.allowed:
            return Deny(reason=p.NOT_IN_ALLOWLIST.format(name=name))
        st = state_of(view)
        guard = self._guards(view, st, call)
        if guard is not None:
            return guard
        decision = await self.decide(view, call)
        if decision.kind == "allow":
            return Allow(outright=True)
        if decision.kind == "deny":
            return Deny(reason=decision.reason)
        rules = list(decision.always)
        if rules and st.asked.get(call.id, {}).get("rules") != rules:
            view.record(NAME, "asked", {"call_id": call.id, "rules": rules})
        return Ask(prompt=decision.reason, firm=decision.firm, detail={
            "by": NAME, "tool": name, "reason": decision.reason,
            "title": w.TITLES.get(name, name),
            "always": rules or None, "always_label": decision.always_label,
            "choices": choices(decision), "classify": decision.classify})

    def _tenant_refused(self, view: TaskView) -> str | None:
        tenant = view.state.tenant
        if tenant == DEFAULT_TENANT:
            return None
        if self.tenant_refusal is not None:
            return f"this worker does not run tasks of tenant {tenant!r}: {self.tenant_refusal}"
        if tenant not in self.fence.workspaces:
            # Nothing here would keep its commands out of a workspace this
            # worker does not know, nor say where its own may write.
            return (f"this worker does not run tasks of tenant {tenant!r}: it does not know "
                    "the tenant's workspace. Every worker needs the server's tenant "
                    "directories (tenants/<name>/config.toml in noeta's home, with "
                    "workspace = ...) and a restart after one is added")
        return None

    def _guards(self, view: TaskView, st: PermState, call: ToolUse) -> Gate | None:
        """noeta's own guards: the budget, the risk ceiling, the repetition
        guard and the host's PreToolUse rules."""
        name = call.name
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
        return None

    async def decide(self, view: TaskView, call: ToolUse) -> Decision:
        """Claude Code's order: deny rules, ask rules, then what the tool
        touches and the mode."""
        return await self._check.decide(view, call, self.mode_of(view))

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
        mine = [x for x in asks if x.detail.get("by") == NAME]
        if not mine or len(mine) != len(asks):
            return None
        if self.can_use_tool is not None:
            return await self._auto(view, call)
        if not all(x.detail.get("classify") for x in mine):
            return None
        return await self._classifier.answer(view, call, mine[0])

    async def _auto(self, view: TaskView, call: ToolUse) -> Gate:
        decision = state_of(view).auto.get(call.id)
        if decision is None:
            assert self.can_use_tool is not None
            verdict = self.can_use_tool(call.name, dict(call.input))
            if inspect.isawaitable(verdict):
                verdict = await verdict
            decision = "allow" if verdict else "deny"
            view.record(NAME, "auto", {"call_id": call.id, "decision": decision,
                                       "resolver": "can_use_tool"})
        return Allow() if decision == "allow" else Deny(reason=DENIED_BY_HUMAN)

    # ------------------------------------------------------------ events

    async def on_start(self, view: TaskView) -> None:
        st = state_of(view)
        if st.mode is None:
            view.record(NAME, "mode", {"mode": self.mode_of(view)})
        offered = pm.modes_record(self._modes)
        if st.offered != offered:
            view.record(NAME, pm.MODES_KIND, offered)

    async def on_event(self, view: TaskView, recorded: Recorded) -> None:
        ev = recorded.event
        if isinstance(ev, ModelReplied) and self.budget.max_cost_usd is not None:
            from noeta.core.models import price

            model = ev.response.model or str(view.state.config.get("model") or "")
            view.record(NAME, "cost", {"usd": price(model, ev.response.usage) if model else 0.0})
        elif isinstance(ev, InputTaken):
            for item in ev.items:
                await self._on_input(view, item.body, recorded.seq)
        elif isinstance(ev, Compacted):
            mode = self.mode_of(view)
            view.record(NAME, "compacted", {"auto": mode == "auto", "plan": mode == "plan",
                                            "seq": recorded.seq})
        elif isinstance(parked := records.decode(ev), records.Parked):
            md.name_plan(view, parked, self._background)

    async def _on_input(self, view: TaskView, body: Any, seq: int) -> None:
        if isinstance(body, Answer):
            await self._remember(view, body)
        elif isinstance(body, External) and body.name == MODE_SIGNAL:
            data = body.data if isinstance(body.data, dict) else {}
            self._switch(view, data.get("mode"), seq)
        elif isinstance(body, External) and body.name == RESUMED_SIGNAL:
            view.record(NAME, "resumed")
        elif isinstance(body, External) and body.name == md.PLAN_NAME_SIGNAL:
            name = (body.data or {}).get("name") if isinstance(body.data, dict) else None
            if isinstance(name, str) and name:
                view.record(NAME, "plan_name", {"name": name})
                view.notify(f"plan: {name}")
        elif isinstance(body, Say) and body.message.role == "user" and \
                body.message.text.startswith(md.PLAN_COMMAND_HEAD):
            self._switch(view, "plan", seq)

    def _switch(self, view: TaskView, mode: Any, seq: int) -> None:
        md.switch(view, mode, seq, current=self.mode_of(view),
                  disable_bypass=self.disable_bypass, plans_dir=self._plans_dir(view))

    def _enter_plan_mode(self, ctx: ToolContext) -> bool:
        return md.enter_plan(ctx, default_mode=self.mode, plans_dir=self.plans_dir)

    async def _remember(self, view: TaskView, answer: Answer) -> None:
        """An "always" allow remembers the rules the card showed; a choice
        to switch modes switches."""
        if answer.decision != "allow":
            return
        st = state_of(view)
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
        if self.remember_to_project and self.trusts(view):
            try:
                await _append_project(view, rules)
            except Exception:
                log.warning("could not save rules to %s", p.LOCAL_SETTINGS, exc_info=True)

    # ------------------------------------------------------------ requests

    async def before_model(self, view: TaskView, request: ModelRequest) -> ModelRequest:
        import msgspec

        refused = self._tenant_refused(view)
        if refused is not None:
            raise TurnFailed(TENANT_NOT_ISOLATED, refused)
        moved = await fc.cwd_refusal(self.fence, view)
        if moved is not None:
            raise TurnFailed(TENANT_CWD_REFUSED, moved)
        if self.budget.max_iterations is not None:
            view.record(NAME, "step")
        if self.allowed is not None or self.disallowed:
            tools = [t for t in request.tools if self._offered(t.name)]
            if len(tools) != len(request.tools):
                request = msgspec.structs.replace(request, tools=tools)
        mode = self.mode_of(view)
        plan = state_of(view).plan or {}
        if mode != "auto" and not (mode == "plan" and plan.get("pre_mode") == "auto"):
            return request
        return a.auto_request(view, request, lambda: a.server_context(
            self._check.scope(view), tenant=view.state.tenant, plans_dir=self.plans_dir,
            allow=self._check.rules(view),
            deny=self.deny_rules, ask=self.ask_rules, auto_mode=self.auto_mode))

    def _offered(self, name: str) -> bool:
        return name not in self.disallowed and (self.allowed is None or name in self.allowed)

    async def after_tool(self, view: TaskView, call: ToolUse, output: ToolOutput) -> ToolOutput:
        if self.budget.max_tool_calls is not None:
            view.record(NAME, "tool_call")
        return output

    async def reminders(self, view: TaskView) -> Sequence[Reminder]:
        return await md.mode_reminders(view, self.mode_of(view))


# ---------------------------------------------------------------- helpers


def _updated_input(view: TaskView, call: ToolUse) -> dict[str, Any] | None:
    """The input a person's approval of this call replaced its own with
    (``Answer.data`` ``{"updated_input": {...}}``), if any."""
    answer = view.state.answers.get(call.id)
    if answer is None or answer.decision != "allow" or not isinstance(answer.data, dict):
        return None
    updated = answer.data.get("updated_input")
    if not isinstance(updated, dict) or updated == call.input:
        return None
    return dict(updated)


async def _append_project(view: TaskView, rules: Sequence[str]) -> None:
    """Add ``rules`` to the project's ``.noeta/settings.local.json``."""
    path = os.path.join(view.cwd or os.getcwd(), p.LOCAL_SETTINGS)
    raw = await _read(view, path)
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


async def _read(view: TaskView, path: str) -> bytes | None:
    try:
        if view.env is not None:
            return await view.env.read_bytes(path)
        return await asyncio.to_thread(Path(path).read_bytes)
    except Exception:  # noqa: BLE001 — absent or unreadable
        return None
