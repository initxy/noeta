"""The ``Permissions`` extension: Claude Code's permission system over every
tool call — modes, ``allow`` / ``deny`` / ``ask`` rules, the working
directories, protected paths, the auto-mode classifier and what plan mode
may run (plan mode itself is the plan-mode plugin's) — plus noeta's own
guards (host PreToolUse rules, ``can_use_tool``). The budget caps and the
repetition guard are plugins of their own."""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import posixpath
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from noeta.core.events import (
    Answer,
    Compacted,
    External,
    InputTaken,
    Recorded,
)
from noeta.core.execenv import ExecEnv
from noeta.core.extension import (
    Allow,
    Ask,
    Deny,
    Extension,
    Gate,
    Reminder,
    Rewrite,
    TaskView,
)
from noeta.core.mcp_names import allowed_in, named_in
from noeta.core.types import ModelRequest, ToolUse
from noeta.plugins.permissions import auto as a
from noeta.plugins.permissions import guards as g
from noeta.plugins.permissions import modes as md
from noeta.plugins.permissions import policy as p
from noeta.plugins.permissions import wording as w
from noeta.plugins.permissions.decision import Decision, choices
from noeta.plugins.permissions.rule_check import RuleCheck
from noeta.plugins.permissions.rule_specs import rule_spec_text
from noeta.plugins.permissions.state import (
    INHERITED_RULES_KEY,
    NAME,
    PLAN_ORIGIN_KEY,
    PermState,
    auto_active,
    fold,
    inherited_rules,
    state_of,
)
from noeta.toolkit import permission_modes as pm
from noeta.toolkit import permission_rules as r
from noeta.toolkit.context import task_env
from noeta.toolkit.paths import plans_dir as default_plans_dir
from noeta.toolkit.permission_modes import MODE_SIGNAL as MODE_SIGNAL
from noeta.toolkit.signals import RESUMED_SIGNAL
from noeta.toolkit.worktrees import git

__all__ = ["DENIED_BY_HUMAN", "MODE_SIGNAL", "CanUseTool", "PermState", "Permissions",
           "UntrustedProjectShellAllowlistWarning", "inherited_rules"]

log = logging.getLogger("noeta.permissions")

#: The approval callback: ``(tool_name, input) -> bool`` (or awaitable).
CanUseTool = Callable[[str, dict[str, Any]], "bool | Awaitable[bool]"]

