"""Test-only plugins the cluster test's worker processes load by name.

* ``cluster_scripted`` (provider): an offline model whose reply is a pure
  function of the conversation it is sent, so a task recovered on another
  worker after a crash makes the same choice the dead one would have.
* ``cluster_effect`` (plugin): the ``Effect`` tool, which appends one line
  ``<token> <task_id> <call_id>`` to a journal file before it answers: the
  observable side effect the test counts.
* ``cluster_schedule`` (plugin): the real ``schedule`` plugin on a clock
  that runs a minute behind, so a ``ScheduleWakeup`` (clamped to at least
  60 s) fires a few seconds later instead of a minute.

The conversation script. A root task starts from::

    CLUSTER-ROOT {"id": "r07", "journal": "/x/journal", "effects": 2,
                  "bg": 1.5, "agent": true, "wake": true}

and calls, one call per reply: ``Effect`` ``effects`` times, a background
``Bash`` that sleeps ``bg`` seconds and then journals ``<id>-bg`` (when
``bg`` > 0), an ``Agent`` running ``cluster-child`` (which calls ``Effect``
once and answers), a ``ScheduleWakeup`` whose prompt is
``CLUSTER-WAKE <id>``. Every ``CLUSTER-PING <id> <j>`` message it is sent
gets one ``Effect`` (token ``<id>-p<j>``); the wake-up, once it arrives, is
stopped. The task answers ``DONE <id>`` once the job's end (or loss), the
sub-agent's end and the wake-up have all reached it, else ``WAITING <id>``.
A call answered with an error (a crash's "outcome unknown") counts as
settled and is never issued again.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import re
import shlex
import time
from typing import Any

from noeta.core.extension import Extension, TaskView
from noeta.core.tool import Tool, ToolContext, ToolOutput, text_output
from noeta.core.types import Message, ModelRequest, ModelResponse, Text, ToolUse, Usage

ROOT = "CLUSTER-ROOT "
CHILD = "CLUSTER-CHILD "
PING = re.compile(r"CLUSTER-PING (\S+) (\d+)")
CHILD_AGENT = "cluster-child"

# --------------------------------------------------------------------------
# The side effect
# --------------------------------------------------------------------------


def journal_line(path: str, line: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, (line + "\n").encode())
        os.fsync(fd)
    finally:
        os.close(fd)


class EffectTool:
    name = "Effect"
    description = "Record a side effect: append the token to the journal."
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {"token": {"type": "string"}},
        "required": ["token"],
    }
    read_only = False
    clearable = False

    def __init__(self, journal: str) -> None:
        self.journal = journal

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        token = str(input["token"])
        journal_line(self.journal, f"{token} {ctx.task_id} {ctx.call_id}")
        # Widen the window in which a kill lands after the effect and
        # before the result is logged.
        await asyncio.sleep(random.uniform(0.01, 0.2))
        return text_output(f"did {token}")


class EffectPlugin(Extension):
    name = "cluster_effect"

    def __init__(self, *, journal: str) -> None:
        self.tool = EffectTool(journal)

    def tools(self, view: TaskView) -> list[Tool]:
        return [self.tool]


def fast_schedule(*, fire_after: float = 2.0) -> Extension:
    """The ``schedule`` plugin whose clock is ``60 - fire_after`` seconds
    behind: its shortest wake-up (60 s) is due ``fire_after`` real seconds
    after it is set."""
    from noeta.plugins.schedule import Schedule

    skew = 60.0 - fire_after
    return Schedule(clock=lambda: time.time() - skew)


# --------------------------------------------------------------------------
# The scripted model
# --------------------------------------------------------------------------


def _call(call_id: str, name: str, input: dict[str, Any]) -> ModelResponse:
    msg = Message(role="assistant", content=[ToolUse(id=call_id, name=name, input=input)])
    return ModelResponse(message=msg, stop_reason="tool_use", usage=Usage(uncached=10, output=1))


def _say(text: str) -> ModelResponse:
    return ModelResponse(message=Message(role="assistant", content=[Text(text=text)]),
                         stop_reason="end_turn", usage=Usage(uncached=10, output=1))


class _Seen:
    """What the conversation holds: calls issued, their results, and every
    text that is not the assistant's."""

    def __init__(self, messages: list[Message]) -> None:
        self.issued: set[str] = set()
        self.results: dict[str, tuple[str, bool]] = {}
        texts: list[str] = []
        for m in messages:
            for b in m.content:
                if isinstance(b, ToolUse):
                    self.issued.add(b.id)
                elif m.role != "assistant" and isinstance(b, Text):
                    texts.append(b.text)
                elif hasattr(b, "call_id"):
                    self.results[b.call_id] = (b.text, bool(b.is_error))
        self.text = "\n".join(texts)

    def failed(self, call_id: str) -> bool:
        return call_id in self.results and self.results[call_id][1]

    def notified(self, call_id: str) -> bool:
        return f"<tool-use-id>{call_id}</tool-use-id>" in self.text


