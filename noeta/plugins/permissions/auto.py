"""Auto mode: the classifier that answers the asks the rules leave to it,
and what auto-mode requests tell the API (betas, the server-side check)."""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from noeta.core.events import DEFAULT_TENANT
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


class Classifier:
    """The auto-mode classifier over a task's transcript; :meth:`answer`
    turns its verdict on a call into a gate (once per call, recorded)."""

    def __init__(self, *, model: str | None, auto_mode: Mapping[str, Sequence[str]],
                 deny_rules: Sequence[r.Rule], trusted: bool) -> None:
        self.model = model
        self.auto_mode = auto_mode
        self.deny_rules = deny_rules
        self.trusted = trusted
        self._template: str | None = None

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

        async def stage(n: int) -> str:
            nonlocal model, fast, demoted
            assert view.complete is not None
            request = c.classifier_request(model=model, prompt=prompt, instructions=instructions,
                                           transcript=transcript, stage=n, fast=fast,
                                           user_name=user_name(view.state.tenant),
                                           trusted=self.trusted)
            try:
                response = await view.complete(request)
            except Exception:
                main = view.model or str(view.state.config.get("model") or "")
                if not fast or model == main or self.model:
                    raise
                model, fast, demoted = main, False, True
                response = await view.complete(c.classifier_request(
                    model=model, prompt=prompt, instructions=instructions,
                    transcript=transcript, stage=n, fast=False,
                    user_name=user_name(view.state.tenant), trusted=self.trusted))
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


def user_name(tenant: str) -> str | None:
    """The user the classifier is told of: the tenant's name for a tenant
    other than the default one (never the server's OS user), else ``None``
    (the OS user)."""
    return None if tenant == DEFAULT_TENANT else tenant


def server_context(scope: sc.Scope, *, tenant: str, plans_dir: str, allow: Sequence[r.Rule],
                   deny: Sequence[r.Rule], ask: Sequence[r.Rule],
                   auto_mode: Mapping[str, Sequence[str]]) -> dict[str, Any]:
    """What Claude Code tells the API about its permission state on the
    first auto-mode request (the server answers an API-key session that
    it does not classify, and the classifier runs here). For a tenant
    other than the default one, nothing of the server's own: its home
    directory, OS user and the rules (the operator's among them; a call
    they allow never reaches a classifier)."""
    mine = tenant == DEFAULT_TENANT
    return {
        "v": 1, "permission_mode": "auto", "platform": _platform(), "live_cwd": scope.cwd,
        "home_dir": scope.home if mine else "",
        "rule_roots": {"userSettings": str(Path(plans_dir).parent) if mine else "",
                       "projectSettings": scope.cwd, "localSettings": scope.cwd},
        "trusted_directories": {
            "primary": {"path": scope.cwd, "resolved": [scope.cwd]},
            "additional": list(scope.dirs[1:]), "network": [],
            "block_reads_outside_working_directories": False},
        "rules": {"allow": [x.text for x in allow] if mine else [],
                  "deny": [x.text for x in deny] if mine else [],
                  "ask": [x.text for x in ask] if mine else []},
        "auto_mode": {k: list(v) for k, v in auto_mode.items()},
        "artifact_consent_holdback": False, "case_insensitive_paths":
            sys.platform == "darwin",
        "restricted": False, "is_remote_mode": False, "classify_all_shell": False,
        "user_identity": user_name(tenant) or c._user_name(),
        "git_state": {"cwd": scope.cwd, "root": scope.cwd, "branch": None,
                      "default_branch": None, "status": None, "visibility": None},
    }


def _platform() -> str:
    return {"darwin": "macos", "win32": "windows"}.get(sys.platform, sys.platform)
