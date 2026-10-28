"""Compare noeta's requests with the Claude Code contract and write the report.

Pairs each of noeta's main-loop calls with Claude Code's call at the same
scenario step and position, each sub-agent's and compaction's first call
with Claude Code's, and lists every difference in the facts
(``facts.request_facts``): parameters, beta headers, system blocks, tools and
their schemas, text kinds, roles, cache-mark positions, the side requests
Claude Code makes that noeta does not, and what the replay itself had to
bend (tool renames, tools noeta does not offer, steps with no recorded turn).

A difference seen in many requests is one mismatch with a count; the report
lists each where it first appears. A mismatch whose id appears in
``parity/deviations.md`` is an accepted deviation and is not counted.

    python -m scripts.parity.compare [--version 2.1.312] [--out parity/out]
"""

from __future__ import annotations

import argparse
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from scripts.parity.facts import sha
from scripts.parity.wire import TOOL_NAMES

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]

#: Report order and weight for ranking: what the model sees first.
CATEGORIES = {
    "tools": 6, "params": 5, "system": 5, "side-requests": 4, "headers": 4,
    "context": 3, "cache": 3, "layout": 2, "compaction": 3, "subagent": 3, "run": 1,
}
_SKIP_SCHEMA_KEYS = {"description", "title", "properties", "items"}


@dataclass
class Mismatch:
    category: str
    text: str
    where: list[str] = field(default_factory=list)

    @property
    def id(self) -> str:
        return sha(f"{self.category}\0{self.text}", 8)


class Findings:
    def __init__(self) -> None:
        self.items: dict[tuple[str, str], Mismatch] = {}

    def add(self, category: str, text: str, where: str) -> None:
        m = self.items.setdefault((category, text), Mismatch(category, text))
        m.where.append(where)

    def ranked(self) -> list[Mismatch]:
        return sorted(self.items.values(),
                      key=lambda m: (-severity(m), -len(m.where), m.text))


#: Within a category, what makes a difference matter more (first match wins).
_SEVERITY = [
    ("deferred tools noeta does not offer", 2), ("tools noeta does not offer", 4),
    ("never did", 4), ("auto-mode-classifier", 4), ("fields noeta does not send", 4),
    ("system blocks", 3), ("role system", 3), ("parameters missing in noeta", 3),
    ("only noeta offers", 3), ("no command", 2), ("interrupted", 2),
    ("Claude Code sends, noeta does not", 2), ("max_tokens", 2),
    ("parameters only noeta has", 2), ("$schema", -2), ("additionalProperties", -2),
    (" default:", -1),
]


def severity(m: Mismatch) -> int:
    bonus = next((v for k, v in _SEVERITY if k in m.text), 0)
    return CATEGORIES.get(m.category, 0) + bonus


def top(items: list[Mismatch], n: int, per_category: int = 4) -> list[Mismatch]:
    """The ``n`` most important, at most ``per_category`` from one category,
    a difference shown once even when sub-agents and compaction repeat it."""
    out: list[Mismatch] = []
    taken: dict[str, int] = {}
    texts: set[str] = set()
    for m in items:
        text = re.sub(r"^(subagent:[\w-]+|compaction): ", "", m.text)
        if text in texts:
            continue
        texts.add(text)
        if taken.get(m.category, 0) < per_category:
            out.append(m)
            taken[m.category] = taken.get(m.category, 0) + 1
        if len(out) == n:
            break
    return out


def _short(value: Any, n: int = 90) -> str:
    s = json.dumps(value, sort_keys=True) if not isinstance(value, str) else value
    return s if len(s) <= n else s[: n - 1] + "…"


# ------------------------------------------------------------------ pieces


def diff_params(cc: Mapping[str, Any], no: Mapping[str, Any]) -> list[str]:
    out = []
    missing = sorted(set(cc["keys"]) - set(no["keys"]))
    extra = sorted(set(no["keys"]) - set(cc["keys"]))
    if missing:
        out.append(f"top-level fields noeta does not send: {', '.join(missing)}")
    if extra:
        out.append(f"top-level fields only noeta sends: {', '.join(extra)}")
    for k in sorted(set(cc) | set(no)):
        if k == "keys" or k in missing or k in extra:
            continue
        if cc.get(k) != no.get(k):
            out.append(f"{k}: Claude Code {_short(cc.get(k))}, noeta {_short(no.get(k))}")
    return out


