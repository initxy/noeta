"""The ``environment`` plugin: when each part is captured, recorded and sent."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import msgspec

from noeta.core.events import Recorded, Waiting
from noeta.core.extension import Extension, Reminder, TaskView
from noeta.core.loop.state import TaskState
from noeta.core.types import Message, ModelRequest, system_message, user
from noeta.plugins.environment.facts import (
    capture_environment,
    capture_facts,
    model_line,
    render_attribution,
    render_environment_update,
)
from noeta.plugins.environment.git import SNAPSHOT_AGAIN, _find_root, capture_git, git_head
from noeta.plugins.environment.notes import (
    _PROMPT_BUDGET_LINE,
    DATE_CHANGED_REMINDER,
    DATE_CHANGED_UTC_REMINDER,
    LANGUAGE_CLEARED,
    LANGUAGE_NOTE,
    MODEL_CHANGED,
    TOKEN_BUDGET,
    _owes_budget_note,
    budget_note,
    date_line,
    deferred_delta,
    deferred_list,
    gets_budget,
    today_in,
    valid_zone,
    with_model_paragraphs,
)
from noeta.toolkit.context import task_env
from noeta.toolkit.ultrathink import ultrathink_effort, ultrathink_reminders
from noeta.toolkit.usd_budget import BUDGET_KEY, usd_budget_reminders

#: A compaction waits this long for a new git snapshot, then gives the
#: stored one again (each git call has 5 seconds; a slow container could
#: otherwise add half a minute to the compaction).
GIT_RECAPTURE_TIMEOUT_S = 10.0


#: Places of this plugin's notes in the joined system turn (``Reminder.order``):
#: the environment first, the budget and the date last.
ORDER_ENVIRONMENT = 0
#: After a compaction the environment comes after the lists (Claude Code's).
ORDER_ENVIRONMENT_AFTER_COMPACTION = 60
ORDER_DEFERRED = 30
#: A sub-agent's environment turn opens with its deferred tools.
ORDER_DEFERRED_SUBAGENT = -10
ORDER_LANGUAGE = 75
ORDER_BUDGET = 80
ORDER_DATE = 90


class Environment(Extension):
    """Constructor arguments:

    * ``model`` — the agent's model, named when the task's config sets none.
    * ``model_name`` — include the model line.
    * ``git_status`` — put the git snapshot in the first user turn.
    * ``git_refresh`` — give the snapshot again at the start of a later turn
      when the branch or HEAD moved since the previous turn ended (noeta's
      stand-in for Claude Code's re-announce on resume).
    * ``attribution`` — put the commit / PR attribution note there.
    * ``attribution_commit`` / ``attribution_pr`` — the settings'
      ``attribution.commit`` / ``attribution.pr``: the line to use instead
      of the default (``""``: none; both ``""``: the model is told to add
      none).
    * ``language`` — the settings' ``language``: the model is told to answer
      in it (and told again when it changes or goes).
    * ``token_budget`` — the ``<total_tokens>`` budget a turn starts with
      (``0``: no budget notes, and the prompt's budget line taken out).
    * ``date_change`` — tell the model, before any model call, when the date
      is no longer the one it was given (the prompt
      ``reminder/date_changed``, or ``reminder/date_changed_utc`` for UTC
      dates; ``{date}`` is today).
    * ``today`` — today's date as ``YYYY-MM-DD``, taken as the user's
      (default: the date in the task config's ``timezone``, the user's IANA
      zone a front end passes; without one, the UTC date, said to be UTC's,
      so a server never gives its own zone's date as the user's).
    * ``critical_reminder`` — a text sent with every model call (an agent
      definition's ``criticalSystemReminder_EXPERIMENTAL``, Claude Code's).
    """

    name = "environment"

    def __init__(self, *, model: str | None = None, model_name: bool = True,
                 git_status: bool = True, git_refresh: bool = True,
                 attribution: bool = True, attribution_commit: str | None = None,
                 attribution_pr: str | None = None, language: str | None = None,
                 token_budget: int = TOKEN_BUDGET, date_change: bool = True,
                 today: Callable[[], str] | None = None,
                 critical_reminder: str = "") -> None:
        self.model = model
        self.attribution_commit = attribution_commit
        self.attribution_pr = attribution_pr
        self.language = language or None
        self.model_name = model_name
        self.git_status = git_status
        self.git_refresh = git_refresh
        self.attribution = attribution
        self.token_budget = token_budget
        self.date_change = date_change
        self.today = today
        self.critical_reminder = critical_reminder

    def reduce(self, state: Any, kind: str, data: Any) -> Any:
        if not isinstance(data, dict):
            return state
        if kind == "environment" and "text" in data:
            # A record without ``held`` (an older log) went out as a message.
            return {"text": data["text"], "date": data.get("date"),
                    "utc": bool(data.get("utc")), "held": bool(data.get("held")),
                    "turn_base": 0, "model": data.get("model"), "facts": data.get("facts")}
        if not isinstance(state, dict):
            return state
        if kind == "model":
            return {**state, "model": data.get("model"), "text": data.get("text", state["text"])}
        if kind == "env_update":
            return {**state, "facts": data.get("facts"), "text": data.get("text", state["text"])}
        if kind == "language":
            return {**state, "language": data.get("language")}
        if kind == "git":
            text = data.get("text")
            return {**state, "git": text, "git_head": data.get("head"),
                    "git_turn_end": data.get("head")}
        if kind == "git_turn_end":
            return {**state, "git_turn_end": data.get("head")}
        if kind == "attribution":
            return {**state, kind: data.get("text")}
        if kind == "sent":
            return {**state, "held": False, "skip_date": False}
        if kind == "rehold":
            # A compaction: the environment, the deferred tools, the
            # language note and (unless it already follows the summary) the
            # date go out again.
            return {**state, "held": True, "compacted": True, "language": None,
                    "skip_date": not data.get("date", True)}
        if kind in ("deferred", "deferred_delta"):
            names = list(data.get("names") or [])
            ever = [*(state.get("deferred_ever") or state.get("deferred") or []), *names]
            return {**state, "deferred": names, "deferred_ever": list(dict.fromkeys(ever))}
        if kind == "date":
            return {**state, "date": data.get("date")}
        if kind == "budget":
            return {**state, "turn_base": data.get("base", 0)}
        return state

    def _model(self, view: TaskView) -> str:
        return str(view.state.config.get("model") or self.model or view.model or "")

    def _today(self, view: TaskView) -> tuple[str, bool]:
        """Today's date for this task, and whether it is UTC's (the user's
        zone is not known)."""
        if self.today is not None:
            return self.today(), False
        zone = valid_zone(view.state.config.get("timezone"))
        return today_in(zone), zone is None

    def inherit(self, state: TaskState) -> Mapping[str, Any]:
        zone = valid_zone(state.config.get("timezone"))
        out: dict[str, Any] = {"timezone": zone} if zone else {}
        if state.config.get(BUDGET_KEY) is not None:
            out[BUDGET_KEY] = state.config[BUDGET_KEY]  # the conversation's, told to all
        return out

    @staticmethod
    def _dirs(view: TaskView) -> list[str]:
        """The additional working directories: those of the plugin that
        keeps them (permissions, ``working_directories``) after the first."""
        for ext in view.extensions:
            dirs = getattr(ext, "working_directories", None)
            if callable(dirs):
                return list(dirs(view))[1:]
        return []

    async def on_start(self, view: TaskView) -> None:
        if view.ext_state(self.name):
            return
        env = task_env(view.env, view.state.config)
        cwd = view.cwd or env.cwd
        model = self._model(view)
        facts = await capture_facts(env, cwd, self._dirs(view))
        text = await capture_environment(env, cwd, model if self.model_name else "", facts)
        today, utc = self._today(view)
        # Held for the first request's system turn, after the prompt.
        data: dict[str, Any] = {"text": text, "date": today, "held": True, "facts": facts}
        if self.model_name and model:
            data["model"] = model
        if utc:
            data["utc"] = True
        view.record(self.name, "environment", data)
        if self.git_status:
            snapshot = await capture_git(env, cwd)
            if snapshot:
                view.record(self.name, "git", {"text": snapshot,
                                               "head": await git_head(env, cwd)},
                            message=user(snapshot, origin="system"))
        note = render_attribution(model, commit=self.attribution_commit,
                                  pr=self.attribution_pr) if self.attribution and model else None
        if note:
            view.record(self.name, "attribution", {"text": note},
                        message=user(note, origin="system"))

    async def reminders(self, view: TaskView) -> Sequence[Reminder]:
        st = view.ext_state(self.name)
        if not isinstance(st, dict):
            return ()
        out: list[Reminder] = []
        model = self._model(view)
        told = str(st.get("model") or "")
        switched = bool(self.model_name and told and model and model != told)
        if st.get("held"):
            # The first request (or the first after a compaction): the
            # environment follows the prompt, naming the model in use now.
            text = st["text"]
            if switched:
                text = text.replace(model_line(told), model_line(model))
                view.record(self.name, "model", {"model": model, "text": text})
            out.append(Reminder(text=text, kind="sent", data={}, role="system",
                                order=ORDER_ENVIRONMENT_AFTER_COMPACTION
                                if st.get("compacted") else ORDER_ENVIRONMENT))
            # The deferred tools the first request offers, named in full;
            # later changes are told as they happen (below).
            names = view.deferred_tool_names()
            if names:
                order = ORDER_DEFERRED_SUBAGENT if view.state.parent_id else ORDER_DEFERRED
                out.append(Reminder(text=deferred_list(names), kind="deferred",
                                    data={"names": names}, role="system", order=order))
            elif st.get("deferred"):
                view.record(self.name, "deferred", {"names": []})
            if not st.get("skip_date"):
                out.append(Reminder(text=date_line(st["date"], utc=bool(st.get("utc"))),
                                    kind="date", data={"date": st["date"]}, role="system",
                                    order=ORDER_DATE))
        else:
            if switched:
                # Claude Code tells a switched model before its next call.
                line = MODEL_CHANGED.replace("{line}", model_line(model))
                out.append(Reminder(text=line, kind="model", data={
                    "model": model, "text": st["text"].replace(model_line(told),
                                                               model_line(model))},
                    role="system", order=ORDER_ENVIRONMENT))
            out += await self._environment_update(view, st)
            out += self._deferred_delta(view, st)
            out += await self._git_again(view, st)
        if not view.state.parent_id and self.language != st.get("language") \
                and (self.language or st.get("language")):
            text = LANGUAGE_NOTE.replace("{language}", self.language) if self.language \
                else LANGUAGE_CLEARED
            out.append(Reminder(text=text, kind="language", data={"language": self.language},
                                role="system", order=ORDER_LANGUAGE))
        # Checked before every model call, as Claude Code does, so a long
        # turn that runs past midnight is told too. Each call may run on
        # another worker, whose clock can be a little behind: a date before
        # the one last given is never told.
        if not st.get("held") and self.date_change and st.get("date"):
            today, _ = self._today(view)
            if today > str(st["date"]):
                text = view.prompt("reminder/date_changed_utc", DATE_CHANGED_UTC_REMINDER) \
                    if st.get("utc") else view.prompt("reminder/date_changed",
                                                      DATE_CHANGED_REMINDER)
                out.append(Reminder(text=text.replace("{date}", today), kind="date",
                                    data={"date": today}, role="system", order=ORDER_DATE))
        # A sub-agent's task prompt gets none; its tool results do.
        if self.token_budget and gets_budget(self._model(view)) and _owes_budget_note(
                view.state.messages, prompts=not view.state.parent_id):
            spent = view.state.usage.output
            base = spent if not view.state.steps_in_turn else st.get("turn_base", 0)
            left = max(self.token_budget - (spent - base), 0)
            out.append(Reminder(text=budget_note(left), kind="budget", data={"base": base},
                                role="system", order=ORDER_BUDGET))
        out += await usd_budget_reminders(view, ORDER_BUDGET + 1)
        out += ultrathink_reminders(view)
        if self.critical_reminder:
            out.append(Reminder(text=self.critical_reminder, kind="critical_reminder", data={}))
        return out

    async def before_model(self, view: TaskView, request: ModelRequest) -> ModelRequest:
        """The main prompt's paragraphs for this model
        (:func:`with_model_paragraphs`); a model that is told no budget is
        not shown the prompt's budget line either. A turn the person asked to
        ``ultrathink`` runs at ``xhigh`` effort (``noeta.toolkit.ultrathink``)."""
        request = ultrathink_effort(view, request)
        budget = bool(self.token_budget and gets_budget(request.model))
        system = [msgspec.structs.replace(p, text=t) for p in request.system
                  if (t := self._prompt_text(p.text, request.model, budget)) is not None]
        if all(a.text == b.text for a, b in zip(system, request.system, strict=True)):
            return request
        return msgspec.structs.replace(request, system=system)

    @staticmethod
    def _prompt_text(text: str, model: str, budget: bool) -> str:
        text = with_model_paragraphs(text, model)
        return text if budget else _PROMPT_BUDGET_LINE.sub("", text)

    async def _environment_update(self, view: TaskView,
                                  st: dict[str, Any]) -> list[Reminder]:
        """At the start of a later turn, what changed in the facts the
        environment gave (repository, worktree, additional directories):
        Claude Code's environment update. The environment text is taken
        again with them, for a compaction to send."""
        before = st.get("facts")
        if not isinstance(before, dict) or view.state.steps_in_turn or view.state.parent_id:
            return []
        env = task_env(view.env, view.state.config)
        cwd = view.cwd or env.cwd
        try:
            now = await capture_facts(env, cwd, self._dirs(view))
        except Exception:  # noqa: BLE001 — capture is best-effort
            return []
        text = render_environment_update(before, now)
        if not text:
            return []
        full = await capture_environment(env, cwd, self._model(view) if st.get("model") else "",
                                         now)
        return [Reminder(text=text, kind="env_update", data={"facts": now, "text": full},
                         role="system", order=ORDER_ENVIRONMENT)]

    async def _git_again(self, view: TaskView, st: dict[str, Any]) -> list[Reminder]:
        """At the start of a later turn, the snapshot taken again when the
        branch or its commit moved since the previous turn ended (someone
        switched branch or committed in between; edits do not count, nor do
        the model's own commits, which happened inside a turn)."""
        if not self._refreshes(view, st):
            return []
        env = task_env(view.env, view.state.config)
        cwd = view.cwd or env.cwd
        head = await git_head(env, cwd)
        if not head or head == (st.get("git_turn_end") or st["git_head"]):
            return []
        again = await capture_git(env, cwd, when=SNAPSHOT_AGAIN)
        return [Reminder(text=again, kind="git", data={"text": again, "head": head})]

    def _refreshes(self, view: TaskView, st: dict[str, Any]) -> bool:
        return bool(self.git_status and self.git_refresh and not view.state.steps_in_turn
                    and st.get("git_head") and not view.state.parent_id)

    async def on_event(self, view: TaskView, recorded: Recorded) -> None:
        """When a turn ends, where the branch and HEAD are (a git call)."""
        st = view.ext_state(self.name)
        if not (isinstance(recorded.event, Waiting) and recorded.event.on == "input"
                and isinstance(st, dict) and self.git_status and self.git_refresh
                and st.get("git_head") and not view.state.parent_id):
            return
        env = task_env(view.env, view.state.config)
        head = await git_head(env, view.cwd or env.cwd)
        if head and head != st.get("git_turn_end"):
            view.record(self.name, "git_turn_end", {"head": head})

    @staticmethod
    def _deferred_delta(view: TaskView, st: dict[str, Any]) -> list[Reminder]:
        """A note when the deferred tools offered changed since they were
        last named (an MCP server connected late, restarted with other
        tools, or dropped): Claude Code's deferred-tools delta."""
        names = view.deferred_tool_names()
        before = st.get("deferred") or []
        text = deferred_delta(before, st.get("deferred_ever") or before, names)
        if not text:
            return []
        return [Reminder(text=text, kind="deferred_delta", data={"names": names},
                         role="system", order=ORDER_DEFERRED)]

    async def lead_resident(self, view: TaskView) -> Sequence[Message]:
        """Ahead of a compaction's summary, as Claude Code rebuilds it: a
        git snapshot taken now (the summary has replaced the earlier one, so
        it shows what the conversation changed; none once the directory is
        no longer a repository), then the attribution note, or, when
        messages stay after the summary, the date (a system turn, which
        follows the summary) and the note after those messages
        (``resident``). The environment and the deferred tools go out again
        with the next request."""
        st = view.ext_state(self.name)
        if view.keeps_tail is None or not isinstance(st, dict):
            return ()
        out: list[Message] = []
        if isinstance(st.get("git"), str):
            snapshot = await self._git_for_compaction(view, st["git"])
            if snapshot:
                out.append(user(snapshot, origin="system"))
        if not view.keeps_tail and isinstance(st.get("attribution"), str):
            out.append(user(st["attribution"], origin="system"))
        date = view.keeps_tail and bool(st.get("date"))
        if date:
            out.append(system_message(date_line(st["date"], utc=bool(st.get("utc")))))
        if isinstance(st.get("text"), str):
            view.record(self.name, "rehold", {"date": not date})
        return out

    async def _git_for_compaction(self, view: TaskView, stored: str) -> str:
        """The snapshot a compaction gives again: taken now (``""`` once the
        directory is no longer a repository), or the stored one when taking
        it fails or runs past :data:`GIT_RECAPTURE_TIMEOUT_S` seconds."""
        env = task_env(view.env, view.state.config)
        cwd = view.cwd or env.cwd

        async def take() -> tuple[str, str]:
            if await _find_root(env, cwd) is None:
                return "", ""
            snapshot = await capture_git(env, cwd)
            return snapshot, (await git_head(env, cwd) if snapshot else "")

        try:
            snapshot, head = await asyncio.wait_for(take(), GIT_RECAPTURE_TIMEOUT_S)
        except Exception:  # noqa: BLE001 — the stored snapshot stands in
            return stored
        if snapshot:
            view.record(self.name, "git", {"text": snapshot, "head": head})
        return snapshot

    async def resident(self, view: TaskView) -> Sequence[Message]:
        """After the messages a compaction keeps: the attribution note."""
        st = view.ext_state(self.name)
        if not view.keeps_tail or not isinstance(st, dict) \
                or not isinstance(st.get("attribution"), str):
            return ()
        return [user(st["attribution"], origin="system")]
