"""Distill a Claude Code recording into the checked-in parity contract.

    python -m scripts.parity.distill ~/cc-recordings/2.1.312 [--out parity/2.1.312]

Reads ``<recording>/<session>/*.json`` for each session of ``scenarios.json``
and writes, under ``--out``:

* ``<session>.json`` — every request: kind (main loop, sub-agent, fork,
  side request), scenario step, call number within the step, and its facts
  (``facts.request_facts``): parameters, beta headers, system blocks, tool set
  id, message layout. Facts only, never Claude Code's text.
* ``tools.json`` — the tool sets by id: names, order, deferral, schema shapes.
* ``texts.json`` — per text kind, the length and hash of each distinct text,
  so a later recording shows what changed.
* ``script/<session>.json`` — the model's side of the session (text and tool
  calls per main-loop step, per sub-agent, per compaction), which replay
  plays back to noeta. Recorded paths become ``{PROJECT}`` / ``{HOME}``.
"""

from __future__ import annotations

import argparse
import json
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from scripts.parity import facts, wire

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
_ENV_CWD = re.compile(r"Primary working directory: (\S+)")
_HOME = re.compile(r"^(/Users/[^/]+|/home/[^/]+)")


def scenarios() -> dict[str, Any]:
    return json.loads((HERE / "scenarios.json").read_text("utf-8"))


def step_marker(text: str, say: str) -> bool:
    """``text`` shows the user typed ``say`` (a slash command shows as tags)."""
    if say.startswith("/"):
        name, _, args = say[1:].partition(" ")
        return (f"<command-name>/{name}</command-name>" in text
                and (not args or f"<command-args>{args}</command-args>" in text))
    return facts.is_prompt(text, say) or say in text


def find_step(texts: list[str], steps: list[dict[str, Any]], kinds: facts.Kinds) -> int:
    """The latest step whose prompt shows in ``texts`` (a compaction summary,
    which may quote every prompt, excluded); -1 when none does."""
    found = -1
    for t in texts:
        if kinds.name(t.split("\n\n", 1)[0]) == "compact-summary":
            continue
        for i, s in enumerate(steps):
            if i > found and step_marker(t, s["say"]):
                found = i
    return found


class Paths:
    """Rewrites the recording machine's paths and user name into placeholders."""

    def __init__(self) -> None:
        self.project: str | None = None
        self.home: str | None = None
        self.user: re.Pattern[str] | None = None

    def learn(self, body: Mapping[str, Any]) -> None:
        if self.project:
            return
        for t in wire.user_texts(body):
            m = _ENV_CWD.search(t)
            if m:
                self.project = m.group(1)
                h = _HOME.match(self.project)
                self.home = h.group(1) if h else None
                if self.home:
                    self.user = re.compile(rf"\b{re.escape(self.home.rsplit('/', 1)[1])}\b")
                return

    def scrub(self, value: Any) -> Any:
        if isinstance(value, str):
            if self.project:
                value = value.replace(self.project, "{PROJECT}")
            if self.home:
                value = value.replace(self.home, "{HOME}")
            if self.user:
                value = self.user.sub("{USER}", value)
            return value
        if isinstance(value, list):
            return [self.scrub(v) for v in value]
        if isinstance(value, dict):
            return {k: self.scrub(v) for k, v in value.items()}
        return value


def load_session(rec_dir: Path) -> list[tuple[int, dict[str, Any]]]:
    out = []
    for f in sorted(rec_dir.glob("*.json")):
        m = re.match(r"(\d+)-", f.name)
        if m:
            out.append((int(m.group(1)), json.loads(f.read_text("utf-8"))))
    return out


def _catalog_add(catalog: dict[str, Any], found: dict[str, dict[str, int]]) -> None:
    for kind, shas in found.items():
        row = catalog.setdefault(kind, {"shas": {}, "varies": False})
        if row["varies"]:
            continue
        row["shas"].update(shas)
        if len(row["shas"]) > 4:  # a text with run-specific detail: keep only its size range
            lens = list(row["shas"].values())
            catalog[kind] = {"varies": True, "len": [min(lens), max(lens)]}


def read_cards(rec_dir: Path) -> list[dict[str, str]]:
    """The cards the driver answered (``drive.py`` saves each to
    ``<session>.cards/``): step and key only, not the card's text."""
    out = []
    for f in sorted(rec_dir.with_name(rec_dir.name + ".cards").glob("*.txt")):
        m = re.match(r"# step (\S+); answered with (\S+)", f.read_text("utf-8"))
        if m:
            out.append({"step": m.group(1), "key": m.group(2)})
    return out


def session_prompts(sessions: Mapping[str, Any], name: str) -> list[str]:
    """What the user typed in a session, and in the session it resumes."""
    sess = sessions[name]
    base = sessions.get(sess.get("resume") or "", {})
    return [s["say"] for s in [*base.get("steps", []), *sess["steps"]]]


def task_prompts(records: Iterable[Mapping[str, Any]]) -> list[str]:
    """The prompts the model wrote for sub-agents (``Agent`` / ``Task`` calls)."""
    out = []
    for rec in records:
        for m in (rec.get("body") or {}).get("messages") or []:
            for b in m.get("content") if isinstance(m.get("content"), list) else []:
                if b.get("type") == "tool_use" and b.get("name") in ("Agent", "Task"):
                    p = (b.get("input") or {}).get("prompt")
                    if isinstance(p, str):
                        out.append(p)
    return out