def _spec(text: str, prefix: str) -> dict[str, Any] | None:
    at = text.find(prefix)
    if at < 0:
        return None
    raw = text[at + len(prefix):].split("\n", 1)[0]
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def root_step(spec: dict[str, Any], seen: _Seen) -> ModelResponse:
    rid = str(spec["id"])
    journal = str(spec["journal"])
    for i in range(int(spec.get("effects", 0))):
        cid = f"eff-{rid}-{i}"
        if cid not in seen.issued:
            return _call(cid, "Effect", {"token": f"{rid}-e{i}"})
    bg = float(spec.get("bg", 0) or 0)
    bg_id = f"bg-{rid}"
    if bg > 0 and bg_id not in seen.issued:
        command = (f"sleep {bg}; echo {shlex.quote(f'{rid}-bg ' + rid + ' ' + bg_id)}"
                   f" >> {shlex.quote(journal)}")
        return _call(bg_id, "Bash", {"command": command, "run_in_background": True,
                                     "description": f"cluster job {rid}"})
    agent_id = f"agt-{rid}"
    if spec.get("agent") and agent_id not in seen.issued:
        child = json.dumps({"id": rid, "journal": journal})
        return _call(agent_id, "Agent", {"description": f"cluster child {rid}",
                                         "prompt": CHILD + child,
                                         "subagent_type": CHILD_AGENT})
    wake_id = f"wake-{rid}"
    if spec.get("wake") and wake_id not in seen.issued:
        return _call(wake_id, "ScheduleWakeup", {
            "delaySeconds": 60, "reason": "cluster test wake-up",
            "prompt": f"CLUSTER-WAKE {rid}", "noop": False})
    for m in PING.finditer(seen.text):
        if m.group(1) != rid:
            continue
        cid = f"png-{rid}-{m.group(2)}"
        if cid not in seen.issued:
            return _call(cid, "Effect", {"token": f"{rid}-p{m.group(2)}"})
    woke = f"CLUSTER-WAKE {rid}" in seen.text
    stop_id = f"wstop-{rid}"
    if spec.get("wake") and woke and stop_id not in seen.issued:
        return _call(stop_id, "ScheduleWakeup", {"stop": True})
    pending = []
    if bg > 0 and not seen.failed(bg_id) and not seen.notified(bg_id):
        pending.append("job")  # its end, or its loss, names the call
    if spec.get("agent") and not seen.failed(agent_id) and not seen.notified(agent_id):
        pending.append("agent")
    if spec.get("wake") and not seen.failed(wake_id) and not woke:
        pending.append("wake")
    if pending:
        return _say(f"WAITING {rid} for {', '.join(pending)}")
    return _say(f"DONE {rid}")


def child_step(spec: dict[str, Any], seen: _Seen) -> ModelResponse:
    rid = str(spec["id"])
    cid = f"ceff-{rid}"
    if cid not in seen.issued:
        return _call(cid, "Effect", {"token": f"{rid}-c"})
    return _say(f"CHILD-DONE {rid}")


class ScriptedProvider:
    family = "scripted"

    async def complete(self, request: ModelRequest, on_delta: Any | None = None) -> ModelResponse:
        await asyncio.sleep(random.uniform(0, 0.05))
        seen = _Seen(list(request.messages))
        spec = _spec(seen.text, ROOT)
        if spec is not None:
            return root_step(spec, seen)
        spec = _spec(seen.text, CHILD)
        if spec is not None:
            return child_step(spec, seen)
        return _say("OK")


def provider(**_kw: Any) -> ScriptedProvider:
    """The ``noeta.providers`` factory (base URL, key and headers ignored)."""
    return ScriptedProvider()
