"""File checkpoints, so a person can put files back as they were before a
prompt (Claude Code's rewind).

Right before ``Edit``, ``Write`` or ``NotebookEdit`` runs (its
``ToolStarted`` is in the log), the file's content is recorded, once per
file and prompt. Contents are content-addressed: a task records a blob once
and later snapshots of the same bytes name it by hash. Records, read by
front ends (``noeta.host.rewind``):

* ``snapshot`` ``{"path", "sha", "blob"?, "skipped"?}`` — ``sha`` is
  ``None`` when the file did not exist (a rewind removes it); ``blob``
  (base64) comes with the first snapshot of those bytes in this task;
  ``skipped`` says why no content was kept (too large, unreadable).
* ``restored`` ``{"files", "errors"}`` — what a ``restore_files`` signal
  wrote back.

The ``restore_files`` signal (``{"files": [{"path", "data": base64 |
None}]}``) writes files back through the task's environment, wherever the
task runs; ``None`` removes the file. Changes made through ``Bash`` are not
tracked, as in Claude Code.
"""

from __future__ import annotations

import base64
import hashlib
import logging
from collections.abc import Mapping
from typing import Any

from noeta.core.events import External, InputTaken, Recorded, ToolStarted
from noeta.core.extension import Extension, TaskView
from noeta.core.types import ToolUse

__all__ = ["EDITING_TOOLS", "MAX_SNAPSHOT_BYTES", "NAME", "RESTORE_SIGNAL", "Checkpoints"]

log = logging.getLogger("noeta.plugins.checkpoints")

NAME = "checkpoints"
RESTORE_SIGNAL = "restore_files"
#: The tools whose target file is saved first, and the input field naming it.
EDITING_TOOLS: Mapping[str, str] = {"Edit": "file_path", "Write": "file_path",
                                    "NotebookEdit": "notebook_path"}
#: A larger file is noted but not kept (a rewind leaves it as it is).
MAX_SNAPSHOT_BYTES = 10 * 1024 * 1024


def _last_prompt(view: TaskView) -> int:
    """The log seq of the person's newest message (0 before any)."""
    st = view.state
    for message, seq in zip(reversed(st.history), reversed(st.history_seqs), strict=True):
        if message.role == "user" and message.origin in (None, "human") and message.text:
            return seq
    return 0


class Checkpoints(Extension):
    name = NAME

    def initial_state(self) -> Any:
        #: ``shas``: blobs this task recorded; ``saved``: per path, the
        #: prompt (log seq) it was last saved for.
        return {"shas": frozenset(), "saved": {}}

    def reduce(self, state: Any, kind: str, data: Any) -> Any:
        if kind != "snapshot" or not isinstance(data, dict):
            return state
        shas = state["shas"] | {data["sha"]} if data.get("blob") is not None else state["shas"]
        saved = {**state["saved"], str(data.get("path")): str(data.get("prompt") or "")}
        return {"shas": shas, "saved": saved}

    async def on_event(self, view: TaskView, recorded: Recorded) -> None:
        ev = recorded.event
        if isinstance(ev, ToolStarted) and ev.call.name in EDITING_TOOLS:
            await self._snapshot(view, ev.call)
        elif isinstance(ev, InputTaken):
            for item in ev.items:
                if isinstance(item.body, External) and item.body.name == RESTORE_SIGNAL:
                    await self._restore(view, item.body.data)

    async def _snapshot(self, view: TaskView, call: ToolUse) -> None:
        raw = call.input.get(EDITING_TOOLS[call.name])
        env = view.env
        if not isinstance(raw, str) or not raw or env is None:
            return
        try:
            path = await env.realpath(raw)
        except Exception:  # noqa: BLE001 — the tool reports a bad path itself
            return
        state = view.ext_state(NAME) or self.initial_state()
        prompt = f"{view.task_id}:{_last_prompt(view)}"
        if path in state["saved"] and state["saved"][path] == prompt:
            return  # saved for this prompt already: the first content is the one to keep
        data: dict[str, Any] = {"path": path, "prompt": prompt, "sha": None}
        try:
            stat = await env.stat(path)
            if stat is not None and stat.is_dir:
                return
            if stat is not None and stat.size > MAX_SNAPSHOT_BYTES:
                data["skipped"] = f"larger than {MAX_SNAPSHOT_BYTES} bytes"
                data["sha"] = ""
            elif stat is not None:
                content = await env.read_bytes(path)
                data["sha"] = hashlib.sha256(content).hexdigest()
                if data["sha"] not in state["shas"]:
                    data["blob"] = base64.b64encode(content).decode()
        except Exception as exc:  # noqa: BLE001 — a missing snapshot only limits a rewind
            data["skipped"] = f"{type(exc).__name__}: {exc}"
            data["sha"] = ""
        view.record(NAME, "snapshot", data)

    async def _restore(self, view: TaskView, data: Any) -> None:
        env = view.env
        files = data.get("files") if isinstance(data, dict) else None
        if env is None or not isinstance(files, list):
            return
        done: list[str] = []
        errors: dict[str, str] = {}
        for entry in files:
            path = entry.get("path") if isinstance(entry, dict) else None
            if not isinstance(path, str) or not path:
                continue
            try:
                blob = entry.get("data")
                if blob is None:
                    if await env.stat(path) is not None:
                        result = await env.run(["rm", "-f", "--", path], timeout=30)
                        if result.exit_code != 0:
                            raise OSError(result.stderr.decode(errors="replace").strip())
                else:
                    await env.write_bytes(path, base64.b64decode(blob))
                done.append(path)
            except Exception as exc:  # noqa: BLE001 — reported, the rest still restored
                errors[path] = f"{type(exc).__name__}: {exc}"
        view.record(NAME, "restored", {"files": done, "errors": errors})
        text = f"restored {len(done)} file(s)"
        if errors:
            text += f"; {len(errors)} could not be restored: " + ", ".join(sorted(errors))
        view.notify(text)
