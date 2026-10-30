"""Test-only plugins the cluster test's worker processes load by name.

* ``cluster_scripted`` (provider): an offline model whose reply is a pure
  function of the conversation it is sent, so a task recovered on another
  worker after a crash makes the same choice the dead one would have.
* ``cluster_effect`` (plugin): the ``Effect`` tool, which appends one line
  ``<token> <task_id> <call_id>`` to a journal file before it answers: the
  observable side effect the test counts; and ``Lookup``, a ``retry_safe``
  tool that takes a while and writes each run (with its idempotency key) to
  ``<journal>.lookups``, so a crash during it shows as a second run.
* ``cluster_schedule`` (plugin): the real ``schedule`` plugin on a clock
  that runs a minute behind, so a ``ScheduleWakeup`` (clamped to at least
  60 s) fires a few seconds later instead of a minute.
* ``cluster_crash`` (plugin): when armed, kills its worker right after a
  take is committed and before the plugins after it react to it.

The conversation script. A root task starts from::

    CLUSTER-ROOT {"id": "r07", "journal": "/x/journal", "effects": 2,
                  "bg": 1.5, "agent": true, "wake": true}

and calls, one call per reply: ``Effect`` ``effects`` times, ``Lookup``
running ``lookup`` seconds (when ``lookup`` > 0), a background
``Bash`` that sleeps ``bg`` seconds and then journals ``<id>-bg`` (when
``bg`` > 0), an ``Agent`` running ``cluster-child`` (which calls ``Effect``
once and answers), a ``ScheduleWakeup`` whose prompt is
``CLUSTER-WAKE <id>``. With ``child_bg`` > 0 the sub-agent also starts a
background ``Bash`` (``child_bg`` seconds, journalling ``<id>-cbg``) and ends
without waiting for it; the root then waits for that job's end or loss too
(a sub-agent whose job could not start hands back ``CHILD-DONE <id> NO-JOB``
and leaves nothing to wait for). With ``agent_first`` the ``Agent`` call
comes right after the ``Effect`` calls, before ``Lookup`` and the job.
With ``cron`` > 0 it loads the cron tools (``ToolSearch``), creates a
recurring ``CronCreate`` job whose prompt is ``CLUSTER-CRON <id>``, and
deletes it (``CronDelete``, again if a crash leaves the outcome unknown)
once it has fired ``cron`` times. Every ``CLUSTER-PING <id> <j>`` message it is sent
gets one ``Effect`` (token ``<id>-p<j>``); the wake-up, once it arrives, is
stopped. The task answers ``DONE <id>`` once the job's end (or loss), the
sub-agent's end and the wake-up have all reached it, else ``WAITING <id>``.
A call answered with an error (a crash's "outcome unknown") counts as
settled and is never issued again.

A spill task starts from ``CLUSTER-SPILL {"id": "s1", "lines": 4000}``: a
``Bash`` call prints that many lines (more than ``Bash`` returns inline, so
its output is saved to a file) and the task answers ``SPILLED <id>``; once
it is sent ``CLUSTER-SPILL-READ <id>`` it calls ``Read`` on the saved file's
last lines and answers ``SPILL-READ <id> ok`` when the last line is there
(``missing`` otherwise).
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

from noeta.core.events import External, InputTaken, Recorded
from noeta.core.extension import Extension, TaskView
from noeta.core.tool import Tool, ToolContext, ToolOutput, text_output
from noeta.core.types import Message, ModelRequest, ModelResponse, Text, ToolUse, Usage

ROOT = "CLUSTER-ROOT "
CHILD = "CLUSTER-CHILD "
SPILL = "CLUSTER-SPILL "
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


class LookupTool:
    name = "Lookup"
    description = "Look the token up (takes a while; safe to run again)."
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {"token": {"type": "string"}, "seconds": {"type": "number"}},
        "required": ["token"],
    }
    read_only = True
    clearable = False
    retry_safe = True

    def __init__(self, journal: str) -> None:
        self.journal = journal + ".lookups"

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        token = str(input["token"])
        journal_line(self.journal, f"{token} {ctx.idempotency_key}")
        await asyncio.sleep(float(input.get("seconds") or 0))
        return text_output(f"found {token}")


class EffectPlugin(Extension):
    name = "cluster_effect"

    def __init__(self, *, journal: str) -> None:
        self.tool = EffectTool(journal)
        self.lookup = LookupTool(journal)

    def tools(self, view: TaskView) -> list[Tool]:
        return [self.tool, self.lookup]


class CrashPlugin(Extension):
    """Kills its worker process right after a take of ``External(<name>)``
    is committed, before any plugin listed after it decides what follows
    from it, once: when the file ``arm`` holds ``<name>``. The file is
    removed first (so one worker dies), and ``<arm>.done`` written."""

    name = "cluster_crash"

    def __init__(self, *, arm: str) -> None:
        self.arm = arm

    async def on_event(self, view: TaskView, recorded: Recorded) -> None:
        ev = recorded.event
        if not isinstance(ev, InputTaken):
            return
        try:
            with open(self.arm) as f:
                want = f.read().strip()
        except OSError:
            return
        if not any(isinstance(i.body, External) and i.body.name == want for i in ev.items):
            return
        try:
            os.unlink(self.arm)
        except FileNotFoundError:
            return  # another worker took it
        journal_line(self.arm + ".done", f"{os.getpid()} {view.task_id} {recorded.seq}")
        os._exit(70)


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


def _agent_call(spec: dict[str, Any], rid: str, journal: str) -> ModelResponse:
    child = json.dumps({"id": rid, "journal": journal,
                        "bg": float(spec.get("child_bg", 0) or 0)})
    return _call(f"agt-{rid}", "Agent", {"description": f"cluster child {rid}",
                                         "prompt": CHILD + child,
                                         "subagent_type": CHILD_AGENT})


def root_step(spec: dict[str, Any], seen: _Seen) -> ModelResponse:
    rid = str(spec["id"])
    journal = str(spec["journal"])
    for i in range(int(spec.get("effects", 0))):
        cid = f"eff-{rid}-{i}"
        if cid not in seen.issued:
            return _call(cid, "Effect", {"token": f"{rid}-e{i}"})
    agent_id = f"agt-{rid}"
    if spec.get("agent") and spec.get("agent_first") and agent_id not in seen.issued:
        return _agent_call(spec, rid, journal)
    lookup = float(spec.get("lookup", 0) or 0)
    if lookup > 0 and f"lkp-{rid}" not in seen.issued:
        return _call(f"lkp-{rid}", "Lookup", {"token": f"{rid}-l", "seconds": lookup})
    bg = float(spec.get("bg", 0) or 0)
    bg_id = f"bg-{rid}"
    if bg > 0 and bg_id not in seen.issued:
        command = (f"sleep {bg}; echo {shlex.quote(f'{rid}-bg ' + rid + ' ' + bg_id)}"
                   f" >> {shlex.quote(journal)}")
        return _call(bg_id, "Bash", {"command": command, "run_in_background": True,
                                     "description": f"cluster job {rid}"})
    if spec.get("agent") and agent_id not in seen.issued:
        return _agent_call(spec, rid, journal)
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
    cron_step, cron_pending = _cron(rid, int(spec.get("cron", 0) or 0), seen)
    if cron_step is not None:
        return cron_step
    woke = f"CLUSTER-WAKE {rid}" in seen.text
    stop_id = f"wstop-{rid}"
    if spec.get("wake") and woke and stop_id not in seen.issued:
        return _call(stop_id, "ScheduleWakeup", {"stop": True})
    pending = []
    if bg > 0 and not seen.failed(bg_id) and not seen.notified(bg_id):
        pending.append("job")  # its end, or its loss, names the call
    if spec.get("agent") and not seen.failed(agent_id) and not seen.notified(agent_id):
        pending.append("agent")
    if spec.get("agent") and float(spec.get("child_bg", 0) or 0) > 0 \
            and not seen.failed(agent_id) and not seen.notified(f"cbg-{rid}") \
            and f"CHILD-DONE {rid} NO-JOB" not in seen.text:
        pending.append("the sub-agent's job")
    if spec.get("wake") and not seen.failed(wake_id) and not woke:
        pending.append("wake")
    if cron_pending:
        pending.append("cron")
    if pending:
        return _say(f"WAITING {rid} for {', '.join(pending)}")
    return _say(f"DONE {rid}")


#: Attempts at deleting the cron job before the script gives up on it.
CRON_DELETES = 4


def _cron(rid: str, fires: int, seen: _Seen) -> tuple[ModelResponse | None, bool]:
    """The cron part of a root's script: ``(the next call, still waiting)``."""
    if fires <= 0:
        return None, False
    search, create = f"ts-{rid}", f"cron-{rid}"
    if search not in seen.issued:
        return _call(search, "ToolSearch", {"query": "select:CronCreate,CronDelete",
                                            "max_results": 5}), True
    if create not in seen.issued:
        return _call(create, "CronCreate", {"cron": "* * * * *", "recurring": True,
                                            "prompt": f"CLUSTER-CRON {rid}"}), True
    if create not in seen.results or seen.failed(create):
        return None, create not in seen.results
    if seen.text.count(f"CLUSTER-CRON {rid}") < fires:
        return None, True
    job = re.search(r"job ([0-9a-f]{8})", seen.results[create][0])
    for n in range(CRON_DELETES):
        delete = f"cdel-{rid}-{n}"
        if delete not in seen.issued:
            return _call(delete, "CronDelete", {"id": job.group(1) if job else ""}), True
        if delete not in seen.results:
            return None, True
        if not seen.failed(delete):
            return None, False
    return None, False