def diff_headers(cc: Mapping[str, Any], no: Mapping[str, Any]) -> list[str]:
    out = []
    missing = sorted(set(cc["betas"]) - set(no["betas"]))
    extra = sorted(set(no["betas"]) - set(cc["betas"]))
    if missing:
        out.append(f"anthropic-beta noeta does not send: {', '.join(missing)}")
    if extra:
        out.append(f"anthropic-beta only noeta sends: {', '.join(extra)}")
    if cc.get("anthropic-version") != no.get("anthropic-version"):
        out.append(f"anthropic-version: Claude Code {cc.get('anthropic-version')}, "
                   f"noeta {no.get('anthropic-version')}")
    return out


def _sys_line(blocks: list[Mapping[str, Any]]) -> str:
    return "[" + ", ".join(f"{b['kind']}{b.get('cache', '')}" for b in blocks) + "]"


def diff_system(cc: list[Mapping[str, Any]], no: list[Mapping[str, Any]]) -> list[str]:
    a, b = _sys_line(cc), _sys_line(no)
    return [] if a == b else [f"system blocks: Claude Code {a}, noeta {b}"]


def schema_diff(a: Any, b: Any, path: str) -> list[str]:
    if not isinstance(a, Mapping) or not isinstance(b, Mapping):
        return [] if a == b else [f"{path}: Claude Code {_short(a)}, noeta {_short(b)}"]
    out = []
    for k in sorted((set(a) | set(b)) - _SKIP_SCHEMA_KEYS):
        if k == "additionalProperties" and isinstance(a.get(k), Mapping) \
                and isinstance(b.get(k), Mapping):
            out += schema_diff(a[k], b[k], f"{path}{{*}}")  # a schema: our own wording
        elif a.get(k) != b.get(k):
            out.append(f"{path} {k}: Claude Code {_short(a.get(k))}, noeta {_short(b.get(k))}")
    pa, pb = a.get("properties") or {}, b.get("properties") or {}
    missing = [k for k in pa if k not in pb]
    extra = [k for k in pb if k not in pa]
    if missing:
        out.append(f"{path}: parameters missing in noeta: {', '.join(missing)}")
    if extra:
        out.append(f"{path}: parameters only noeta has: {', '.join(extra)}")
    for k in pa:
        if k in pb:
            out += schema_diff(pa[k], pb[k], f"{path}.{k}")
    if "items" in a or "items" in b:
        out += schema_diff(a.get("items"), b.get("items"), f"{path}[]")
    return out


def diff_tools(cc: list[Mapping[str, Any]], no: list[Mapping[str, Any]],
               renames: Mapping[str, str]) -> list[str]:
    out = []
    by_cc = {t["name"]: t for t in cc}
    by_no = {t["name"]: t for t in no}
    alias = {renames.get(n, n): n for n in by_cc}  # noeta name -> Claude Code name
    absent = [n for n in by_cc if renames.get(n, n) not in by_no]
    loaded = [n for n in absent if not by_cc[n].get("defer_loading")]
    deferred = [n for n in absent if by_cc[n].get("defer_loading")]
    if loaded:
        out.append(f"tools noeta does not offer: {', '.join(loaded)}")
    if deferred:
        out.append(f"deferred tools noeta does not offer: {', '.join(deferred)}")
    extra = [n for n in by_no if n not in alias]
    if extra:
        out.append(f"tools only noeta offers: {', '.join(extra)}")
    for n in by_cc:
        if renames.get(n, n) != n and renames[n] in by_no:
            out.append(f"tool {n} is called {renames[n]} in noeta")
    common_cc = [n for n in by_cc if renames.get(n, n) in by_no]
    common_no = [n for n in by_no if n in alias]
    if [renames.get(n, n) for n in common_cc] != common_no:
        out.append("tool order differs: Claude Code " + ", ".join(common_cc)
                   + " / noeta " + ", ".join(common_no))
    # The same schema difference on several tools is one line naming them all.
    shared: dict[str, list[str]] = {}
    for n in common_cc:
        a, b = by_cc[n], by_no[renames.get(n, n)]
        for k in ("defer_loading", "cache_control", "strict", "type"):
            if a.get(k) != b.get(k):
                shared.setdefault(f" {k}: Claude Code {_short(a.get(k))}, "
                                  f"noeta {_short(b.get(k))}", []).append(n)
        for d in schema_diff(a["input_schema"], b["input_schema"], ""):
            shared.setdefault(d, []).append(n)
    for rest, names in shared.items():
        out.append(f"tools {', '.join(names)}:{rest}" if len(names) > 1 and rest[0] == " "
                   else f"tool {names[0]}{rest}" if len(names) == 1
                   else f"tools {', '.join(names)}: {rest.lstrip('.')}")
    return out