#: Text a call refused by ``can_use_tool`` returns (0.6's deny default).
DENIED_BY_HUMAN = "denied by human"



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
      ``environment``, each a list of plain-language lines that replaces
      the built-in text of its section (an entry ``"$defaults"`` puts the
      built-in text at its place).
    * ``disable_bypass_mode`` — refuse ``bypassPermissions`` (Claude Code's
      ``disableBypassPermissionsMode``): as the starting mode, from a task's
      config (an agent file's ``permissionMode``) and as a switch.

    Plan mode (``plan``) is offered only with the plan-mode plugin, which
    records the switches into it. A plan that runs with the auto-mode
    classifier (its record says so: the plan-mode plugin's
    ``auto_during_plan``) sends an ask to the classifier, except a file
    tool's write and plan mode's refusal of a tool that is neither read-only
    nor a shell tool, which the person answers; any other plan leaves every
    ask to the person, and its requests carry no auto-mode betas.

    The rest are noeta's own:

    * ``classifier_model`` — the auto-mode classifier's model (default:
      ``claude-sonnet-5`` when the task runs a Claude model, else the task's
      model; a failing classifier model falls back to the task's).
    * ``plans_dir`` — the plan files' directory, written and read without
      asking (default ``<noeta home>/plans``, as the plan-mode plugin's).
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
    * ``pre_tool_use`` (:class:`guards.PreToolUseRule`).
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
        trusted_workspace: bool | Callable[[str], bool] = False,
        remember_to_project: bool = True,
        webfetch_allowed_hosts: Iterable[str] = (),
        pre_tool_use: Sequence[g.PreToolUseRule] = (),
        can_use_tool: CanUseTool | None = None,
    ) -> None:
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
        for key in ("hard_deny", "soft_deny"):
            if self.auto_mode[key] and "$defaults" not in self.auto_mode[key]:
                # Only a log line: the classifier's text is as Claude Code's.
                log.warning('auto_mode.%s has no "$defaults": it replaces the classifier\'s '
                            "built-in %s rules", key, key)
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
        self.allowed = frozenset(allowed_tools) | p.HARNESS_TOOLS \
            if allowed_tools is not None else None
        self.disallowed = p.expand_disallowed(disallowed_tools)
        self.trusted = trusted_workspace
        self.remember_to_project = remember_to_project
        self.pre_rules = tuple(g.check_pre_rule(f"pre_tool_use[{i}]", rule)
                               for i, rule in enumerate(pre_tool_use))
        self.can_use_tool = can_use_tool
        self._check = RuleCheck(
            allow_rules=self.allow_rules, deny_rules=self.deny_rules, ask_rules=self.ask_rules,
            additional_directories=self.additional_directories, risk=self.risk,
            default_risk=self.default_risk, plans_dir=self.plans_dir)
        self._classifier = a.Classifier(model=classifier_model, auto_mode=self.auto_mode,
                                        deny_rules=self.deny_rules, trusted=self.trusts)

    def working_directories(self, view: TaskView) -> tuple[str, ...]:
        """The task's working directory, then the additional ones (absolute):
        what the environment message lists."""
        return self._check.scope(view).dirs

    async def read_deny_rule(self, view: TaskView, path: str) -> str | None:
        """The deny rule that keeps ``Read`` from ``path``, else ``None``:
        what the instructions plugin asks before it loads a file (Claude
        Code withholds instruction files a ``Read`` deny rule covers)."""
        return await self._check.read_deny_rule(view, path)

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
        current mode (Claude Code's sub-agents run in their parent's, plan
        mode included: the plan-mode plugin passes on the plan)."""
        out: dict[str, Any] = {}
        rules = inherited_rules(state)
        if rules:
            out[INHERITED_RULES_KEY] = rules
        st = state.ext.get(NAME)
        mode = st.mode if isinstance(st, PermState) else None
        if mode is not None:
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
        """The task's current permission mode (plan mode only with the
        plan-mode plugin)."""
        st = state_of(view)
        plans = _plans(view)
        recorded = st.mode
        if recorded == "plan" and plans:
            return "plan"
        if recorded == "plan":
            # A log left planning, read without the plan-mode plugin: the
            # mode the plan began in.
            recorded = (st.plan or {}).get("pre_mode") or "default"
        for mode in (recorded, view.state.config.get("permission_mode"), self.mode):
            if isinstance(mode, str) and mode in p.PERMISSION_MODES and not (
                    mode == "bypassPermissions" and self.disable_bypass) \
                    and (plans or mode != "plan"):
                return mode
        return "default"

    def plan_start_mode(self, view: TaskView) -> str:
        """The mode a task that starts in plan mode plans from (the plan-mode
        plugin asks it by duck type): a sub-agent of a planning task plans
        as its parent does (the parent's plan origin), not as the service's
        default would."""
        origin = view.state.config.get(PLAN_ORIGIN_KEY)
        origin = origin if isinstance(origin, dict) else {}
        pre = origin.get("pre_mode")
        if pre not in p.PERMISSION_MODES or pre == "plan" or (
                pre == "bypassPermissions" and self.disable_bypass):
            pre = self.mode
        return pre if pre != "plan" else "default"

    # ------------------------------------------------------------ the gate

    async def before_tool(self, view: TaskView, call: ToolUse) -> Gate | None:
        updated = _updated_input(view, call)
        if updated is not None:
            # Approved with changed input (a headless client's
            # ``updatedInput``): that input runs, and is gated again.
            return Rewrite(input=updated)
        name = call.name
        if named_in(self.disallowed, name, "disallowed_tools"):
            return Deny(reason=p.DENIED_BY_POLICY.format(name=name))
        if self.allowed is not None and not allowed_in(self.allowed, name, "allowed_tools"):
            return Deny(reason=p.NOT_IN_ALLOWLIST.format(name=name))
        st = state_of(view)
        guard = self._guards(view, call)
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

    def _guards(self, view: TaskView, call: ToolUse) -> Gate | None:
        """noeta's own guards: the risk ceiling and the host's PreToolUse
        rules."""
        name = call.name
        risk = self.risk.get(name, self.default_risk)
        if self.max_risk is not None and (
                p.KNOWN_RISK_LEVELS.index(risk) > p.KNOWN_RISK_LEVELS.index(self.max_risk)):
            return Deny(reason=p.RISK_EXCEEDS.format(name=name, risk=risk, ceiling=self.max_risk))
        hook = g.pre_tool_use_verdict(self.pre_rules, call)
        if hook is not None and hook[0] == "deny":
            return Deny(reason=hook[1])
        if hook is not None and hook[0] == "require_approval":
            return Ask(prompt=hook[1], detail={"by": NAME, "tool": name}, firm=True)
        return None

    async def decide(self, view: TaskView, call: ToolUse) -> Decision:
        """Claude Code's order: deny rules, ask rules, then what the tool
        touches and the mode."""
        mode = self.mode_of(view)
        return await self._check.decide(view, call, mode,
                                        plan_classifier=self.plan_classifier(view, mode))

    def plan_classifier(self, view: TaskView, mode: str) -> bool:
        """Whether this task plans with the auto-mode classifier, as its
        plan record says (settled on entry: :func:`state.plan_classifier_on`)."""
        return mode == "plan" and auto_active(mode, state_of(view).plan)

    # ------------------------------------------------------------ answering asks

    async def resolve_ask(self, view: TaskView, call: ToolUse,
                          asks: Sequence[Ask]) -> Gate | None:
        """Answers this plugin's own asks (``can_use_tool``, else the
        classifier); another plugin's ask on the same call still goes to the
        person once these are allowed."""
        mine = [x for x in asks if x.detail.get("by") == NAME]
        others = [x for x in asks if x.detail.get("by") != NAME]
        if not mine:
            return None
        if self.can_use_tool is not None:
            resolved: Gate | None = await self._auto(view, call)
        elif all(x.detail.get("classify") for x in mine):
            resolved = await self._classifier.answer(view, call, mine[0])
        else:
            return None
        if not others or isinstance(resolved, (Deny, Rewrite)):
            return resolved
        # Allowed (or handed to the person): the others' asks stand.
        return others[0] if len(others) == 1 and isinstance(resolved, Allow) else None

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
        # A task that starts in plan mode is recorded so by the plan-mode
        # plugin (from plan_start_mode).
        if st.mode is None and self.mode_of(view) != "plan":
            view.record(NAME, "mode", {"mode": self.mode_of(view)})
        offered = pm.modes_record(self._modes if _plans(view) else self._modes.without("plan"))
        if st.offered != offered:
            view.record(NAME, pm.MODES_KIND, offered)

    async def on_event(self, view: TaskView, recorded: Recorded) -> None:
        ev = recorded.event
        if isinstance(ev, InputTaken):
            for item in ev.items:
                await self._on_input(view, item.body, recorded.seq)
        elif isinstance(ev, Compacted):
            view.record(NAME, "compacted", {"auto": self.mode_of(view) == "auto",
                                            "seq": recorded.seq})

    async def _on_input(self, view: TaskView, body: Any, seq: int) -> None:
        if isinstance(body, Answer):
            await self._remember(view, body)
        elif isinstance(body, External) and body.name == MODE_SIGNAL:
            data = body.data if isinstance(body.data, dict) else {}
            self._switch(view, data.get("mode"))
        elif isinstance(body, External) and body.name == RESUMED_SIGNAL:
            view.record(NAME, "resumed")

    def _switch(self, view: TaskView, mode: Any) -> None:
        md.switch(view, mode, current=self.mode_of(view), disable_bypass=self.disable_bypass)

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
            self._switch(view, mode)
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

        if self.allowed is not None or self.disallowed:
            tools = [t for t in request.tools if self._offered(t.name)]
            if len(tools) != len(request.tools):
                request = msgspec.structs.replace(request, tools=tools)
        mode = self.mode_of(view)
        if mode != "auto" and not self.plan_classifier(view, mode):
            # Claude Code's auto-mode betas ride on every request while the
            # classifier judges the calls (plan mode with it included).
            return request
        return await a.auto_request(view, request, lambda: a.server_context(
            self._check.scope(view), task_env(view.env, view.state.config),
            plans_dir=self.plans_dir, allow=self._check.rules(view),
            deny=self.deny_rules, ask=self.ask_rules, auto_mode=self.auto_mode))

    def _offered(self, name: str) -> bool:
        return not named_in(self.disallowed, name, "disallowed_tools") and (
            self.allowed is None or allowed_in(self.allowed, name, "allowed_tools"))

    async def reminders(self, view: TaskView) -> Sequence[Reminder]:
        return await md.mode_reminders(view, self.mode_of(view))


# ---------------------------------------------------------------- helpers


def _plans(view: TaskView) -> bool:
    """Whether the task runs the plan-mode plugin: without it there is no
    plan mode to switch to."""
    return any(ext.name == pm.PLAN_MODE_PLUGIN for ext in view.extensions)


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
    try:
        await _keep_out_of_git(task_env(view.env, view.state.config), path)
    except Exception:
        log.warning("could not add %s to the repository's excludes", path, exc_info=True)


async def _keep_out_of_git(env: ExecEnv, path: str) -> None:
    """Add ``path`` to its repository's ``.git/info/exclude`` unless git
    ignores it already, so the person's grants never show in ``git status``
    (nor get committed with ``git add -A``). Claude Code adds the file to the
    user's global excludes; this changes only the repository's own list."""
    cwd = posixpath.dirname(path)
    code, _ = await git(env, ["check-ignore", "-q", path], cwd)
    if code != 1:  # 0: ignored already; otherwise no repository here
        return
    code, common = await git(env, ["rev-parse", "--path-format=absolute", "--git-common-dir"],
                             cwd)
    top_code, top = await git(env, ["rev-parse", "--show-toplevel"], cwd)
    if code != 0 or top_code != 0 or not common or not top:
        return
    exclude = posixpath.join(common, "info", "exclude")
    line = "/" + posixpath.relpath(path, top)
    try:
        raw = await env.read_bytes(exclude)
    except OSError:
        raw = b""
    if line in raw.decode("utf-8", "replace").splitlines():
        return
    sep = b"\n" if raw and not raw.endswith(b"\n") else b""
    await env.write_bytes(exclude, raw + sep + line.encode("utf-8") + b"\n")


async def _read(view: TaskView, path: str) -> bytes | None:
    try:
        if view.env is not None:
            return await view.env.read_bytes(path)
        return await asyncio.to_thread(Path(path).read_bytes)
    except Exception:  # noqa: BLE001 — absent or unreadable
        return None
