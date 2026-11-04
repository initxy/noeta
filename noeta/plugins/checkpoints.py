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
task runs; ``None`` removes the file. It only puts back what was
snapshotted in this task's log, the conversations it continues (whole: a
rewound conversation's fork restores what came after its fork point) and
the sub-agents under any of them: a path with a snapshot of it (outside the
workspace only one an editing call finished writing without an error, so
never one the file tools' fence or a refusal kept them from), and content
hashing to one of that path's snapshots
(``None`` only when a snapshot found it absent); anything else is reported
as not restored. Changes made through ``Bash`` are not tracked, as in
Claude Code.
"""

from __future__ import annotations

import base64
import hashlib
import logging
from collections.abc import Mapping, Sequence
from typing import Any

from noeta.core.events import (
    ExtensionRecorded,
    External,
    InputTaken,
    Recorded,
    TaskCreated,
    ToolFinished,
    ToolStarted,
)
from noeta.core.extension import Extension, TaskView
from noeta.core.types import ToolUse
from noeta.toolkit.plugin_records import CHECKPOINTS
from noeta.toolkit.tools import Workspace, path_within

__all__ = ["EDITING_TOOLS", "MAX_SNAPSHOT_BYTES", "NAME", "RESTORE_SIGNAL", "Checkpoints"]

log = logging.getLogger("noeta.plugins.checkpoints")

NAME = CHECKPOINTS
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
        if message.from_person and message.text:
            return seq
    return 0


#: How far a restore follows continued conversations back.
_MAX_LINEAGE = 64


async def _restorable(view: TaskView) -> tuple[dict[str, frozenset[str | None]], set[str]]:
    """Per path, every content sha a snapshot found: in this task's state,
    and in the logs a rewind's file plan reads (``noeta.host.rewind``) —
    each conversation this task continues, whole, and every sub-agent under
    any of them; and the paths (as the calls named them) that an editing
    call in those logs finished writing without an error."""
    state = view.ext_state(NAME) or {}
    snaps: dict[str, frozenset[str | None]] = dict(state.get("snaps") or {})
    written: set[str] = set()
    read, children = view.read_log, view.child_tasks
    if read is None or children is None:
        return snaps, written
    logs: dict[str, Sequence[Recorded]] = {}
    current: str | None = view.task_id
    while current and current not in logs and len(logs) < _MAX_LINEAGE:
        logs[current] = await read(current)
        first = logs[current][0].event if logs[current] else None
        current = first.history_from if isinstance(first, TaskCreated) else None
    queue, seen = list(logs), set()
    while queue:
        tid = queue.pop()
        if tid in seen:
            continue
        seen.add(tid)
        queue += await children(tid)
        started: dict[str, str] = {}
        for rec in logs.get(tid) or await read(tid):
            ev = rec.event
            if isinstance(ev, ToolStarted) and ev.call.name in EDITING_TOOLS:
                raw = ev.call.input.get(EDITING_TOOLS[ev.call.name])
                if isinstance(raw, str) and raw:
                    started[ev.call.id] = raw
            elif isinstance(ev, ToolFinished) and not ev.result.is_error \
                    and ev.result.call_id in started:
                written.add(started[ev.result.call_id])
            if isinstance(ev, ExtensionRecorded) and ev.extension == NAME \
                    and ev.kind == "snapshot" and isinstance(ev.data, dict) \
                    and ev.data.get("sha") != "" and ev.data.get("path"):
                path = str(ev.data["path"])
                snaps[path] = snaps.get(path, frozenset()) | {ev.data.get("sha")}
    return snaps, written


class Checkpoints(Extension):
    name = NAME

    def initial_state(self) -> Any:
        #: ``shas``: blobs this task recorded; ``saved``: per path, the
        #: prompt (log seq) it was last saved for; ``snaps``: per path, every
        #: content sha a snapshot found (``None``: the file did not exist).
        return {"shas": frozenset(), "saved": {}, "snaps": {}}

    def reduce(self, state: Any, kind: str, data: Any) -> Any:
        if kind != "snapshot" or not isinstance(data, dict):
            return state
        shas = state["shas"] | {data["sha"]} if data.get("blob") is not None else state["shas"]
        path = str(data.get("path"))
        saved = {**state["saved"], path: str(data.get("prompt") or "")}
        snaps = dict(state.get("snaps") or {})
        if data.get("sha") != "":  # "" = no content kept: nothing to restore from
            snaps[path] = snaps.get(path, frozenset()) | {data.get("sha")}
        return {"shas": shas, "saved": saved, "snaps": snaps}

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
        snaps, written = await _restorable(view)
        done: list[str] = []
        errors: dict[str, str] = {}
        ws: Workspace | None = None
        outside_ok: set[str] | None = None
        for entry in files:
            path = entry.get("path") if isinstance(entry, dict) else None
            if not isinstance(path, str) or not path:
                continue
            try:
                ws = ws or await Workspace.of(env)
                resolved = await ws.canonical(path)
                known = snaps.get(resolved)
                if known is None:
                    raise PermissionError("no snapshot of the file was taken")
                if not path_within(resolved, ws.root):
                    if outside_ok is None:
                        outside_ok = {await ws.canonical(p) for p in written}
                    if resolved not in outside_ok:
                        raise PermissionError(
                            "outside the workspace, and no call wrote the file")
                blob = entry.get("data")
                if blob is None:
                    if None not in known:
                        raise PermissionError("no snapshot found the file absent")
                    if await env.stat(resolved) is not None:
                        result = await env.run(["rm", "-f", "--", resolved], timeout=30)
                        if result.exit_code != 0:
                            raise OSError(result.stderr.decode(errors="replace").strip())
                else:
                    content = base64.b64decode(blob)
                    if hashlib.sha256(content).hexdigest() not in known:
                        raise PermissionError("the content matches no snapshot of the file")
                    await env.write_bytes(resolved, content)
                done.append(path)
            except Exception as exc:  # noqa: BLE001 — reported, the rest still restored
                errors[path] = f"{type(exc).__name__}: {exc}"
        view.record(NAME, "restored", {"files": done, "errors": errors})
        text = f"restored {len(done)} file(s)"
        if errors:
            text += f"; {len(errors)} could not be restored: " + ", ".join(sorted(errors))
        view.notify(text)
