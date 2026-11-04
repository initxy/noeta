"""Auto mode: the classifier that answers the asks the rules leave to it,
and what auto-mode requests tell the API (betas, the server-side check)."""

from __future__ import annotations

import logging
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from noeta.core.errors import ContextOverflow
from noeta.core.events import ExtensionRecorded
from noeta.core.execenv import ExecEnv
from noeta.core.extension import Allow, Ask, Deny, Gate, Rewrite, TaskView
from noeta.core.types import ModelRequest, ToolUse
from noeta.plugins.permissions import classifier as c
from noeta.plugins.permissions import scope as sc
from noeta.plugins.permissions import wording as w
from noeta.plugins.permissions.state import NAME, PermState, state_of
from noeta.toolkit import permission_rules as r
from noeta.toolkit.interactive import can_ask
from noeta.toolkit.local_env import env_home, env_platform, is_local
from noeta.toolkit.notices import HANDBACK_REVIEW_KEY
from noeta.toolkit.plugin_records import INSTRUCTIONS, instructions_managed_only
from noeta.toolkit.worktrees import git, repository_top

log = logging.getLogger("noeta.permissions")

#: Claude Code's denial limits before auto mode falls back to asking (a
#: task nobody can answer is refused instead: the ask would park it for good).
MAX_CONSECUTIVE_DENIALS = 3
MAX_TOTAL_DENIALS = 20
#: Betas Claude Code's auto-mode requests carry: every main-loop and
#: sub-agent request names auto mode; the main loop also the server-side
#: classifier one, until its first compaction (as recorded).
AUTO_BETAS = ("afk-mode-2026-01-31",)
MAIN_AUTO_BETAS = ("dangerous-tool-use-2026-09-03", "afk-mode-2026-01-31")
#: Root tasks whose instructions are kept for their sub-agents' classifier.
ROOTS_KEPT = 256
#: The sub-agent's report tool: a block flags the report instead of
#: refusing it (Claude Code's ``onBlock: "flag"``).
HANDBACK_TOOL = "SubagentHandback"


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
        #: Root task id -> the instruction texts it was shown, for its
        #: sub-agents, and the last seq of its log read.
        self._roots: OrderedDict[str, tuple[list[str], int]] = OrderedDict()
        #: Verdicts recorded but maybe not folded yet: a hand-back rewritten
        #: with its warning is gated again before the record lands.
        self._fresh: OrderedDict[str, dict[str, Any]] = OrderedDict()

    async def answer(self, view: TaskView, call: ToolUse, ask: Ask) -> Gate | None:
        st = state_of(view)
        done = st.classified.get(call.id) or self._fresh.get(call.id)
        if done is None:
            done = await self._run(view, call, st)
            view.record(NAME, "classified", done)
            self._fresh[call.id] = done
            while len(self._fresh) > ROOTS_KEPT:
                self._fresh.popitem(last=False)
        if done.get("fallback"):
            if not can_ask(view.state.config):
                return Deny(reason=str(done.get("fallback")))
            return Ask(prompt=str(done.get("fallback")), detail={**ask.detail, "classify": False},
                       firm=True)
        if done.get("too_long"):
            # As Claude Code: the call is asked about as if auto mode were
            # off (no retry would ever fit).
            return Ask(prompt=ask.prompt, detail={**ask.detail, "classify": False}, firm=True)
        if call.name == HANDBACK_TOOL and done["decision"] in ("allow", "flag"):
            return _handback_gate(view, call, done)
        if done["decision"] == "allow":
            return Allow()
        if done.get("counted", True):
            return Deny(reason=w.CLASSIFIER_BLOCKED.format(reason=done.get("reason") or ""))
        return Deny(reason=w.CLASSIFIER_NO_VERDICT.format(reason=done.get("reason") or ""))

    async def _run(self, view: TaskView, call: ToolUse, st: PermState) -> dict[str, Any]:
        out: dict[str, Any] = {"call_id": call.id}
        handback = call.name == HANDBACK_TOOL
        try:
            verdict, demoted = await self._verdict(view, call, st)
        except ContextOverflow:
            log.warning("auto-mode classifier transcript too long for %s", call.name)
            if handback:
                return {**out, "decision": "deny", "counted": False,
                        "reason": w.CLASSIFIER_TOO_LONG.format(name=call.name)}
            return {**out, "decision": "ask", "too_long": True}
        except Exception as exc:  # noqa: BLE001 — no verdict holds the call back
            log.warning("auto-mode classifier failed for %s: %s", call.name, exc)
            if handback:
                return {**out, "decision": "flag", "unreviewed": True}
            return {**out, "decision": "deny", "counted": False,
                    "reason": w.CLASSIFIER_UNAVAILABLE}
        if demoted:
            view.record(NAME, "demoted")
        if verdict.block is None:
            if handback:
                return {**out, "decision": "flag", "unreviewed": True}
            return {**out, "decision": "deny", "counted": False,
                    "reason": w.CLASSIFIER_UNPARSED}
        if not verdict.block:
            return {**out, "decision": "allow"}
        if handback:
            # Delivered under a warning, not refused, and no denial counted.
            return {**out, "decision": "flag", "reason": verdict.reason,
                    "category": verdict.category}
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
        root = view.state.root_id or view.task_id or None

        async def stage(n: int) -> str:
            nonlocal model, fast, demoted
            assert view.complete is not None
            request = c.classifier_request(model=model, prompt=prompt, instructions=instructions,
                                           transcript=transcript, stage=n, fast=fast,
                                           trusted=trusted, root_id=root)
            try:
                response = await view.complete(request)
            except ContextOverflow:
                raise
            except Exception:
                main = view.model or str(view.state.config.get("model") or "")
                if not fast or model == main or self.model:
                    raise
                model, fast, demoted = main, False, True
                response = await view.complete(c.classifier_request(
                    model=model, prompt=prompt, instructions=instructions,
                    transcript=transcript, stage=n, fast=False, trusted=trusted, root_id=root))
            return response.message.text

        first = c.parse_stage1(await stage(1))
        if first.block is False:
            return first, demoted
        try:
            return c.parse_stage2(await stage(2)), demoted
        except ContextOverflow:
            raise
        except Exception as exc:  # noqa: BLE001 — stage 1's lean to block stands
            log.warning("auto-mode classifier stage 2 failed for %s: %s", call.name, exc)
            return c.Verdict(block=True, reason=w.CLASSIFIER_STAGE2_FAILED), demoted

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
        or, for a sub-agent that runs without them or with the managed ones
        only (Explore, Plan), those its root task was shown (Claude Code's
        classifier always has the session's)."""
        texts = [m.text for m in await view.residents(lead=True) if m.text.strip()]
        managed_only = instructions_managed_only(view)
        if view.state.parent_id is None or (texts and not managed_only):
            return "\n\n".join(texts)
        return await self._root_instructions(view)

    async def _root_instructions(self, view: TaskView) -> str:
        """The instructions the root task was shown: its ``start`` record,
        then each re-read of a changed file. The root's log is read on from
        the last record seen, so a ``NOETA.md`` changed mid-session reaches
        the sub-agent's classifier as it reached the root."""
        root = view.state.root_id
        if not root or view.read_log is None:
            return ""
        texts, seen = self._roots.get(root, ([], 0))
        records = await view.read_log(root, seen)
        texts = list(texts)
        for rec in records:
            seen = max(seen, rec.seq)
            ev = rec.event
            if (isinstance(ev, ExtensionRecorded) and ev.extension == INSTRUCTIONS
                    and ev.kind in ("start", "reread") and ev.message is not None):
                texts.append(ev.message.text)
        self._roots[root] = (texts, seen)
        self._roots.move_to_end(root)
        while len(self._roots) > ROOTS_KEPT:
            self._roots.popitem(last=False)
        return "\n\n".join(texts)

    def _deny_texts(self, view: TaskView) -> list[str]:
        return [x.text for x in self.deny_rules] + list(view.denied_tools)

    def _prompt(self, view: TaskView) -> str:
        if self._template is None:
            self._template = c.prompt_template()
        return view.prompt(f"{NAME}/auto_mode_classifier", self._template)


def _handback_gate(view: TaskView, call: ToolUse, done: Mapping[str, Any]) -> Gate:
    """A reviewed hand-back runs; one auto mode flagged (or could not
    review) runs with the warning its caller reads above the report, under
    :data:`HANDBACK_REVIEW_KEY`, which only this layer sets.

    The tool drops a value equal to the one the model wrote under the key
    (its own), so the warning set here never equals that: a model that
    wrote the warning's exact text gets it with a trailing newline (the
    tool strips it). The rewritten call is gated again and kept as is."""
    wanted = {k: v for k, v in call.input.items() if k != HANDBACK_REVIEW_KEY}
    if done.get("decision") == "flag":
        note = w.HANDBACK_UNREVIEWED if done.get("unreviewed") else \
            w.HANDBACK_FLAGGED.format(reason=done.get("reason") or "")
        written = next((c.use.input.get(HANDBACK_REVIEW_KEY) for c in view.state.calls
                        if c.use.id == call.id), None)
        wanted[HANDBACK_REVIEW_KEY] = note + "\n" if written == note else note
    return Allow() if wanted == call.input else Rewrite(input=wanted)


async def auto_request(view: TaskView, request: ModelRequest,
                       context: Callable[[], Awaitable[dict[str, Any]]]) -> ModelRequest:
    """``request`` as an auto-mode one: its betas, and on the first main
    request the server-side check with ``await context()``."""
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
                                 "classifier_context": await context()}]
        view.record(NAME, "probed")
    return msgspec.structs.replace(request, betas=betas, vendor_fields=fields)


