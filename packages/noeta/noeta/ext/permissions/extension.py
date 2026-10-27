"""The ``Permissions`` extension: one gate over every tool call."""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import posixpath
import warnings
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from noeta.events import Answer, InputTaken, ModelReplied, Recorded
from noeta.ext.base import Allow, Ask, Deny, Extension, Gate, TaskView
from noeta.ext.permissions import guards as g
from noeta.ext.permissions import policy as p
from noeta.tools.base import ToolOutput
from noeta.types import ModelRequest, ToolUse

log = logging.getLogger("noeta.permissions")

NAME = "permissions"

#: The approval callback: ``(tool_name, input) -> bool`` (or awaitable).
CanUseTool = Callable[[str, dict[str, Any]], "bool | Awaitable[bool]"]

#: Text a call refused by ``can_use_tool`` returns (0.6's deny default).
DENIED_BY_HUMAN = "denied by human"

#: Auto-resolved decisions / open asks kept in the folded state.
_AUTO_KEEP = 64
#: Task config key a child reads its parent's remembered rules from.
INHERITED_RULES_KEY = "permission_rules"
#: The workspace directory whose files steer this extension (the project
#: shell allowlist): writing there always asks, outside bypassPermissions.
CONFIG_DIR = ".noeta"
#: Edit / Write tools whose ``file_path`` is checked against it.
WRITE_TOOLS = frozenset({"Write", "Edit"})
CONFIG_WRITE_REQUIRES_APPROVAL = (
    "tool {name!r} call requires human approval: it changes the workspace's "
    ".noeta/ settings")


class UntrustedProjectShellAllowlistWarning(UserWarning):
    """A workspace's ``.noeta/shell-allowlist.json`` was ignored because the
    workspace is not trusted. ``workspace_dir`` and ``path`` are attributes."""

    def __init__(self, workspace_dir: str, path: str, rule_count: int) -> None:
        self.workspace_dir = workspace_dir
        self.path = path
        self.rule_count = rule_count
        super().__init__(
            f"workspace {workspace_dir} is not trusted; ignoring the "
            f"{rule_count} shell rule(s) in {path} — those commands still need "
            "approval (grant_trust the workspace to honour the file)"
        )


@dataclass(frozen=True)
class PermState:
    """Folded from this extension's records."""

    #: Rules remembered from "always" answers: ``{"kind": "shell", "spec"}``,
    #: ``{"kind": "host", "host"}`` or ``{"kind": "tool", "tool"}``.
    rules: tuple[dict[str, Any], ...] = ()
    steps: int = 0
    tool_calls: int = 0
    cost_usd: float = 0.0
    #: ``call_id -> "allow" | "deny"`` decided by ``can_use_tool``.
    auto: dict[str, str] = field(default_factory=dict)
    #: ``call_id -> rule`` an "always" answer to that open ask remembers.
    asked: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: The project shell allowlist as read at the task's first pick-up
    #: (``None`` before; trusted workspaces only).
    project: tuple[dict[str, Any], ...] | None = None


def inherited_rules(state: Any) -> list[dict[str, Any]]:
    """A task's remembered rules, its own and those it inherited: what a
    child it spawns starts with (``config["permission_rules"]``)."""
    st = state.ext.get(NAME)
    own = list(st.rules) if isinstance(st, PermState) else []
    parent = state.config.get(INHERITED_RULES_KEY)
    rules = [r for r in parent if isinstance(r, dict)] if isinstance(parent, list) else []
    return [*rules, *(r for r in own if r not in rules)]