def child_step(spec: dict[str, Any], seen: _Seen) -> ModelResponse:
    rid = str(spec["id"])
    cid = f"ceff-{rid}"
    if cid not in seen.issued:
        return _call(cid, "Effect", {"token": f"{rid}-c"})
    bg = float(spec.get("bg", 0) or 0)
    if bg > 0 and f"cbg-{rid}" not in seen.issued:
        line = shlex.quote(f"{rid}-cbg {rid} cbg-{rid}")
        return _call(f"cbg-{rid}", "Bash", {
            "command": f"sleep {bg}; echo {line} >> {shlex.quote(str(spec['journal']))}",
            "run_in_background": True, "description": f"cluster child job {rid}"})
    if bg > 0 and seen.failed(f"cbg-{rid}"):
        # Handed back: with SubagentHandback offered, the root's notice of
        # the end carries no last reply, only the handed-back report.
        if f"chb-{rid}" not in seen.issued:
            return _call(f"chb-{rid}", "SubagentHandback",
                         {"message": f"CHILD-DONE {rid} NO-JOB"})
        return _say(f"CHILD-DONE {rid} NO-JOB")
    return _say(f"CHILD-DONE {rid}")


def spill_step(spec: dict[str, Any], seen: _Seen) -> ModelResponse:
    rid, lines = str(spec["id"]), int(spec.get("lines", 4000))
    run, read = f"spl-{rid}", f"spr-{rid}"
    if run not in seen.issued:
        return _call(run, "Bash", {"command": f"seq -f 'spill-line %g' 1 {lines}",
                                   "description": "Print many lines"})
    if f"CLUSTER-SPILL-READ {rid}" not in seen.text:
        return _say(f"SPILLED {rid}")
    saved = re.search(r"Full output saved to: (\S+)", seen.results.get(run, ("", True))[0])
    if saved is not None and read not in seen.issued:
        return _call(read, "Read", {"file_path": saved.group(1), "offset": lines - 1,
                                    "limit": 5})
    got, failed = seen.results.get(read, ("", True))
    ok = not failed and f"spill-line {lines}" in got
    return _say(f"SPILL-READ {rid} {'ok' if ok else 'missing'}")


class ScriptedProvider:
    family = "scripted"

    async def complete(self, request: ModelRequest, on_delta: Any | None = None) -> ModelResponse:
        await asyncio.sleep(random.uniform(0, 0.05))
        seen = _Seen(list(request.messages))
        spec = _spec(seen.text, SPILL)
        if spec is not None:
            return spill_step(spec, seen)
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
