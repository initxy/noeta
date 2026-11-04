"""Replay a recorded scenario against noeta and capture what noeta sends.

    python -m scripts.parity.replay [--version 2.1.312] [--session m1 ...] [--out parity/out]

A local fake Anthropic Messages endpoint answers each of noeta's model calls
with the turn Claude Code's model gave at the same step of the same scenario
(``parity/<version>/script/<session>.json``): main-loop calls by step and
position in the step, sub-agent calls by their opening prompt, compaction
calls with the recorded summary. noeta runs as the ``noeta`` command would —
the default agents with its real Anthropic provider, a fresh store and
``NOETA_HOME``, the scenario project in a scratch directory — and executes
every tool for real. Approvals are allowed and questions get their first
option, as the recording's driver answered Claude Code.

Every request noeta makes is kept (``<out>/<session>.json``: how it was
routed and its facts; ``<out>/raw/<session>/`` the bodies), then the
comparator writes ``parity/report.md``. Exit status 1 when anything differs.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
from collections.abc import Iterator, Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from noeta.plugins.compaction.estimate import text_tokens
from scripts.parity import compare, facts, wire
from scripts.parity.distill import session_prompts, step_marker
from scripts.parity.project import NOETA_PLACES, write_project

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
MODEL = "claude-opus-5-5"
STEP_TIMEOUT_S = 120.0
TOOL_NAMES = wire.TOOL_NAMES
#: Opus 5.5 as Claude Code sends it, for noeta's model catalog.
MODEL_ROW = {"id": MODEL, "context_window": 1_000_000, "max_output_tokens": 128_000,
             "supports_vision": True, "family": "anthropic"}
#: A scenario's Claude Code environment knob -> noeta's name for it.
NOETA_ENV = {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "NOETA_AUTO_COMPACT_WINDOW",
             "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "NOETA_AUTOCOMPACT_PCT_OVERRIDE"}


#: Side requests answered the way the recordings answered them: every
#: recorded classification ended in an allow (a stage-1 block in r2/m5 was
#: cleared at stage 2), so both stages say allow here.
SIDE_ANSWERS: dict[str, dict[str, Any]] = {
    "auto-mode-classifier": {"content": [{"type": "text", "text": "<block>no</block>"}],
                             "stop_reason": "end_turn"},
    "plan-name": {"content": [{"type": "text", "text": '{"name": "plan"}'}],
                  "stop_reason": "end_turn"},
}


#: A plan file path in Claude Code's recorded turns (``<config>/plans/<a>-<b>-<c>.md``).
PLAN_FILE = re.compile(r"\{HOME\}[^\"\s]*/plans/[a-z]+-[a-z]+-[a-z]+\.md")
NOETA_PLAN_FILE = re.compile(r"/[^\s'\"]+/plans/[a-z]+-[a-z]+-[a-z]+\.md")


#: A background task's output file in Claude Code's recorded turns.
TASK_OUTPUT = re.compile(r"/private/tmp/claude-\d+/[^\s\"']*/tasks/[A-Za-z0-9]+\.output")


def _output_files(events: list[Any]) -> list[str]:
    """noeta's background job output files, in the order the jobs started."""
    return [str(rec.event.data["output_file"]) for rec in events
            if getattr(rec.event, "extension", "") == "shell"
            and getattr(rec.event, "kind", "") == "started"
            and isinstance(rec.event.data, dict) and rec.event.data.get("output_file")]


def _plan_path(events: list[Any]) -> str:
    path = ""
    for rec in events:
        ev = rec.event
        data = getattr(ev, "data", None)
        if getattr(ev, "extension", "") == "permissions" and isinstance(data, dict) and \
                isinstance(data.get("plan"), dict):
            path = str(data["plan"].get("path") or "")
    return path


def _texts(m: Mapping[str, Any]) -> list[str]:
    c = m.get("content")
    if isinstance(c, str):
        return [c]
    return [b.get("text") or "" for b in c or [] if b.get("type") == "text"]


def _shows(text: str, say: str) -> bool:
    return step_marker(text, say) or say in text