#: The settings sources whose root directory the check is told: the user
#: settings' directory, the working directory for every other (Claude
#: Code's list, as recorded).
RULE_ROOT_SOURCES = ("userSettings", "projectSettings", "localSettings", "flagSettings",
                     "policySettings", "cliArg", "command", "session", "toolsNarrowing",
                     "mcpServerPolicy", "hostCredential")


async def server_context(scope: sc.Scope, env: ExecEnv, *, plans_dir: str,
                         allow: Sequence[r.Rule], deny: Sequence[r.Rule],
                         ask: Sequence[r.Rule],
                         auto_mode: Mapping[str, Sequence[str]]) -> dict[str, Any]:
    """What Claude Code tells the API about its permission state on the
    first auto-mode request (the server answers an API-key session that
    it does not classify, and the classifier runs here). The platform and
    the git state are the task's exec env's, not this worker's."""
    platform = _platform(await env_platform(env))
    user_root = str(Path(plans_dir).parent)
    return {
        "v": 1, "permission_mode": "auto", "platform": platform, "live_cwd": scope.cwd,
        "home_dir": await _home(env, scope.home),
        "rule_roots": {k: user_root if k == "userSettings" else scope.cwd
                       for k in RULE_ROOT_SOURCES},
        "trusted_directories": {
            "primary": {"path": scope.cwd, "resolved": [scope.cwd]},
            "additional": list(scope.dirs[1:]), "network": [],
            "block_reads_outside_working_directories": False},
        "rules": {"allow": [x.text for x in allow], "deny": [x.text for x in deny],
                  "ask": [x.text for x in ask]},
        "auto_mode": {k: list(v) for k, v in auto_mode.items()},
        # Claude Code: true on Windows only.
        "artifact_consent_holdback": False, "case_insensitive_paths": platform == "windows",
        "restricted": False, "is_remote_mode": False, "classify_all_shell": False,
        "user_identity": c._user_name(),
        "git_state": await git_state(env, scope.cwd),
    }


async def git_state(env: ExecEnv, cwd: str) -> dict[str, Any]:
    """Claude Code's git state for an API-key session: the checkout's root
    and branch (its status and visibility probes are off by default, so
    sent as ``null``); outside a repository, no root and the
    ``not_a_repo`` error."""
    out: dict[str, Any] = {"cwd": cwd, "root": None, "branch": None, "default_branch": None,
                           "status": None, "visibility": None}
    try:
        root = await repository_top(env, cwd)
        if root is None:
            return {**out, "error": "not_a_repo"}
        code, branch = await git(env, ["rev-parse", "--abbrev-ref", "HEAD"], cwd)
    except Exception:  # noqa: BLE001 — best-effort, as Claude Code's probe
        return {**out, "error": "error"}
    return {**out, "root": root, "branch": branch if code == 0 and branch else None}


async def _home(env: ExecEnv, here: str) -> str:
    """The exec env's home directory (``here``, this worker's, for a local
    env or when it cannot be told)."""
    if is_local(env):
        return here
    return await env_home(env, here) or here


def _platform(name: str) -> str:
    return {"darwin": "macos", "win32": "windows"}.get(name, name)
