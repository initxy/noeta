"""Auto mode: the classifier that answers the asks the rules leave to it,
and what auto-mode requests tell the API (betas, the server-side check)."""

from __future__ import annotations

import logging
import sys
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from noeta.core.events import ExtensionRecorded
from noeta.core.extension import Allow, Ask, Deny, Gate, TaskView
from noeta.core.types import ModelRequest, ToolUse
from noeta.plugins.permissions import classifier as c
from noeta.plugins.permissions import scope as sc
from noeta.plugins.permissions import wording as w
from noeta.plugins.permissions.state import NAME, PermState, state_of
from noeta.toolkit import permission_rules as r

log = logging.getLogger("noeta.permissions")

#: Claude Code's denial limits before auto mode falls back to asking.
MAX_CONSECUTIVE_DENIALS = 3
MAX_TOTAL_DENIALS = 20
#: Betas Claude Code's auto-mode requests carry: every main-loop and
#: sub-agent request names auto mode; the main loop also the server-side
#: classifier one, until its first compaction (as recorded).
AUTO_BETAS = ("afk-mode-2026-01-31",)
MAIN_AUTO_BETAS = ("dangerous-tool-use-2026-09-03", "afk-mode-2026-01-31")
#: The plugin whose ``start`` record holds the instructions a task was shown.
INSTRUCTIONS_PLUGIN = "instructions"
#: Root tasks whose instructions are kept for their sub-agents' classifier.
ROOTS_KEPT = 256


class Classifier:
    """The auto-mode classifier over a task's transcript; :meth:`answer`
    turns its verdict on a call into a gate (once per call, recorded)."""

    def __init__(self, *, model: str | None, auto_mode: Mapping[str, Sequence[str]],
                 deny_rules: Sequence[r.Rule], trusted: Callable[[TaskView], bool]) -> None:
        self.model = model
        self.auto_mode = auto_mode
        self.deny_rules = deny_rules
        self.trusted = trusted
        self._template: str | None = None
        #: Root task id -> the instructions it was shown, for its sub-agents.
        self._roots: OrderedDict[str, str] = OrderedDict()

    async def answer(self, view: TaskView, call: ToolUse, ask: Ask) -> Gate | None:
        st = state_of(view)
        done = st.classified.get(call.id)
        if done is None:
            done = await self._run(view, call, st)
            view.record(NAME, "classified", done)
        if done.get("fallback"):
            return Ask(prompt=str(done.get("fallback")), detail={**ask.detail, "classify": False},
                       firm=True)
        if done["decision"] == "allow":
            return Allow()
        if done.get("counted", True):
            return Deny(reason=w.CLASSIFIER_BLOCKED.format(reason=done.get("reason") or ""))
        return Deny(reason=w.CLASSIFIER_NO_VERDICT.format(reason=done.get("reason") or ""))

    async def _run(self, view: TaskView, call: ToolUse, st: PermState) -> dict[str, Any]:
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
        if self.model:
            return self.model, self.model != main
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
        trusted = self.trusted(view)

        async def stage(n: int) -> str:
            nonlocal model, fast, demoted
            assert view.complete is not None
            request = c.classifier_request(model=model, prompt=prompt, instructions=instructions,
                                           transcript=transcript, stage=n, fast=fast,
                                           trusted=trusted)
            try:
                response = await view.complete(request)
            except Exception:
                main = view.model or str(view.state.config.get("model") or "")
                if not fast or model == main or self.model:
                    raise
                model, fast, demoted = main, False, True
                response = await view.complete(c.classifier_request(
                    model=model, prompt=prompt, instructions=instructions,
                    transcript=transcript, stage=n, fast=False, trusted=trusted))
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
        """The project instructions the classifier is shown: the task's own,
        or, for a sub-agent that runs without them (Explore, Plan), those its
        root task was shown (Claude Code's classifier always has them)."""
        texts = [m.text for m in await view.residents(lead=True) if m.text.strip()]
        if texts or view.state.parent_id is None:
            return "\n\n".join(texts)
        return await self._root_instructions(view)

    async def _root_instructions(self, view: TaskView) -> str:
        root = view.state.root_id
        if not root or view.read_log is None:
            return ""
        text = self._roots.get(root)
        if text is None:
            text = ""
            for rec in await view.read_log(root):
                ev = rec.event
                if (isinstance(ev, ExtensionRecorded) and ev.extension == INSTRUCTIONS_PLUGIN
                        and ev.kind == "start" and ev.message is not None):
                    text = ev.message.text
                    break
            self._roots[root] = text
            while len(self._roots) > ROOTS_KEPT:
                self._roots.popitem(last=False)
        self._roots.move_to_end(root)
        return text

    def _deny_texts(self, view: TaskView) -> list[str]:
        return [x.text for x in self.deny_rules] + list(view.denied_tools)

    def _prompt(self, view: TaskView) -> str:
        if self._template is None:
            self._template = c.prompt_template()
        return view.prompt(f"{NAME}/auto_mode_classifier", self._template)


def auto_request(view: TaskView, request: ModelRequest,
                 context: Callable[[], dict[str, Any]]) -> ModelRequest:
    """``request`` as an auto-mode one: its betas, and on the first main
    request the server-side check with ``context()``."""
    import msgspec

    # As Claude Code per process: the first main request offers the
    # server-side check (the API answers an API-key session that it does
    # not run it), and its beta rides along until the first compaction.
    # A front end resuming the task is the new process; a worker picking
    # it up is not (both facts are recorded: every worker sends the same
    # bytes).
    main = view.state.parent_id is None
    st = state_of(view)
    wanted = MAIN_AUTO_BETAS if main and not st.compacted else AUTO_BETAS
    betas = [*request.betas, *(b for b in wanted if b not in request.betas)]
    fields = dict(request.vendor_fields)
    if main and not st.probed:
        fields["safeguards"] = [{"type": "dangerous_tool_use",
                                 "classifier_context": context()}]
        view.record(NAME, "probed")
    return msgspec.structs.replace(request, betas=betas, vendor_fields=fields)


def server_context(scope: sc.Scope, *, plans_dir: str, allow: Sequence[r.Rule],
                   deny: Sequence[r.Rule], ask: Sequence[r.Rule],
                   auto_mode: Mapping[str, Sequence[str]]) -> dict[str, Any]:
    """What Claude Code tells the API about its permission state on the
    first auto-mode request (the server answers an API-key session that
    it does not classify, and the classifier runs here)."""
    return {
        "v": 1, "permission_mode": "auto", "platform": _platform(), "live_cwd": scope.cwd,
        "home_dir": scope.home,
        "rule_roots": {"userSettings": str(Path(plans_dir).parent),
                       "projectSettings": scope.cwd, "localSettings": scope.cwd},
        "trusted_directories": {
            "primary": {"path": scope.cwd, "resolved": [scope.cwd]},
            "additional": list(scope.dirs[1:]), "network": [],
            "block_reads_outside_working_directories": False},
        "rules": {"allow": [x.text for x in allow], "deny": [x.text for x in deny],
                  "ask": [x.text for x in ask]},
        "auto_mode": {k: list(v) for k, v in auto_mode.items()},
        "artifact_consent_holdback": False, "case_insensitive_paths":
            sys.platform == "darwin",
        "restricted": False, "is_remote_mode": False, "classify_all_shell": False,
        "user_identity": c._user_name(),
        "git_state": {"cwd": scope.cwd, "root": scope.cwd, "branch": None,
                      "default_branch": None, "status": None, "visibility": None},
    }


def _platform() -> str:
    return {"darwin": "macos", "win32": "windows"}.get(sys.platform, sys.platform)