class Permissions(Extension):
    """Permission modes, the shell and WebFetch gates, remembered "always"
    rules, budget caps, the repetition guard, declarative PreToolUse rules
    and the ``can_use_tool`` auto-resolver — combined so any deny wins, then
    the first ask.

    Constructor arguments (what 0.6 spread over ``Options`` / ``HostConfig``):

    * ``mode`` — ``"default"`` (ask for every tool whose risk is not low),
      ``"acceptEdits"`` (the same, Edit/Write exempt) or
      ``"bypassPermissions"`` (never ask; no shell or web gate). A task can
      override it with ``config["permission_mode"]``.
    * ``risk`` — per-tool risk overrides over :data:`policy.DEFAULT_RISK`;
      ``default_risk`` is the risk of a tool the table does not name (MCP
      tools: ``high``, as in 0.6). ``max_risk`` denies tools above it.
    * ``allowed_tools`` / ``disallowed_tools`` — filter the offered tools by
      name (removed from the request, and denied if called anyway).
      Disallowing ``Bash`` also removes Monitor, BashOutput and KillShell.
    * ``shell_allowlist`` — extra ``{"program", "subcommand"?}`` specs that
      run without asking, on top of the curated rules.
    * ``trusted_workspace`` — honour the project's
      ``.noeta/shell-allowlist.json`` (read once, at the task's first
      pick-up) and the curated rules that run workspace code (test runners);
      "always" answers for shell commands are appended to that file.
      Untrusted (the default): neither. In every mode but
      ``bypassPermissions``, a ``Write`` / ``Edit`` into the workspace's
      ``.noeta/`` and a shell command that names ``.noeta`` ask first, so the
      model cannot widen its own approvals.
    * ``remember_to_project`` — write "always" shell rules to the project
      file when trusted (default on). They are recorded in the task either way.
    * ``webfetch_allowed_hosts`` — ``example.com`` / ``*.example.com``
      entries a WebFetch may reach without asking.
    * ``budget`` (:class:`guards.Budget`) or ``max_turns`` (shorthand for
      ``budget.max_iterations``); ``spawn_tools`` names the tools that spawn
      subtasks (for the spawn and depth caps). Depth is
      ``config["subtask_depth"]`` (set by the Subtasks extension), else 1 for any child.
    * ``repetition_threshold`` (0 = off), ``repetition_action``,
      ``repetition_window``.
    * ``pre_tool_use`` — :class:`guards.PreToolUseRule` s.
    * ``can_use_tool`` — ``(tool, input) -> bool`` (sync or async) that
      answers this extension's approvals instead of a human; its decision is
      recorded with ``resolver="can_use_tool"``. Asks from other extensions
      still reach the host.

    "Always" rules are per task; a child spawned by ``Subtasks`` starts with
    its parent's (``config["permission_rules"]``, see :func:`inherited_rules`).
    """

    name = NAME

    def __init__(
        self,
        *,
        mode: str = "default",
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
        spawn_tools: Iterable[str] = ("Task",),
        repetition_threshold: int = 0,
        repetition_action: g.RepetitionAction = "require_approval",
        repetition_window: int = 8,
        pre_tool_use: Sequence[g.PreToolUseRule] = (),
        can_use_tool: CanUseTool | None = None,
    ) -> None:
        self.mode = p.check_mode(mode)
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
        self.shell_specs = tuple(dict(s) for s in shell_allowlist)
        p.effective_shell_rules(self.shell_specs, trusted=True)  # validates the specs
        self.trusted = trusted_workspace
        self.remember_to_project = remember_to_project
        self.hosts = p.normalize_allowed_hosts(webfetch_allowed_hosts)
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
        self.pre_rules = tuple(g.check_pre_rule(f"pre_tool_use[{i}]", r)
                               for i, r in enumerate(pre_tool_use))
        self.can_use_tool = can_use_tool
        self._warned: OrderedDict[str, None] = OrderedDict()

    # ------------------------------------------------------------ state

    def initial_state(self) -> PermState:
        return PermState()

    def reduce(self, state: Any, kind: str, data: Any) -> Any:
        st: PermState = state if isinstance(state, PermState) else PermState()
        if kind == "rule" and isinstance(data, dict) and data not in st.rules:
            return replace(st, rules=(*st.rules, data))
        if kind == "step":
            return replace(st, steps=st.steps + 1)
        if kind == "tool_call":
            return replace(st, tool_calls=st.tool_calls + 1)
        if kind == "cost" and isinstance(data, dict):
            return replace(st, cost_usd=st.cost_usd + float(data.get("usd") or 0.0))
        if kind == "auto" and isinstance(data, dict):
            auto = dict(st.auto)
            auto[str(data["call_id"])] = str(data["decision"])
            while len(auto) > _AUTO_KEEP:
                auto.pop(next(iter(auto)))
            return replace(st, auto=auto)
        if kind == "asked" and isinstance(data, dict):
            asked = dict(st.asked)
            asked[str(data["call_id"])] = dict(data["always"])
            while len(asked) > _AUTO_KEEP:
                asked.pop(next(iter(asked)))
            return replace(st, asked=asked)
        if kind == "project" and isinstance(data, dict):
            return replace(st, project=tuple(dict(x) for x in data.get("specs") or ()))
        return st

    def _rules(self, view: TaskView, st: PermState) -> list[dict[str, Any]]:
        """Remembered rules: inherited from the parent, then this task's."""
        inherited = view.state.config.get(INHERITED_RULES_KEY)
        base = [r for r in inherited if isinstance(r, dict)] \
            if isinstance(inherited, list) else []
        return [*base, *st.rules]

    def _state(self, view: TaskView) -> PermState:
        st = view.ext_state(NAME)
        return st if isinstance(st, PermState) else PermState()

    def _mode(self, view: TaskView) -> str:
        mode = view.state.config.get("permission_mode")
        return p.check_mode(mode) if isinstance(mode, str) else self.mode

    # ------------------------------------------------------------ hooks

    async def on_start(self, view: TaskView) -> None:
        """Read the project shell allowlist once per task (the first
        pick-up), so a file written later in the task changes nothing."""
        if not self.trusted or self._state(view).project is not None:
            return
        path = os.path.join(self._root(view), p.PROJECT_RULES_FILE)
        specs = p.parse_project_rules(await self._read(view, path))
        view.record(NAME, "project", {"specs": [dict(x) for x in specs]})

    async def before_model(self, view: TaskView, request: ModelRequest) -> ModelRequest:
        if self.budget.max_iterations is not None:
            view.record(NAME, "step")
        if self.allowed is None and not self.disallowed:
            return request
        tools = [t for t in request.tools if self._offered(t.name)]
        if len(tools) == len(request.tools):
            return request
        import msgspec

        return msgspec.structs.replace(request, tools=tools)

    def _offered(self, name: str) -> bool:
        return name not in self.disallowed and (self.allowed is None or name in self.allowed)

    async def after_tool(self, view: TaskView, call: ToolUse, output: ToolOutput) -> ToolOutput:
        if self.budget.max_tool_calls is not None:
            view.record(NAME, "tool_call")
        return output

    async def on_event(self, view: TaskView, recorded: Recorded) -> None:
        ev = recorded.event
        if isinstance(ev, ModelReplied) and self.budget.max_cost_usd is not None:
            from noeta.models import price

            model = ev.response.model or str(view.state.config.get("model") or "")
            view.record(NAME, "cost", {"usd": price(model, ev.response.usage) if model else 0.0})
        elif isinstance(ev, InputTaken):
            for item in ev.items:
                if isinstance(item.body, Answer):
                    await self._remember(view, item.body)

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

        denies: list[str] = []
        asks: list[str] = []
        config_ask = await self._config_ask(view, call)
        if config_ask is not None:
            asks.append(config_ask)
        permission_ask = await self._permission_ask(view, st, call, risk)
        if permission_ask is not None:
            asks.append(permission_ask)
        if self.repetition.threshold > 0:
            history = g.recent_calls(view.state.messages, call.id, self.repetition.window)
            rep = g.repetition_verdict(self.repetition, call, history)
            if rep is not None:
                (denies if rep[0] == "deny" else asks).append(rep[1])
        hook = g.pre_tool_use_verdict(self.pre_rules, call)
        if hook is not None and hook[0] != "allow":
            (denies if hook[0] == "deny" else asks).append(hook[1])
        if denies:
            return Deny(reason=denies[0])
        if not asks:
            return None
        if self.can_use_tool is not None:
            return await self._auto(view, st, call)
        always = p.always_rule(name, call.input) if asks == [permission_ask] else None
        if always is not None and st.asked.get(call.id) != always:
            view.record(NAME, "asked", {"call_id": call.id, "always": always})
        return Ask(prompt=asks[0], detail={"by": NAME, "tool": name, "reason": asks[0],
                                           "always": always})

    # ------------------------------------------------------------ parts

    def _counters(self, view: TaskView, st: PermState, call: ToolUse) -> g.Counters:
        before = 0
        for c in view.state.calls:
            if c.use.id == call.id:
                break
            if c.result is None:
                before += 1
        depth = view.state.config.get("subtask_depth")
        if not isinstance(depth, int):
            depth = 1 if view.state.parent_id else 0
        return g.Counters(iterations=st.steps, tool_calls=st.tool_calls + before,
                          cost_usd=st.cost_usd, spawned=len(view.state.spawned), depth=depth)

    async def _permission_ask(self, view: TaskView, st: PermState, call: ToolUse,
                              risk: str) -> str | None:
        mode = self._mode(view)
        if mode == "bypassPermissions":
            return None
        name = call.name
        rules = self._rules(view, st)
        if name in p.SHELL_GATED_TOOLS:
            remembered = [r["spec"] for r in rules if r.get("kind") == "shell"]
            shell = p.effective_shell_rules(self.shell_specs, await self._project_specs(view),
                                            remembered, trusted=self.trusted)
            if p.shell_call_needs_approval(call.input, shell):
                return p.CALL_REQUIRES_APPROVAL.format(name=name)
            return None
        if p.mode_requires_approval(mode, name, risk):
            if any(r.get("kind") == "tool" and r.get("tool") == name for r in rules):
                return None
            return p.REQUIRES_APPROVAL.format(name=name)
        if name == p.WEBFETCH_TOOL:
            hosts = (*self.hosts, *(r["host"] for r in rules if r.get("kind") == "host"))
            if p.webfetch_needs_approval(call.input, hosts):
                return p.CALL_REQUIRES_APPROVAL.format(name=name)
        return None

    async def _config_ask(self, view: TaskView, call: ToolUse) -> str | None:
        """Outside ``bypassPermissions``: a write into the workspace's
        ``.noeta/``, or a shell command naming it, asks (never remembered)."""
        if self._mode(view) == "bypassPermissions":
            return None
        if call.name in p.SHELL_GATED_TOOLS:
            command = call.input.get("command")
            if isinstance(command, str) and CONFIG_DIR in command:
                return CONFIG_WRITE_REQUIRES_APPROVAL.format(name=call.name)
            return None
        target = call.input.get("file_path")
        if call.name not in WRITE_TOOLS or not isinstance(target, str) or not target:
            return None
        root = self._root(view)
        config_dir = posixpath.join(root, CONFIG_DIR)
        path = target if posixpath.isabs(target) else posixpath.join(root, target)
        candidates = {posixpath.normpath(path)}
        resolved_dir = config_dir
        try:
            if view.env is not None:
                candidates.add(await view.env.realpath(path))
                resolved_dir = await view.env.realpath(config_dir)
            else:
                candidates.add(await asyncio.to_thread(os.path.realpath, path))
                resolved_dir = await asyncio.to_thread(os.path.realpath, config_dir)
        except OSError:
            pass
        for candidate in candidates:
            for d in {posixpath.normpath(config_dir), resolved_dir}:
                if candidate == d or candidate.startswith(d.rstrip("/") + "/"):
                    return CONFIG_WRITE_REQUIRES_APPROVAL.format(name=call.name)
        return None

    async def _auto(self, view: TaskView, st: PermState, call: ToolUse) -> Gate:
        decision = st.auto.get(call.id)
        if decision is None:
            assert self.can_use_tool is not None
            verdict = self.can_use_tool(call.name, dict(call.input))
            if inspect.isawaitable(verdict):
                verdict = await verdict
            decision = "allow" if verdict else "deny"
            view.record(NAME, "auto", {"call_id": call.id, "decision": decision,
                                       "resolver": "can_use_tool"})
        return Allow() if decision == "allow" else Deny(reason=DENIED_BY_HUMAN)

    async def _remember(self, view: TaskView, answer: Answer) -> None:
        """An "always" allow for one of this extension's asks becomes a rule."""
        if not answer.always or answer.decision != "allow":
            return
        # The rule offered with the ask, as this extension recorded it (the
        # task's ``Waiting`` may already be cleared by a message taken in the
        # same batch).
        waiting = view.state.waiting
        detail = waiting.detail.get(answer.request_id) if waiting is not None else None
        if isinstance(detail, dict) and detail.get("by") != NAME:
            return
        rule = self._state(view).asked.get(answer.request_id)
        if not isinstance(rule, dict) or rule in self._state(view).rules:
            return
        view.record(NAME, "rule", rule)
        if rule.get("kind") == "shell" and self.trusted and self.remember_to_project:
            try:
                await self._append_project(view, rule["spec"])
            except Exception:
                log.warning("could not append to %s", p.PROJECT_RULES_FILE, exc_info=True)

    # ------------------------------------------------------------ project file

    def _root(self, view: TaskView) -> str:
        cwd = view.state.config.get("cwd")
        if isinstance(cwd, str) and cwd:
            return cwd
        env_cwd = getattr(view.env, "cwd", None)
        return env_cwd if isinstance(env_cwd, str) and env_cwd else os.getcwd()

    async def _read(self, view: TaskView, path: str) -> bytes | None:
        try:
            if view.env is not None:
                return await view.env.read_bytes(path)
            return await asyncio.to_thread(Path(path).read_bytes)
        except Exception:  # noqa: BLE001 — absent or unreadable: no rules
            return None

    async def _project_specs(self, view: TaskView) -> tuple[dict[str, Any], ...]:
        root = self._root(view)
        if not self.trusted:
            if root not in self._warned:
                self._warned[root] = None
                while len(self._warned) > 256:
                    self._warned.popitem(last=False)
                path = os.path.join(root, p.PROJECT_RULES_FILE)
                specs = p.parse_project_rules(await self._read(view, path))
                if specs:
                    warnings.warn(UntrustedProjectShellAllowlistWarning(root, path, len(specs)),
                                  stacklevel=2)
            return ()
        return self._state(view).project or ()

    async def _append_project(self, view: TaskView, spec: dict[str, Any]) -> None:
        path = os.path.join(self._root(view), p.PROJECT_RULES_FILE)
        existing = p.parse_project_rules(await self._read(view, path))
        text = p.append_project_rule(existing, spec)
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