class Replayer:
    """Picks the recorded turn that answers one of noeta's requests."""

    def __init__(self, script: Mapping[str, Any], steps: list[Mapping[str, Any]],
                 subs: Mapping[str, str], kinds: facts.Kinds) -> None:
        self.script = script
        self.steps = steps
        self.subs = subs  # placeholder -> path
        self.kinds = kinds
        self.lock = threading.Lock()
        self.ids = 0
        self.state = (0, -1)  # main loop: (step index, turn index) last served
        self.compactions_used: set[int] = set()
        self.notes: list[str] = []
        self.current_step = 0  # the step the runner is driving
        self.stalls = threading.Event()
        #: noeta's plan file, once plan mode named it.
        self.plan_path = ""
        #: noeta's background output files in start order, and Claude
        #: Code's recorded ones mapped to them in order of first use.
        self.output_files: list[str] = []
        self.outputs: dict[str, str] = {}
        #: Steps in which Claude Code asked a sub-agent's progress label,
        #: and how far noeta's clock for that label is moved ahead: the label
        #: comes 30 s into a sub-agent's run, which a replay does not last.
        self.label_steps: set[str] = set()
        self.clock_ahead = 0.0
        self._labelled: set[int] = set()

    def fill(self, value: Any) -> Any:
        if isinstance(value, str):
            if self.plan_path:
                # Claude Code's plan file, as noeta named its own.
                value = PLAN_FILE.sub(lambda _m: self.plan_path, value)
            for k, v in self.subs.items():
                value = value.replace(k, v)
            return TASK_OUTPUT.sub(self._output_file, value)
        if isinstance(value, list):
            return [self.fill(v) for v in value]
        if isinstance(value, dict):
            return {k: self.fill(v) for k, v in value.items()}
        return value

    def _output_file(self, m: re.Match[str]) -> str:
        """Claude Code's background output file as noeta's: the n-th one
        recorded is noeta's n-th job's."""
        if m.group(0) not in self.outputs and len(self.outputs) < len(self.output_files):
            self.outputs[m.group(0)] = self.output_files[len(self.outputs)]
        return self.outputs.get(m.group(0), m.group(0))

    def _main_position(self, msgs: list[Mapping[str, Any]]) -> tuple[int, int]:
        best_step, at = -1, -1
        for mi, m in enumerate(msgs):
            if m.get("role") != "user":
                continue
            for t in _texts(m):
                if self.kinds.name(t) == "compact-summary":
                    continue  # a summary quotes earlier prompts
                for si, s in enumerate(self.steps):
                    if si >= best_step and _shows(t, s["say"]):
                        best_step, at = si, mi
        step, turn = self.state
        if best_step < 0 or best_step < step:
            return step, turn + 1  # the prompt was compacted away
        n = sum(1 for m in msgs[at + 1:] if m.get("role") == "assistant")
        return best_step, n

    def _subagent(self, msgs: list[Mapping[str, Any]]) -> tuple[int, Mapping[str, Any]] | None:
        first = next((m for m in msgs if m.get("role") == "user"), None)
        if first is None:
            return None
        texts = " ".join(_texts(first))
        for i, sub in enumerate(self.script.get("subagents") or []):
            key = self.fill(sub["prompt"])[:120]
            if key and key in texts:
                return i, sub
        return None

    def route(self, body: Mapping[str, Any]) -> tuple[dict[str, Any], Mapping[str, Any] | None]:
        """``(routing, turn)``: how the request was read and the recorded turn
        to answer with (``None``: no recorded turn)."""
        msgs = list(body.get("messages") or [])
        for m in msgs:  # noeta names its plan file in the plan-mode note
            for t in _texts(m):
                found = NOETA_PLAN_FILE.search(t)
                if found and "Plan mode is on" in t:
                    self.plan_path = found.group(0)
        kind = wire.request_kind("/v1/messages", body, self.kinds)
        with self.lock:
            if kind.startswith("compaction"):
                comps = self.script.get("compactions") or []
                pick = next((i for i, c in enumerate(comps) if i not in self.compactions_used
                             and c["step"] == self.steps[self.current_step]["name"]),
                            next((i for i in range(len(comps))
                                  if i not in self.compactions_used), None))
                if pick is None:
                    return {"kind": kind, "step": self._step_name()}, None
                self.compactions_used.add(pick)
                return {"kind": kind, "step": self._step_name(), "cc_n": comps[pick]["n"]}, \
                    comps[pick]
            if kind.startswith("activity-summary"):
                return {"kind": kind, "step": self._step_name()}, None
            sub = self._subagent(msgs)
            if sub is not None:
                i, script = sub
                n = sum(1 for m in msgs if m.get("role") == "assistant")
                if n == 1 and i not in self._labelled and self._step_name() in self.label_steps:
                    # Past the label's 30 s before the sub-agent's third call,
                    # where Claude Code's came (m1/0034).
                    from noeta.plugins.subtasks.extension import PROGRESS_EVERY

                    self._labelled.add(i)
                    self.clock_ahead += PROGRESS_EVERY + 1
                turns = script["turns"]
                return ({"kind": f"subagent:{script['agent']}", "sub": i, "call": n + 1,
                         "step": self._step_name()},
                        turns[n] if n < len(turns) else None)
            # A main-loop call carries tools; a tool-less call is a side request.
            if body.get("tools") and (kind == "main" or any(
                    _shows(t, s["say"]) for m in msgs if m.get("role") == "user"
                    for t in _texts(m) for s in self.steps)):
                step, turn = self._main_position(msgs)
                self.state = (step, turn)
                turns = self.script["steps"][step]["turns"]
                routing = {"kind": "main", "step": self.steps[step]["name"], "call": turn + 1}
                if self.steps[step].get("interrupt") and not turns:
                    routing["stall"] = True
                    return routing, {"stall": True}
                return routing, turns[turn] if turn < len(turns) else None
            routing = {"kind": kind if kind != "main" else "side", "step": self._step_name()}
            return routing, SIDE_ANSWERS.get(kind)

    def _step_name(self) -> str:
        return str(self.steps[self.current_step]["name"])

    def materialize(self, turn: Mapping[str, Any] | None, tools: list[str],
                    routing: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
        """The turn as content to stream: placeholders filled, tool calls
        given ids and noeta's names."""
        if turn is None:
            return [{"type": "text", "text": "(replay: no recorded turn here)"}], "end_turn"
        content = []
        for b in self.fill(turn.get("content") or []):
            if b["type"] == "tool_use":
                name = b["name"]
                if name not in tools and TOOL_NAMES.get(name) in tools:
                    routing.setdefault("renamed", []).append(f"{name}->{TOOL_NAMES[name]}")
                    name = TOOL_NAMES[name]
                elif name not in tools:
                    routing.setdefault("unoffered", []).append(name)
                with self.lock:
                    self.ids += 1
                    tid = f"toolu_replay_{self.ids:04d}"
                content.append({"type": "tool_use", "id": tid, "name": name,
                                "input": b["input"]})
            else:
                content.append(b)
        stop = "tool_use" if any(b["type"] == "tool_use" for b in content) else "end_turn"
        return content, stop


def _input_tokens(body: Mapping[str, Any]) -> int:
    """What the fake model reports as the request's input tokens: noeta's
    own estimate of its text (a real model reports its count, which
    compaction's threshold check starts from)."""
    def strings(value: Any) -> Iterator[str]:
        if isinstance(value, str):
            yield value
        elif isinstance(value, Mapping):
            for v in value.values():
                yield from strings(v)
        elif isinstance(value, list):
            for v in value:
                yield from strings(v)
    return sum(text_tokens(s) for k in ("system", "messages", "tools")
               for s in strings(body.get(k)))


class Endpoint:
    """The fake Messages endpoint: serves the replayer's turns, keeps every request."""

    def __init__(self, replayer: Replayer) -> None:
        self.replayer = replayer
        self.captured: list[dict[str, Any]] = []
        self.lock = threading.Lock()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def __enter__(self) -> Endpoint:
        self.thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.server.shutdown()
        self.server.server_close()

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        ep = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: Any) -> None:
                pass

            def do_POST(self) -> None:
                n = int(self.headers.get("content-length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                entry: dict[str, Any] = {"path": self.path, "headers": dict(self.headers),
                                         "body": body}
                with ep.lock:
                    entry["seq"] = len(ep.captured) + 1
                    ep.captured.append(entry)
                if "count_tokens" in self.path:
                    entry["routing"] = {"kind": "count-tokens"}
                    self._send_json({"input_tokens": 1000})
                    return
                routing, turn = ep.replayer.route(body)
                entry["routing"] = routing
                if turn is not None and turn.get("stall"):
                    self._stall()
                    return
                tools = [t.get("name") for t in body.get("tools") or []]
                content, stop = ep.replayer.materialize(turn, tools, routing)
                if not body.get("stream"):
                    self._send_json({"id": "msg_replay", "type": "message", "role": "assistant",
                                     "model": body.get("model"), "content": content,
                                     "stop_reason": stop, "usage": {"input_tokens": 0,
                                                                    "output_tokens": 1}})
                    return
                self._send_sse(wire.sse(content, stop, model=str(body.get("model")),
                                        input_tokens=_input_tokens(body)))

            def _send_json(self, data: Any) -> None:
                raw = json.dumps(data).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _send_sse(self, events: Iterator[bytes]) -> None:
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
                self.send_header("transfer-encoding", "chunked")
                self.end_headers()
                for chunk in events:
                    self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()

            def _stall(self) -> None:
                """A reply that starts and never finishes, for the interrupt step:
                a finished thinking block (empty signature, as the recorded
                gateway sent), then text that is still streaming."""
                head = list(wire.sse([{"type": "thinking", "thinking": "Planning the answer."},
                                      {"type": "text", "text": "Writing"}], None, model=MODEL))
                try:
                    self.send_response(200)
                    self.send_header("content-type", "text/event-stream")
                    self.send_header("transfer-encoding", "chunked")
                    self.end_headers()
                    # message_start, the thinking block start to stop, the
                    # text block's start and first delta.
                    for chunk in head[:7]:
                        self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                    self.wfile.flush()
                    ep.replayer.stalls.set()
                    for _ in range(600):
                        time.sleep(0.1)
                        self.wfile.write(b"5\r\n: .\n\n\r\n")  # SSE comment: keeps it open
                        self.wfile.flush()
                except OSError:
                    return

        return Handler


# ------------------------------------------------------------------ runner


class SessionReplay:
    """One session replayed in a scratch directory."""

    def __init__(self, session: str, spec: Mapping[str, Any], script: Mapping[str, Any],
                 scratch: Path, kinds: facts.Kinds) -> None:
        self.session = session
        self.spec = spec
        self.scratch = scratch
        self.notes: list[str] = []
        self.home = scratch / "home"
        self.home.mkdir(parents=True, exist_ok=True)
        self.proj = scratch / "proj"
        self.replayer = Replayer(script, spec["steps"], {}, kinds)

    def prepare(self) -> None:
        if not self.proj.exists():
            write_project(self.scratch, data=bool(self.spec.get("data")),
                          hooks=bool(self.spec.get("hooks")), python=sys.executable,
                          noeta=True)
        elif self.spec.get("data") and not (self.proj / "data.txt").exists():
            from scripts.parity.project import data_rows
            (self.proj / "data.txt").write_text(data_rows(), "utf-8")
        # A recorded path to one of Claude Code's project files names the
        # same file in noeta's place (the project was written that way).
        places = {f"{{PROJECT}}/{theirs}": f"{self.proj}/{ours}"
                  for theirs, ours in NOETA_PLACES}
        self.replayer.subs = {**places, "{PROJECT}": str(self.proj), "{HOME}": str(self.home),
                              "{USER}": getpass.getuser()}
        # Claude Code was recording in a project its trust dialog accepted,
        # with the project's MCP server consented to for that project.
        from noeta.cli.config import trust_project
        trust_project(str(self.proj))
        (self.proj / ".noeta").mkdir(exist_ok=True)
        (self.proj / ".noeta" / "settings.local.json").write_text(
            json.dumps({"enabledMcpjsonServers": ["echo"]}), "utf-8")
        # Kept out of git status, as noeta keeps the file it writes (Claude
        # Code kept its consent outside the project): the first request's git
        # snapshot then reads as Claude Code's.
        exclude = self.proj / ".git" / "info" / "exclude"
        if exclude.parent.is_dir():
            with exclude.open("a", encoding="utf-8") as f:
                f.write("/.noeta/settings.local.json\n")

    async def drive(self, endpoint: Endpoint, task_id: str | None) -> str | None:
        from noeta.cli.config import Settings, from_settings
        from noeta.core.commands import expand_command
        from noeta.core.extension import CommandContext
        from noeta.toolkit.local_env import local_timezone

        # The session's environment knobs (Claude Code's compaction window
        # and percentage) reach noeta's plugins under noeta's names.
        for key, value in (self.spec.get("env") or {}).items():
            os.environ[NOETA_ENV.get(key, key)] = str(value)
        settings = Settings(model=MODEL, provider="anthropic", base_url=endpoint.url,
                            api_key="replay", store=f"sqlite:///{self.scratch / 'tasks.db'}")
        app = from_settings(settings, cwd=str(self.proj), own_tasks_only=True,
                            plugins=self.spec.get("noeta_plugins") or {})
        for notice in app.notices:
            self.notes.append(f"noeta notice: {notice}")
        main = app.runtime.agents["main"]
        from noeta.plugins.subtasks import extension as subtasks_ext

        subtasks_ext._now = lambda: time.time() + self.replayer.clock_ahead
        async with app:
            if task_id is not None:
                from noeta.toolkit.signals import RESUMED_SIGNAL

                await app.adopt(task_id)
                await app.client.signal(task_id, RESUMED_SIGNAL)
            for i, step in enumerate(self.spec["steps"]):
                self.replayer.current_step = i
                text = step["say"]
                if text.startswith("/"):
                    ctx = CommandContext(cwd=str(self.proj), task_id=task_id)
                    expanded = await expand_command(main, ctx, text)
                    if expanded is None:
                        self.notes.append(f"step {step['name']}: noeta has no command "
                                          f"{text.split()[0]}; sent as a plain message")
                    else:
                        text = expanded
                after = len(await app.client.events(task_id)) if task_id else 0
                if task_id is None:
                    config = {"cwd": str(self.proj), "model": MODEL}
                    # As the TUI does: the person's dates are in this machine's zone.
                    if zone := local_timezone():
                        config["timezone"] = zone
                    if self.spec.get("permission_mode"):
                        # The mode Claude Code was started in (``--permission-mode``).
                        config["permission_mode"] = self.spec["permission_mode"]
                    task_id = await app.client.create("main", text, config=config)
                else:
                    await app.client.send(task_id, text)
                await self._settle(app, task_id, step, after)
        return task_id

    async def _settle(self, app: Any, task_id: str, step: Mapping[str, Any],
                      after: int) -> None:
        """Until the turn the step started is over: the task waits for input
        again after taking the step's message (or has ended). Approvals are
        answered as the recording's driver answered Claude Code's cards (the
        step's ``cards`` keys in order: ``Escape`` denies, ``2`` allows
        always, anything else allows), questions get their first option."""
        from noeta.core.events import InputTaken, Waiting
        from noeta.host.pending import pending_tree

        deadline = time.monotonic() + STEP_TIMEOUT_S
        self.replayer.stalls.clear()
        keys = iter(step.get("cards") or [])
        while time.monotonic() < deadline:
            await asyncio.sleep(0.2)
            if step.get("interrupt") and self.replayer.stalls.is_set():
                self.replayer.stalls.clear()
                await app.client.cancel(task_id, "interrupted", turn_only=True)
            for p in await pending_tree(app.client, task_id):
                if p.kind == "approval":
                    key = next(keys, None)
                    if key is None:  # Claude Code showed no card here
                        key = "1"
                        self.notes.append(f"step {step['name']}: approval asked for {p.tool}")
                    await app.client.answer(p.task_id, p.request_id,
                                            "deny" if key == "Escape" else "allow",
                                            always=key == "2")
                else:
                    qs = p.detail.get("questions") or []
                    data = {str(q.get("id")): {"selected": [q["options"][0]["label"]]}
                            for q in qs if q.get("options")}
                    await app.client.answer(p.task_id, p.request_id, data=data)
            recorded = await app.client.events(task_id)
            plan = _plan_path(recorded)
            if plan:
                self.replayer.plan_path = plan
            self.replayer.output_files = _output_files(recorded)
            row = await app.client.get(task_id)
            if row.status == "ended":
                self.notes.append(f"step {step['name']}: task ended ({row.end_status})")
                return
            events = [r.event for r in await app.client.events(task_id, after)]
            taken = [i for i, e in enumerate(events) if isinstance(e, InputTaken)]
            if taken and row.status == "waiting" and row.waiting_on == "input" and any(
                    isinstance(e, Waiting) and e.on == "input" for e in events[taken[0]:]) \
                    and row.inbox_read_seq >= row.inbox_seq and await _agents_done(app, task_id) \
                    and not (step.get("idle") and _jobs_running(task_id)):
                return
        self.notes.append(f"step {step['name']}: no settle within {STEP_TIMEOUT_S:.0f}s; "
                          "turn cancelled")
        await app.client.cancel(task_id, "replay timeout", turn_only=True)


def _jobs_running(root_id: str) -> bool:
    """A background job of the tree still runs: a step with ``idle`` is one
    whose recording waited for the session to go quiet, its jobs' end notices
    included."""
    from noeta.plugins.shell.jobs import REGISTRY

    return bool(REGISTRY.running(root_id))


async def _agents_done(app: Any, task_id: str) -> bool:
    """Every background agent below the task has ended, as Claude Code's
    driver waited for the session to go quiet before the next step."""
    from noeta.host.pending import tree_busy

    return not await tree_busy(app.client, task_id)


def write_models_file(path: Path) -> None:
    path.write_text(json.dumps({"models": [MODEL_ROW]}), "utf-8")


def capture_facts(entries: list[dict[str, Any]], kinds: facts.Kinds,
                  toolsets: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for e in entries:
        body = e.get("body") or {}
        row = {"seq": e["seq"], **(e.get("routing") or {})}
        row.update(facts.request_facts(body, e.get("headers") or {}, kinds, toolsets))
        out.append(row)
    return out


#: The page the `web` step fetches, as https://example.com serves it.
EXAMPLE_PAGE = (
    '<!doctype html><html lang=en><head><meta charset=utf-8><link rel=icon href=data:,>'
    '<meta name=viewport content="width=device-width,initial-scale=1"><title>Example Domain</title>'
    '<style>html{color-scheme:light dark;background:light-dark(#eee,#222)}body{font:16px/1.6 '
    'system-ui,sans-serif;max-width:26em;margin:auto;padding:25vh 2em 2em;text-align:center}'
    '</style></head><body><p>This domain is for use in documentation examples without needing '
    'permission. This is not a service; avoid relying on it for testing and monitoring purposes.'
    '</p><script src=/s.js></script></body></html>')


def _serve_pages_offline() -> None:
    """Answer WebFetch from :data:`EXAMPLE_PAGE`: a network failure made the
    replay skip the digest request Claude Code's recording has."""
    from noeta.plugins.web.fetch import HttpFetchTransport
    from noeta.plugins.web.render import FetchedPage

    async def fetch(self: HttpFetchTransport, url: str) -> FetchedPage:
        if url.rstrip("/") != "https://example.com":
            raise OSError(f"parity replay serves only https://example.com, not {url}")
        return FetchedPage(EXAMPLE_PAGE, "text/html")

    HttpFetchTransport.fetch = fetch  # type: ignore[method-assign]


def replay(version: str, sessions: list[str] | None, out: Path, keep: bool = False
           ) -> dict[str, Any]:
    spec = json.loads((HERE / "scenarios.json").read_text("utf-8"))["sessions"]
    contract_dir = ROOT / "parity" / version
    scratch_root = Path(tempfile.mkdtemp(prefix="noeta-parity-"))
    os.environ["NOETA_HOME"] = str(scratch_root / "noeta-home")
    # Claude Code offers WebSearch through its API; noeta needs a search key
    # to offer it. No scenario searches, so a placeholder only lists it.
    os.environ.setdefault("NOETA_WEB_SEARCH_API_KEY", "parity-placeholder")
    _serve_pages_offline()
    (scratch_root / "noeta-home").mkdir(mode=0o700)  # an open home draws a notice
    write_models_file(scratch_root / "models.json")
    from noeta.cli.config import register_models_file
    register_models_file(str(scratch_root / "models.json"))
    names = facts.load_names()
    out.mkdir(parents=True, exist_ok=True)
    results: dict[str, Any] = {}
    task_ids: dict[str, str | None] = {}
    toolsets: dict[str, Any] = {}
    order = [s for s in spec if not sessions or s in sessions]
    for name in order:
        sess = spec[name]
        base = sess.get("resume")
        if base and base not in task_ids:
            results[name] = {"skipped": f"resumes {base}, which was not replayed in this run"}
            continue
        script = json.loads((contract_dir / "script" / f"{name}.json").read_text("utf-8"))
        kinds = facts.Kinds(names, prompts=session_prompts(spec, name))
        scratch = scratch_root / (base or name)
        run = SessionReplay(name, sess, script, scratch, kinds)
        cc_facts = contract_dir / f"{name}.json"
        if cc_facts.exists():
            run.replayer.label_steps = {
                str(r.get("step")) for r in json.loads(cc_facts.read_text("utf-8"))["requests"]
                if str(r.get("kind", "")).startswith("activity-summary")}
        run.prepare()
        subs = [*script.get("subagents", [])]
        if base:
            base_script = contract_dir / "script" / f"{base}.json"
            subs += json.loads(base_script.read_text("utf-8")).get("subagents", [])
        kinds.tasks = {run.replayer.fill(s["prompt"])[:120] for s in subs}
        saved = {k: os.environ.get(k) for k in (NOETA_ENV.get(k, k) for k in sess.get("env") or {})}
        try:
            with Endpoint(run.replayer) as ep:
                task_ids[name] = asyncio.run(run.drive(ep, task_ids.get(base) if base else None))
                entries = list(ep.captured)
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        raw_dir = out / "raw" / name
        shutil.rmtree(raw_dir, ignore_errors=True)
        raw_dir.mkdir(parents=True)
        for e in entries:
            (raw_dir / f"{e['seq']:04d}.json").write_text(
                json.dumps(e, indent=1, ensure_ascii=False), "utf-8")
        rows = capture_facts(entries, kinds, toolsets)
        results[name] = {"requests": rows, "notes": run.notes}
        (out / f"{name}.json").write_text(json.dumps(results[name], indent=1), "utf-8")
    (out / "tools.json").write_text(json.dumps(toolsets, indent=1, sort_keys=True), "utf-8")
    if not keep:
        shutil.rmtree(scratch_root, ignore_errors=True)
    return {"sessions": results, "toolsets": toolsets}


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Replay Claude Code scenarios against noeta.")
    ap.add_argument("--version", default="2.1.312")
    ap.add_argument("--session", action="append", help="only these sessions")
    ap.add_argument("--out", type=Path, default=ROOT / "parity" / "out")
    ap.add_argument("--report", type=Path, default=ROOT / "parity" / "report.md")
    ap.add_argument("--keep", action="store_true", help="keep the scratch directory")
    args = ap.parse_args(argv)
    noeta = replay(args.version, args.session, args.out, keep=args.keep)
    report, count = compare.compare_all(ROOT / "parity" / args.version, noeta)
    args.report.write_text(report, "utf-8")
    print(report.split("\n## ", 1)[0])
    print(f"full report: {args.report}")
    sys.exit(1 if count else 0)


if __name__ == "__main__":
    main()
