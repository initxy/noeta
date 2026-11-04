"""A task's title, asked of the model as Claude Code names a session: once,
from the first prompt that says something (at least 10 characters), in a
side request sent beside the main one. A task nobody follows live
(``interactive: false``, the headless protocol) is not named, as Claude Code
names no session in print mode.

The title is recorded as ``("title", "title", {"title": ...})``; front ends
read it from the task's events. A failed request leaves the task untitled.
"""

from __future__ import annotations

import asyncio
import json
import logging
from importlib import resources
from typing import Any

from noeta.core.events import External, InputTaken, Recorded, Say
from noeta.core.extension import Extension, TaskView
from noeta.core.types import user
from noeta.toolkit.interactive import interactive
from noeta.toolkit.plugin_records import TITLE
from noeta.toolkit.side_requests import side_request

__all__ = ["PROMPT", "Title", "title_of"]

log = logging.getLogger("noeta.plugins.title")

NAME = TITLE
SIGNAL = "title.named"
#: A prompt shorter than this (a greeting) does not name the task.
MIN_PROMPT_CHARS = 10

#: The side request's system prompt (``title/name``).
PROMPT = resources.files("noeta.plugins").joinpath("title.md").read_text("utf-8").rstrip("\n")

LANGUAGE = ("Write the title in the session's main language; a few words or code tokens in "
            "another language do not change that, nor does the fact that these instructions "
            "are in English.")

SCHEMA: dict[str, Any] = {"type": "object", "properties": {"title": {"type": "string"}},
                          "required": ["title"], "additionalProperties": False}


def title_of(state: Any) -> str | None:
    """The title recorded in this plugin's folded state, if any."""
    return state.get("title") if isinstance(state, dict) else None


class Title(Extension):
    """Names the task once. Constructor: ``model`` for the side request
    (default: the task's model)."""

    name = NAME

    def __init__(self, *, model: str | None = None) -> None:
        self.model = model
        self._jobs: set[asyncio.Task[None]] = set()

    def reduce(self, state: Any, kind: str, data: Any) -> Any:
        st = dict(state or {})
        if kind == "asked":
            st["asked"] = True
        elif kind == "title":
            st["title"] = data.get("title")
        return st

    async def on_event(self, view: TaskView, recorded: Recorded) -> None:
        ev = recorded.event
        if not isinstance(ev, InputTaken):
            return
        st = view.ext_state(self.name) or {}
        for item in ev.items:
            body = item.body
            if isinstance(body, External) and body.name == SIGNAL:
                title = str((body.data or {}).get("title") or "").strip()
                if title and not title_of(st):
                    view.record(self.name, "title", {"title": title})
            elif isinstance(body, Say) and body.message.origin in (None, "human") \
                    and not st.get("asked") and not view.state.parent_id \
                    and interactive(view.state.config):
                text = body.message.text.strip()
                if len(text) < MIN_PROMPT_CHARS or view.complete is None or view.post is None:
                    continue
                view.record(self.name, "asked")
                st = {**st, "asked": True}
                job = asyncio.ensure_future(self._ask(view, text))
                self._jobs.add(job)
                job.add_done_callback(self._jobs.discard)

    async def _ask(self, view: TaskView, text: str) -> None:
        assert view.complete is not None and view.post is not None
        request = side_request(
            model=self.model or view.model, identity=view.identity,
            prompt=view.prompt("title/name", PROMPT),
            messages=[user(f"<session>\n{text}\n</session>\n\n{LANGUAGE}")],
            effort=view.effort, output_schema=SCHEMA)
        try:
            response = await view.complete(request)
            title = json.loads(response.message.text).get("title")
        except Exception:  # a title is decoration; it never fails the task
            log.debug("naming task %s failed", view.task_id, exc_info=True)
            return
        if isinstance(title, str) and title.strip():
            await view.post(External(name=SIGNAL, data={"title": title.strip()}),
                            dedupe_key=f"{SIGNAL}:{view.task_id}")