def kinds_of(layout: Iterable[str]) -> set[str]:
    """Text kinds a request carries (prompts and model text left out)."""
    out = set()
    for line in layout:
        for tok in line.split(": ", 1)[1].split(" | ") if ": " in line else []:
            tok = tok.split("*", 1)[0]
            if tok.startswith("text:"):
                for part in tok[5:].split("+"):
                    if part != "prompt":
                        out.add(part)
    return out


def cache_marks(sys: list[Mapping[str, Any]], layout: list[str]) -> list[str]:
    """Where the cache marks sit: ``system[i]`` and ``message[-k]`` counted
    from the end (``.inner`` when not on the message's last block), with the
    TTL when one is set."""
    out = [f"system[{i}]{b['cache'][1:]}" for i, b in enumerate(sys) if b.get("cache")]
    n = len(layout)
    for i, line in enumerate(layout):
        toks = line.split(": ", 1)[1].split(" | ") if ": " in line else []
        for j, tok in enumerate(toks):
            if "*" in tok:
                out.append(f"message[-{n - i}]{'' if j == len(toks) - 1 else '.inner'}"
                           + tok.split("*", 1)[1])
    return out


def roles(layout: list[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for line in layout:
        r = line.split(":", 1)[0]
        out[r] = out.get(r, 0) + 1
    return out


def diff_layout(cc: Mapping[str, Any], no: Mapping[str, Any]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    kc, kn = kinds_of(cc["messages"]), kinds_of(no["messages"])
    for k in sorted(kc - kn):
        out.append(("context", f"text kind Claude Code sends, noeta does not: {k}"))
    for k in sorted(kn - kc):
        out.append(("context", f"text kind only noeta sends: {k}"))
    rc, rn = roles(cc["messages"]), roles(no["messages"])
    for r in sorted(set(rc) | set(rn)):
        if r.startswith(("system", "user(str)", "assistant(str)")) and \
                bool(rc.get(r)) != bool(rn.get(r)):
            out.append(("layout", f"messages with role {r}: Claude Code "
                                  f"{'uses' if rc.get(r) else 'never uses'} them, noeta "
                                  f"{'does' if rn.get(r) else 'does not'}"))
    mc = cache_marks(cc["system"], cc["messages"])
    mn = cache_marks(no["system"], no["messages"])
    if mc != mn:
        out.append(("cache", f"cache marks: Claude Code {mc}, noeta {mn}"))
    if cc["messages"] and no["messages"]:
        a, b = cc["messages"][0], no["messages"][0]
        a, b = a.replace("*", ""), b.replace("*", "")
        if a != b:
            out.append(("layout", f"first message: Claude Code `{a}`, noeta `{b}`"))
    return out


# ------------------------------------------------------------------ driver


def _by(rows: Iterable[Mapping[str, Any]], kind: str) -> dict[tuple[str, int], Mapping[str, Any]]:
    return {(r["step"], r["call"]): r for r in rows
            if r.get("kind") == kind and r.get("call") and r.get("status", 200) == 200}


def _number_interrupted(rows: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """Claude Code's main-loop request the person interrupted gets the call
    number it had in its step, as noeta's stalled request has one, so the
    two are compared."""
    out: list[Mapping[str, Any]] = []
    calls: dict[str, int] = {}
    for r in rows:
        if r.get("kind") == "main" and r.get("call"):
            calls[r["step"]] = max(calls.get(r["step"], 0), r["call"])
        elif r.get("kind") == "main" and r.get("interrupted"):
            calls[r["step"]] = calls.get(r["step"], 0) + 1
            r = {**r, "call": calls[r["step"]]}
        out.append(r)
    return out


def compare_session(name: str, cc: Mapping[str, Any], no: Mapping[str, Any],
                    cc_tools: Mapping[str, Any], no_tools: Mapping[str, Any],
                    found: Findings) -> None:
    cc_rows = _number_interrupted(cc["requests"])
    no_rows = no["requests"]
    renames = TOOL_NAMES
    for r in no_rows:
        for pair in r.get("renamed") or []:
            a, b = pair.split("->")
            found.add("run", f"replay renamed Claude Code tool {a} to noeta's {b}",
                      f"{name}/{r['step']}#{r.get('call')}")
        for t in r.get("unoffered") or []:
            found.add("run", f"the recorded model called {t}, which noeta does not offer",
                      f"{name}/{r['step']}#{r.get('call')}")
    for note in no.get("notes") or []:
        step, _, text = note.partition(": ")
        found.add("run", text, f"{name}/{step.removeprefix('step ')}")

    # tools: every tool each side offered over the session, per conversation kind
    groups = ["main", *sorted({r["kind"] for r in cc_rows if r["kind"].startswith("subagent:")})]
    for kind in groups:
        a_rows = [r for r in cc_rows if r["kind"] == kind]
        b_rows = [r for r in no_rows if r.get("kind") == kind]
        if a_rows and b_rows:
            prefix = "" if kind == "main" else f"{kind}: "
            for d in diff_tools(union_tools(a_rows, cc_tools), union_tools(b_rows, no_tools),
                                renames):
                found.add("tools", prefix + d, f"{name}/{a_rows[0]['step']}")

    # main loop, call by call
    cc_main, no_main = _by(cc_rows, "main"), _by(no_rows, "main")
    for key in sorted(set(cc_main) | set(no_main),
                      key=lambda k: (_step_order(cc, k[0]), k[1])):
        where = f"{name}/{key[0]}#{key[1]}"
        a, b = cc_main.get(key), no_main.get(key)
        if a is None:
            assert b is not None
            found.add("run", "noeta made a main-loop call Claude Code did not make "
                             "(no recorded turn to answer with)", where)
            continue
        if b is None:
            found.add("run", "Claude Code made a main-loop call noeta did not make", where)
            continue
        _compare_pair(a, b, where, found)

    # sub-agents: call by call, in order
    for kind in sorted({r["kind"] for r in cc_rows if r["kind"].startswith("subagent:")}):
        a_calls = [r for r in cc_rows if r["kind"] == kind and r.get("status", 200) == 200]
        b_calls = [r for r in no_rows if r.get("kind") == kind]
        if not b_calls:
            found.add("subagent", f"{kind}: noeta made no such sub-agent call",
                      f"{name}/{a_calls[0]['step']}")
            continue
        for i, (a, b) in enumerate(zip(a_calls, b_calls, strict=False)):
            where = f"{name}/{a['step']}" + (f"#{i + 1}" if i else "")
            _compare_pair(a, b, where, found, prefix=f"{kind}: ")
        if len(a_calls) != len(b_calls):
            found.add("subagent", f"{kind}: Claude Code made {len(a_calls)} calls, noeta "
                                  f"{len(b_calls)}", f"{name}/{a_calls[0]['step']}")

    # compaction
    cc_comp = [r for r in cc_rows if r["kind"] == "compaction" and r.get("status") == 200]
    no_comp = [r for r in no_rows if str(r.get("kind", "")).startswith("compaction")]
    if cc_comp and not no_comp:
        found.add("compaction", f"Claude Code compacted ({len(cc_comp)} requests), noeta "
                                "never did", f"{name}/{cc_comp[0]['step']}")
    elif cc_comp and no_comp:
        _compare_pair(cc_comp[0], no_comp[0], f"{name}/{no_comp[0]['step']}", found,
                      prefix="compaction: ")

    # side requests
    side = {r["kind"] for r in cc_rows
            if r["kind"] not in ("main", "compaction") and not r["kind"].startswith("subagent:")}
    no_side = {str(r.get("kind")) for r in no_rows}
    for kind in sorted(side):
        steps = sorted({r["step"] for r in cc_rows if r["kind"] == kind},
                       key=lambda s: _step_order(cc, s))
        if kind not in no_side:
            for s in steps:
                found.add("side-requests", f"Claude Code makes a {kind} request, noeta does "
                                           "not", f"{name}/{s}")
    for kind in sorted(no_side - side - {"main", "compaction"}):
        if not kind.startswith("subagent:"):
            for r in no_rows:
                if r.get("kind") == kind:
                    found.add("side-requests", f"noeta makes a {kind} request Claude Code "
                                               "does not", f"{name}/{r['step']}")


def _step_order(cc: Mapping[str, Any], step: str) -> int:
    for i, r in enumerate(cc["requests"]):
        if r["step"] == step:
            return i
    return 10_000


def union_tools(rows: Iterable[Mapping[str, Any]], toolsets: Mapping[str, Any]
                ) -> list[Mapping[str, Any]]:
    """Every tool the requests offered, in first-seen order, each as last seen."""
    out: dict[str, Mapping[str, Any]] = {}
    for r in rows:
        for t in toolsets.get(r.get("toolset") or "", []):
            out[t["name"]] = t
    return list(out.values())


def _compare_pair(a: Mapping[str, Any], b: Mapping[str, Any], where: str, found: Findings,
                  prefix: str = "") -> None:
    for d in diff_params(a["params"], b["params"]):
        found.add("params", prefix + d, where)
    for d in diff_headers(a["headers"], b["headers"]):
        found.add("headers", prefix + d, where)
    for d in diff_system(a["system"], b["system"]):
        found.add("system", prefix + d, where)
    if bool(a.get("toolset")) != bool(b.get("toolset")):
        sends = {True: "sends", False: "omits"}
        found.add("tools", prefix + f"tool list: Claude Code {sends[bool(a.get('toolset'))]} "
                  f"it, noeta {sends[bool(b.get('toolset'))]} it", where)
    for cat, d in diff_layout(a, b):
        found.add(cat, prefix + d, where)


def accepted(path: Path) -> set[str]:
    if not path.is_file():
        return set()
    return set(re.findall(r"`([0-9a-f]{8})`", path.read_text("utf-8")))


def render(version: str, found: Findings, skipped: Mapping[str, str], ok_ids: set[str],
           n_top: int = 15) -> tuple[str, int]:
    items = [m for m in found.ranked() if m.id not in ok_ids]
    counts: dict[str, list[int]] = {}
    for m in items:
        c = counts.setdefault(m.category, [0, 0])
        c[0] += 1
        c[1] += len(m.where)
    lines = [f"# Parity report: noeta vs Claude Code {version}", "",
             "Written by `make parity` (`scripts/parity/`): noeta replayed through the "
             "recorded scenarios, each request compared with Claude Code's at the same step. "
             "Each line is one difference; the count is how many requests show it.", "",
             f"**{len(items)} mismatches** ({len(ok_ids)} accepted deviations not counted).",
             "", "| category | mismatches | occurrences |", "|---|---|---|"]
    for cat in sorted(counts, key=lambda c: -CATEGORIES.get(c, 0)):
        lines.append(f"| {cat} | {counts[cat][0]} | {counts[cat][1]} |")
    for s, why in skipped.items():
        lines.append(f"\nSession {s} skipped: {why}")
    lines += ["", f"## Top {n_top}", ""]
    for i, m in enumerate(top(items, n_top), 1):
        lines.append(f"{i}. [{m.category}] {m.text} — {len(m.where)}x, first "
                     f"{m.where[0]} `{m.id}`")
    lines += ["", "## By session and step", "",
              "Each mismatch is listed under the step where it first appears.", ""]
    by_place: dict[str, list[Mismatch]] = {}
    for m in items:
        by_place.setdefault(m.where[0].split("#")[0], []).append(m)
    for place in sorted(by_place, key=lambda p: (p.split("/")[0], _first_index(items, p))):
        lines.append(f"### {place}")
        lines.append("")
        for m in sorted(by_place[place], key=lambda m: list(CATEGORIES).index(m.category)
                        if m.category in CATEGORIES else 99):
            lines.append(f"- [{m.category}] {m.text} ({len(m.where)}x) `{m.id}`")
        lines.append("")
    return "\n".join(lines) + "\n", len(items)


def _first_index(items: list[Mismatch], place: str) -> int:
    return min((i for i, m in enumerate(items) if m.where[0].startswith(place)), default=0)


def compare_all(contract_dir: Path, noeta: Mapping[str, Any],
                deviations: Path | None = None) -> tuple[str, int]:
    """The markdown report and the number of (not accepted) mismatches."""
    cc_tools = json.loads((contract_dir / "tools.json").read_text("utf-8"))
    found = Findings()
    skipped: dict[str, str] = {}
    for name, data in noeta["sessions"].items():
        if "skipped" in data:
            skipped[name] = data["skipped"]
            continue
        cc = json.loads((contract_dir / f"{name}.json").read_text("utf-8"))
        compare_session(name, cc, data, cc_tools, noeta["toolsets"], found)
    ok_ids = accepted(deviations or contract_dir.parent / "deviations.md")
    return render(contract_dir.name, found, skipped, ok_ids)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Compare a replay capture with the contract.")
    ap.add_argument("--version", default="2.1.312")
    ap.add_argument("--out", type=Path, default=ROOT / "parity" / "out")
    ap.add_argument("--report", type=Path, default=ROOT / "parity" / "report.md")
    args = ap.parse_args(argv)
    sessions = {p.stem: json.loads(p.read_text("utf-8")) for p in sorted(args.out.glob("*.json")) if p.name != "tools.json"}
    toolsets = json.loads((args.out / "tools.json").read_text("utf-8"))
    report, count = compare_all(ROOT / "parity" / args.version,
                                {"sessions": sessions, "toolsets": toolsets})
    args.report.write_text(report, "utf-8")
    print(report.split("\n## By session", 1)[0])
    raise SystemExit(1 if count else 0)


if __name__ == "__main__":
    main()