def distill_session(name: str, spec: Mapping[str, Any], rec_dir: Path,
                    toolsets: dict[str, Any], catalog: dict[str, Any],
                    prompts: list[str] | None = None
                    ) -> tuple[dict[str, Any], dict[str, Any]]:
    steps = spec["steps"]
    recorded = load_session(rec_dir)
    kinds = facts.Kinds(prompts=prompts or [s["say"] for s in steps],
                        tasks=task_prompts(rec for _, rec in recorded))
    paths = Paths()
    requests: list[dict[str, Any]] = []
    script_steps: list[dict[str, Any]] = [{"name": s["name"], "say": s["say"], "turns": []}
                                          for s in steps]
    subagents: dict[str, dict[str, Any]] = {}
    compactions: list[dict[str, Any]] = []
    step = 0
    calls: dict[str, int] = {}
    for n, rec in recorded:
        body = rec.get("body") or {}
        path = rec.get("path", "")
        paths.learn(body)
        kind = wire.request_kind(path, body, kinds)
        found = find_step(wire.user_texts(body), steps, kinds)
        if kind == "main":
            step = max(step, found)
        elif kind == "compaction" and step + 1 < len(steps) \
                and steps[step + 1]["say"] == "/compact":
            step += 1
        step = max(step, 0)
        # A side request belongs to the latest step it shows (a title names a
        # prompt before its first main-loop call) without moving the loop on.
        here = step if kind == "main" else max(step, found)
        sname = steps[here]["name"]
        ok = rec.get("status") == 200 and not rec.get("interrupted")
        row: dict[str, Any] = {"n": n, "kind": kind, "step": sname, "status": rec.get("status")}
        if rec.get("interrupted"):
            row["interrupted"] = True
        if kind == "main" and ok:
            calls[sname] = calls.get(sname, 0) + 1
            row["call"] = calls[sname]
        row.update(facts.request_facts(body, rec.get("headers") or {}, kinds, toolsets))
        requests.append(row)
        found_texts: dict[str, dict[str, int]] = {}
        facts.text_catalog(body, kinds, found_texts)
        _catalog_add(catalog, found_texts)

        msg = wire.parse_response(rec.get("response") or "") if ok else None
        if kind == "auto-mode-classifier":
            said = "".join(b.get("text", "") for b in (msg or {}).get("content") or [])
            row["verdict"] = ("block" if "<block>yes" in said else
                              "allow" if "<block>no" in said else None)
        if msg is None:
            continue
        turn = paths.scrub(wire.script_turn(msg))
        turn["n"] = n
        if kind == "main":
            script_steps[here]["turns"].append(turn)
        elif kind.startswith("subagent:"):
            opening = next((t for t in wire.user_texts(body)
                            if not t.lstrip().startswith("<")), "")
            key = paths.scrub(opening.strip())
            sub = subagents.setdefault(key, {"agent": kind.split(":", 1)[1],
                                             "prompt": key, "turns": []})
            sub["turns"].append(turn)
        elif kind == "compaction":
            compactions.append({"step": sname, **turn})
    contract = {"session": name, "about": spec.get("about", ""), "requests": requests,
                "cards": read_cards(rec_dir)}
    missing = [s["name"] for s in script_steps if not s["turns"] and s["say"] != "/compact"
               and not any(r["step"] == s["name"] and r["kind"] == "main" for r in requests)]
    if missing:
        contract["steps_without_main_calls"] = missing
        print(f"distill: {name}: no main-loop call recorded for step(s) {', '.join(missing)}")
    script = {"session": name, "steps": script_steps, "subagents": list(subagents.values()),
              "compactions": compactions}
    return contract, script


def distill(rec_root: Path, out: Path, sessions: list[str] | None = None) -> list[Path]:
    spec = scenarios()
    out.mkdir(parents=True, exist_ok=True)
    (out / "script").mkdir(exist_ok=True)
    toolsets: dict[str, Any] = {}
    catalog: dict[str, Any] = {}
    written = []
    for name, sess in spec["sessions"].items():
        if sessions and name not in sessions:
            continue
        rec_dir = rec_root / name
        if not rec_dir.is_dir():
            print(f"distill: no recording for {name} in {rec_root}")
            continue
        contract, script = distill_session(name, sess, rec_dir, toolsets, catalog,
                                           session_prompts(spec["sessions"], name))
        for path, data in ((out / f"{name}.json", contract),
                           (out / "script" / f"{name}.json", script)):
            path.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n", "utf-8")
            written.append(path)
    for path, data in ((out / "tools.json", toolsets), (out / "texts.json", catalog)):
        path.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n", "utf-8")
        written.append(path)
    return written


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Distill a Claude Code recording into parity/.")
    ap.add_argument("recording", type=Path, help="e.g. ~/cc-recordings/2.1.312")
    ap.add_argument("--out", type=Path, help="default: parity/<recording dir name>")
    ap.add_argument("--session", action="append", help="only these sessions")
    args = ap.parse_args(argv)
    out = args.out or ROOT / "parity" / args.recording.name
    for p in distill(args.recording.expanduser(), out, args.session):
        print(p.relative_to(ROOT) if p.is_relative_to(ROOT) else p)


if __name__ == "__main__":
    main()
